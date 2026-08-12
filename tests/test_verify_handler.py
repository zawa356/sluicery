from __future__ import annotations

from pathlib import Path

from sluicery.db.models import (
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
from sluicery.runner.ffprobe import ProbeResult
from sluicery.tasks.handlers.verify import VerifyHandler
from sluicery.tasks.queue import TaskOutcome


class _Probe:
    def __init__(self, result: ProbeResult) -> None:
        self.result = result

    def probe(self, file_path: Path, *, timeout_sec: int) -> ProbeResult:
        return self.result

    def cancel(self) -> None:
        pass


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
            status=TargetStatus.DOWNLOADING,
        )
        session.add(target)
        session.flush()
        download = Task(
            type=TaskType.DOWNLOAD,
            target_ref_type="target",
            target_ref_id=target.id,
            payload_json={"file_path": str(file_path)},
            worker_class=WorkerClass.NETWORK,
            status=TaskStatus.SUCCEEDED,
        )
        session.add(download)
        session.flush()
        verify = Task(
            type=TaskType.VERIFY,
            target_ref_type="target",
            target_ref_id=target.id,
            payload_json={"target_id": target.id},
            worker_class=WorkerClass.COMPUTE,
            status=TaskStatus.RUNNING,
            depends_on_task_id=download.id,
        )
        session.add(verify)
        session.commit()
        return target.id, verify.id


def test_verify_records_metadata_without_duration_judgement(
    session_factory, tmp_path: Path
) -> None:
    media = tmp_path / "media.mkv"
    media.write_bytes(b"not-empty")
    target_id, task_id = _graph(session_factory, media)
    runner = _Probe(
        ProbeResult(
            0,
            {
                "format": {"format_name": "matroska,webm", "duration": "12.6"},
                "streams": [
                    {"index": 0, "codec_type": "video", "codec_name": "av1"},
                    {"index": 1, "codec_type": "audio", "codec_name": "opus"},
                ],
            },
            "",
            None,
        )
    )
    result = VerifyHandler(session_factory, runner=runner).run(  # type: ignore[arg-type]
        {"target_id": target_id, "_execution": {"task_id": task_id}}, lambda _: None
    )

    assert result.outcome == TaskOutcome.SUCCEEDED
    assert result.payload_update["duration"] == 13
    assert result.payload_update["video_codec"] == "av1"
    assert result.payload_update["audio_codec"] == "opus"


def test_verify_json_failure_keeps_file_and_records_reason(session_factory, tmp_path: Path) -> None:
    media = tmp_path / "broken.mkv"
    media.write_bytes(b"broken")
    target_id, task_id = _graph(session_factory, media)
    result = VerifyHandler(  # type: ignore[arg-type]
        session_factory,
        runner=_Probe(ProbeResult(0, None, "", None)),
    ).run({"target_id": target_id, "_execution": {"task_id": task_id}}, lambda _: None)

    assert result.outcome == TaskOutcome.FAILED
    assert media.exists()
    assert "JSON" in result.payload_update["verify_error"]
    with session_factory() as session:
        target = session.get(Target, target_id)
        assert target.status == TargetStatus.FAILED
