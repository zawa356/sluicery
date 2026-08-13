from __future__ import annotations

from pathlib import Path

import pytest

from sluicery.core.cookies import save_playlist_cookie
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
)
from sluicery.downloader.errors import Classification
from sluicery.downloader.ytdlp import RunResult
from sluicery.tasks.handlers.download import DownloadHandler
from sluicery.tasks.queue import TaskOutcome


class _FakeRunner:
    def __init__(self, result: RunResult) -> None:
        self.result = result
        self.sensitive_values: tuple[str, ...] = ()
        self.args: list[str] = []
        self.cookie_path_existed = False

    def run(self, args, *, timeout, on_progress=None, cwd=None, sensitive_values=()):
        self.args = args
        self.sensitive_values = sensitive_values
        if "--cookies" in args:
            cookie_path = Path(args[args.index("--cookies") + 1])
            self.cookie_path_existed = cookie_path.exists()
        return self.result

    def cancel(self) -> None:
        pass


def _target(session_factory, source_url: str = "https://example.com/item") -> int:
    with session_factory() as session:
        storage = Storage(name="local", kind=StorageKind.LOCAL, config_json={"path": "out"})
        profile = Profile(
            name="video",
            kind=ProfileKind.VIDEO,
            layout_strategy=LayoutStrategy.FLAT,
        )
        playlist = Playlist(
            name="list",
            folder_name="list",
            url="https://example.com/list",
            kind_hint=PlaylistKindHint.MIXED,
        )
        session.add_all([storage, profile, playlist])
        session.flush()
        assignment = PlaylistProfile(
            playlist_id=playlist.id,
            profile_id=profile.id,
            storage_id=storage.id,
            subpath="list",
        )
        item = Item(playlist_id=playlist.id, source_id="item", source_url=source_url)
        session.add_all([assignment, item])
        session.flush()
        target = Target(
            item_id=item.id,
            playlist_profile_id=assignment.id,
            status=TargetStatus.QUEUED,
        )
        session.add(target)
        session.commit()
        return target.id


def test_download_success_records_staging_file(session_factory, tmp_path: Path) -> None:
    target_id = _target(session_factory)
    work = tmp_path / "work-1"
    work.mkdir()
    output = work / "item.mkv"
    output.write_bytes(b"media")
    runner = _FakeRunner(
        RunResult(
            0,
            Classification.OK,
            stdout_lines=[str(output)],
            result_metadata=[{"file_path": str(output.resolve()), "format_id": "137+140"}],
        )
    )
    handler = DownloadHandler(session_factory, staging_dir=tmp_path, runner=runner)  # type: ignore[arg-type]

    result = handler.run({"target_id": target_id, "work_id": "work-1"}, lambda _: None)

    assert result.outcome == TaskOutcome.SUCCEEDED
    assert result.payload_update == {
        "file_path": str(output.resolve()),
        "filesize": 5,
        "format_id": "137+140",
    }
    assert runner.sensitive_values == ("https://example.com/item",)
    with session_factory() as session:
        assert session.get(Target, target_id).status == TargetStatus.DOWNLOADING


@pytest.mark.parametrize(
    ("classification", "outcome", "status", "retry_count"),
    [
        (Classification.FAILED, TaskOutcome.FAILED, TargetStatus.FAILED, 1),
        (
            Classification.UNAVAILABLE,
            TaskOutcome.UNAVAILABLE,
            TargetStatus.UNAVAILABLE,
            0,
        ),
        (Classification.BLOCKED, TaskOutcome.BLOCKED, TargetStatus.BLOCKED, 0),
    ],
)
def test_download_maps_classification(
    session_factory,
    tmp_path: Path,
    classification,
    outcome,
    status,
    retry_count,
) -> None:
    target_id = _target(session_factory)
    handler = DownloadHandler(
        session_factory,
        staging_dir=tmp_path,
        runner=_FakeRunner(RunResult(1, classification, stderr_tail="reason")),  # type: ignore[arg-type]
    )

    result = handler.run({"target_id": target_id, "work_id": "work"}, lambda _: None)

    assert result.outcome == outcome
    with session_factory() as session:
        target = session.get(Target, target_id)
        assert target.status == status
        assert target.retry_count == retry_count


def test_download_materializes_playlist_cookie_and_cleans_it(
    session_factory, tmp_path: Path
) -> None:
    target_id = _target(session_factory)
    with session_factory() as session:
        target = session.get(Target, target_id)
        assert target is not None
        item = session.get(Item, target.item_id)
        assert item is not None
        playlist = session.get(Playlist, item.playlist_id)
        assert playlist is not None
        save_playlist_cookie(
            session,
            playlist,
            b"""# Netscape HTTP Cookie File
.example.com\tTRUE\t/\tTRUE\t2147483647\tSID\tdownload-cookie-secret
""",
            enable_confirmed=True,
        )
    work = tmp_path / "work-cookie"
    work.mkdir()
    output = work / "item.mkv"
    output.write_bytes(b"media")
    runner = _FakeRunner(
        RunResult(0, Classification.OK, stdout_lines=[str(output)])
    )
    handler = DownloadHandler(
        session_factory,
        staging_dir=tmp_path,
        runner=runner,  # type: ignore[arg-type]
        cookie_runtime_dir=tmp_path / "runtime",
    )

    result = handler.run(
        {"target_id": target_id, "work_id": "work-cookie"},
        lambda _: None,
    )

    assert result.outcome == TaskOutcome.SUCCEEDED
    assert runner.cookie_path_existed is True
    cookie_path = Path(runner.args[runner.args.index("--cookies") + 1])
    assert not cookie_path.exists()
    assert str(cookie_path) in runner.sensitive_values
    assert "download-cookie-secret" in runner.sensitive_values
