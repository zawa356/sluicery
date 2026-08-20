from __future__ import annotations

from pathlib import Path

import pytest

from sluicery.db.models import (
    Artifact,
    ArtifactRole,
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
from sluicery.storage.base import PublishResult, RemoteFile
from sluicery.storage.errors import StorageClassification
from sluicery.storage.local import LocalStorageAdapter
from sluicery.tasks.handlers.publish import PublishHandler
from sluicery.tasks.queue import TaskOutcome


class _Adapter:
    def __init__(self, classification=StorageClassification.OK, *, existing_size=None):
        self.classification = classification
        self.existing_size = existing_size
        self.published = False

    def free_space(self):
        return 100 * 1024**3

    def exists(self, rel):
        return self.existing_size is not None or self.published

    def list_recursive(self, rel):
        if self.existing_size is not None:
            yield RemoteFile("folder/media.mkv", self.existing_size)

    def publish(self, src, dest_rel, *, overwrite=False):
        if self.classification != StorageClassification.OK:
            return PublishResult(False, dest_rel, None, self.classification, "error", "error")
        self.published = True
        return PublishResult(True, dest_rel, src.stat().st_size, self.classification, "ok", "ok")


def _graph(session_factory, file_path: Path) -> tuple[int, int]:
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
        postprocess = Task(
            type=TaskType.POSTPROCESS,
            target_ref_type="target",
            target_ref_id=target.id,
            payload_json={"file_path": str(file_path)},
            worker_class=WorkerClass.COMPUTE,
            status=TaskStatus.SUCCEEDED,
        )
        session.add(postprocess)
        session.flush()
        publish = Task(
            type=TaskType.PUBLISH,
            target_ref_type="target",
            target_ref_id=target.id,
            payload_json={"target_id": target.id, "work_id": "work"},
            worker_class=WorkerClass.NETWORK,
            status=TaskStatus.RUNNING,
            depends_on_task_id=postprocess.id,
        )
        session.add(publish)
        session.commit()
        return target.id, publish.id


@pytest.mark.parametrize(
    ("classification", "outcome", "target_status"),
    [
        (StorageClassification.OK, TaskOutcome.SUCCEEDED, TargetStatus.PROCESSING),
        (StorageClassification.UNREACHABLE, TaskOutcome.BLOCKED, TargetStatus.BLOCKED),
        (StorageClassification.NO_SPACE, TaskOutcome.BLOCKED, TargetStatus.BLOCKED),
        (StorageClassification.AUTH_FAILED, TaskOutcome.FAILED, TargetStatus.FAILED),
        (StorageClassification.PERMISSION_DENIED, TaskOutcome.FAILED, TargetStatus.FAILED),
    ],
)
def test_publish_maps_storage_classification(
    session_factory, tmp_path: Path, classification, outcome, target_status
) -> None:
    work = tmp_path / "work" / "folder"
    work.mkdir(parents=True)
    source = work / "media.mkv"
    source.write_bytes(b"media")
    target_id, task_id = _graph(session_factory, source)
    adapter = _Adapter(classification)
    handler = PublishHandler(
        session_factory,
        staging_dir=tmp_path,
        adapter_factory=lambda storage, settings: adapter,
    )
    result = handler.run(
        {
            "target_id": target_id,
            "work_id": "work",
            "_execution": {"task_id": task_id},
        },
        lambda _: None,
    )

    assert result.outcome == outcome
    assert source.exists(), "Staging削除はindexまで行わない"
    with session_factory() as session:
        assert session.get(Target, target_id).status == target_status


def test_publish_resumes_when_existing_size_matches(session_factory, tmp_path: Path) -> None:
    work = tmp_path / "work" / "folder"
    work.mkdir(parents=True)
    source = work / "media.mkv"
    source.write_bytes(b"media")
    target_id, task_id = _graph(session_factory, source)
    adapter = _Adapter(existing_size=5)
    result = PublishHandler(
        session_factory,
        staging_dir=tmp_path,
        adapter_factory=lambda storage, settings: adapter,
    ).run(
        {"target_id": target_id, "work_id": "work", "_execution": {"task_id": task_id}},
        lambda _: None,
    )

    assert result.outcome == TaskOutcome.SUCCEEDED
    assert result.payload_update["publish_resumed"] is True
    assert not adapter.published


def test_publish_refuses_existing_file_with_different_size(
    session_factory, tmp_path: Path
) -> None:
    work = tmp_path / "work" / "folder"
    work.mkdir(parents=True)
    source = work / "media.mkv"
    source.write_bytes(b"media")
    target_id, task_id = _graph(session_factory, source)
    adapter = _Adapter(existing_size=4)

    result = PublishHandler(
        session_factory,
        staging_dir=tmp_path,
        adapter_factory=lambda storage, settings: adapter,
    ).run(
        {"target_id": target_id, "work_id": "work", "_execution": {"task_id": task_id}},
        lambda _: None,
    )

    assert result.outcome == TaskOutcome.FAILED
    assert not adapter.published
    assert source.exists()
    with session_factory() as session:
        assert session.get(Target, target_id).status == TargetStatus.FAILED


def test_publish_recovery_moves_blocked_target_back_to_processing(
    session_factory, tmp_path: Path
) -> None:
    work = tmp_path / "work" / "folder"
    work.mkdir(parents=True)
    source = work / "media.mkv"
    source.write_bytes(b"media")
    target_id, task_id = _graph(session_factory, source)
    with session_factory() as session:
        target = session.get(Target, target_id)
        target.status = TargetStatus.BLOCKED
        session.commit()
    adapter = _Adapter()

    result = PublishHandler(
        session_factory,
        staging_dir=tmp_path,
        adapter_factory=lambda storage, settings: adapter,
    ).run(
        {"target_id": target_id, "work_id": "work", "_execution": {"task_id": task_id}},
        lambda _: None,
    )

    assert result.outcome == TaskOutcome.SUCCEEDED
    with session_factory() as session:
        assert session.get(Target, target_id).status == TargetStatus.PROCESSING


@pytest.mark.parametrize(
    ("existing_bytes", "expects_hardlink"),
    [(b"same-media", True), (b"other-data", False)],
)
def test_publish_dedup_uses_strong_hash_before_local_hardlink(
    session_factory, tmp_path: Path, existing_bytes: bytes, expects_hardlink: bool
) -> None:
    media_root = tmp_path / "media"
    existing_path = media_root / "out" / "first" / "media.mkv"
    existing_path.parent.mkdir(parents=True)
    existing_path.write_bytes(existing_bytes)
    staging = tmp_path / "staging"
    staged_path = staging / "work" / "second" / "media.mkv"
    staged_path.parent.mkdir(parents=True)
    staged_path.write_bytes(b"same-media")
    with session_factory() as session:
        storage = Storage(name="s", kind=StorageKind.LOCAL, config_json={"path": "out"})
        profile = Profile(name="p", kind=ProfileKind.VIDEO, layout_strategy=LayoutStrategy.FLAT)
        first_playlist = Playlist(
            name="first",
            folder_name="first",
            url="https://example.com/first",
            kind_hint=PlaylistKindHint.VIDEO,
        )
        second_playlist = Playlist(
            name="second",
            folder_name="second",
            url="https://example.com/second",
            kind_hint=PlaylistKindHint.VIDEO,
            dedup_hardlink=True,
        )
        session.add_all([storage, profile, first_playlist, second_playlist])
        session.flush()
        first_assignment = PlaylistProfile(
            playlist_id=first_playlist.id, profile_id=profile.id, storage_id=storage.id
        )
        second_assignment = PlaylistProfile(
            playlist_id=second_playlist.id, profile_id=profile.id, storage_id=storage.id
        )
        session.add_all([first_assignment, second_assignment])
        session.flush()
        first_item = Item(
            playlist_id=first_playlist.id,
            source_id="shared-source",
            source_url="https://example.com/item",
        )
        second_item = Item(
            playlist_id=second_playlist.id,
            source_id="shared-source",
            source_url="https://example.com/item",
        )
        session.add_all([first_item, second_item])
        session.flush()
        first_target = Target(
            item_id=first_item.id,
            playlist_profile_id=first_assignment.id,
            status=TargetStatus.DOWNLOADED,
        )
        second_target = Target(
            item_id=second_item.id,
            playlist_profile_id=second_assignment.id,
            status=TargetStatus.PROCESSING,
        )
        session.add_all([first_target, second_target])
        session.flush()
        session.add(
            Artifact(
                target_id=first_target.id,
                role=ArtifactRole.SOURCE,
                storage_id=storage.id,
                relative_path="first/media.mkv",
                filesize=existing_path.stat().st_size,
            )
        )
        postprocess = Task(
            type=TaskType.POSTPROCESS,
            target_ref_type="target",
            target_ref_id=second_target.id,
            payload_json={"file_path": str(staged_path)},
            worker_class=WorkerClass.COMPUTE,
            status=TaskStatus.SUCCEEDED,
        )
        session.add(postprocess)
        session.flush()
        publish = Task(
            type=TaskType.PUBLISH,
            target_ref_type="target",
            target_ref_id=second_target.id,
            payload_json={"target_id": second_target.id, "work_id": "work"},
            worker_class=WorkerClass.NETWORK,
            status=TaskStatus.RUNNING,
            depends_on_task_id=postprocess.id,
        )
        session.add(publish)
        session.commit()
        target_id = second_target.id
        task_id = publish.id

    adapter = LocalStorageAdapter("out", media_root=media_root)
    handler = PublishHandler(
        session_factory,
        staging_dir=staging,
        adapter_factory=lambda _storage, _settings: adapter,
    )
    result = handler.run(
        {"target_id": target_id, "work_id": "work", "_execution": {"task_id": task_id}},
        lambda _: None,
    )

    linked_path = media_root / "out" / "second" / "media.mkv"
    assert result.outcome == TaskOutcome.SUCCEEDED
    assert result.payload_update.get("publish_hardlinked", False) is expects_hardlink
    assert (existing_path.stat().st_ino == linked_path.stat().st_ino) is expects_hardlink
    assert linked_path.read_bytes() == b"same-media"
    assert handler.log_paths
