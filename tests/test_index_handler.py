from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from sluicery.db.models import (
    Artifact,
    EventLog,
    Item,
    LayoutStrategy,
    Playlist,
    PlaylistKindHint,
    PlaylistProfile,
    Profile,
    ProfileKind,
    Storage,
    StorageKind,
    Target,
    TargetStatus,
    Task,
    TaskStatus,
    TaskType,
    WorkerClass,
)
from sluicery.tasks.handlers.index import IndexHandler
from sluicery.tasks.queue import TaskOutcome


def _graph(session_factory, source: Path) -> tuple[int, int]:
    with session_factory() as session:
        storage = Storage(name="s", kind=StorageKind.LOCAL, config_json={"path": "out"})
        profile = Profile(name="p", kind=ProfileKind.VIDEO, layout_strategy=LayoutStrategy.FLAT)
        playlist = Playlist(
            name="p", folder_name="p", url="https://example.com", kind_hint=PlaylistKindHint.VIDEO
        )
        session.add_all([storage, profile, playlist])
        session.flush()
        assignment = PlaylistProfile(
            playlist_id=playlist.id, profile_id=profile.id, storage_id=storage.id
        )
        item = Item(playlist_id=playlist.id, source_id="x", source_url="https://example.com/x")
        session.add_all([assignment, item])
        session.flush()
        target = Target(
            item_id=item.id,
            playlist_profile_id=assignment.id,
            status=TargetStatus.PROCESSING,
        )
        session.add(target)
        session.flush()
        publish = Task(
            type=TaskType.PUBLISH,
            target_ref_type="target",
            target_ref_id=target.id,
            payload_json={
                "file_path": str(source),
                "storage_id": storage.id,
                "relative_path": "folder/media.mkv",
                "filesize": 5,
                "duration": 13,
                "container": "matroska",
                "video_codec": "av1",
                "audio_codec": "opus",
                "verified_at": datetime.now(UTC).isoformat(),
            },
            worker_class=WorkerClass.NETWORK,
            status=TaskStatus.SUCCEEDED,
        )
        session.add(publish)
        session.flush()
        index = Task(
            type=TaskType.INDEX,
            target_ref_type="target",
            target_ref_id=target.id,
            payload_json={"target_id": target.id, "work_id": "work"},
            worker_class=WorkerClass.NETWORK,
            status=TaskStatus.RUNNING,
            depends_on_task_id=publish.id,
        )
        session.add(index)
        session.commit()
        return target.id, index.id


def test_index_creates_artifact_then_deletes_staging(session_factory, tmp_path: Path) -> None:
    directory = tmp_path / "work" / "folder"
    directory.mkdir(parents=True)
    source = directory / "media.mkv"
    source.write_bytes(b"media")
    target_id, task_id = _graph(session_factory, source)
    result = IndexHandler(session_factory, staging_dir=tmp_path).run(
        {"target_id": target_id, "work_id": "work", "_execution": {"task_id": task_id}},
        lambda _: None,
    )

    assert result.outcome == TaskOutcome.SUCCEEDED
    assert not source.exists()
    with session_factory() as session:
        artifact = session.scalar(select(Artifact))
        assert artifact is not None
        assert artifact.produced_by_task_id == task_id
        assert artifact.checksum is None
        assert session.get(Target, target_id).status == TargetStatus.DOWNLOADED
        assert [event.event_type for event in session.scalars(select(EventLog))] == [
            "target_downloaded",
            "artifact_published",
        ]


def test_index_is_idempotent_when_cleanup_is_disabled(
    session_factory, tmp_path: Path
) -> None:
    directory = tmp_path / "work" / "folder"
    directory.mkdir(parents=True)
    source = directory / "media.mkv"
    source.write_bytes(b"media")
    target_id, task_id = _graph(session_factory, source)
    handler = IndexHandler(session_factory, staging_dir=tmp_path, delete_staging=False)
    first = handler.run(
        {"target_id": target_id, "work_id": "work", "_execution": {"task_id": task_id}},
        lambda _: None,
    )
    second = handler.run(
        {"target_id": target_id, "work_id": "work", "_execution": {"task_id": task_id}},
        lambda _: None,
    )

    assert first.outcome == second.outcome == TaskOutcome.SUCCEEDED
    assert source.exists()
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Artifact)) == 1


def test_index_cleanup_oserror_does_not_undo_database_result(
    session_factory, tmp_path: Path, monkeypatch
) -> None:
    directory = tmp_path / "work" / "folder"
    directory.mkdir(parents=True)
    source = directory / "media.mkv"
    source.write_bytes(b"media")
    target_id, task_id = _graph(session_factory, source)

    def fail_unlink(self: Path) -> None:
        raise OSError("busy")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    result = IndexHandler(session_factory, staging_dir=tmp_path).run(
        {"target_id": target_id, "work_id": "work", "_execution": {"task_id": task_id}},
        lambda _: None,
    )

    assert result.outcome == TaskOutcome.SUCCEEDED
    assert result.payload_update["staging_deleted"] is False
    assert source.exists()
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Artifact)) == 1
        assert session.get(Target, target_id).status == TargetStatus.DOWNLOADED
