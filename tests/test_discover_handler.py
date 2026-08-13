from __future__ import annotations

import json

from sqlalchemy import func, select

from sluicery.db.models import (
    Item,
    Playlist,
    PlaylistKindHint,
    Run,
    RunStatus,
    RunTrigger,
)
from sluicery.downloader.errors import Classification
from sluicery.downloader.ytdlp import RunResult
from sluicery.tasks.handlers.discover import DiscoverHandler
from sluicery.tasks.queue import TaskOutcome


class _Runner:
    def __init__(self, result: RunResult) -> None:
        self.result = result
        self.args: list[str] = []
        self.sensitive_values: tuple[str, ...] = ()

    def cancel(self) -> None:
        pass

    def run(self, args, *, timeout, sensitive_values=()):
        self.args = args
        self.sensitive_values = sensitive_values
        return self.result


def _records(session_factory):
    with session_factory() as session:
        playlist = Playlist(
            name="list",
            folder_name="list",
            url="https://example.com/playlist/private-token",
            kind_hint=PlaylistKindHint.VIDEO,
        )
        session.add(playlist)
        session.flush()
        run = Run(
            trigger=RunTrigger.MANUAL,
            kind="discover",
            playlist_id=playlist.id,
        )
        session.add(run)
        session.commit()
        return playlist.id, run.id, playlist.url


def test_discover_handler_runs_flat_playlist_and_records_stats(session_factory):
    playlist_id, run_id, url = _records(session_factory)
    runner = _Runner(
        RunResult(
            returncode=0,
            classification=Classification.OK,
            stdout_lines=[
                json.dumps(
                    {
                        "id": "one",
                        "webpage_url": "https://example.com/watch/one",
                        "title": "One",
                        "playlist_index": 1,
                    }
                )
            ],
        )
    )
    progress = []
    handler = DiscoverHandler(session_factory, runner=runner)  # type: ignore[arg-type]

    result = handler.run(
        {
            "playlist_id": playlist_id,
            "dry_run": False,
            "_execution": {"run_id": run_id},
        },
        progress.append,
    )

    assert result.outcome == TaskOutcome.SUCCEEDED
    assert "--flat-playlist" in runner.args
    assert "--simulate" in runner.args
    assert runner.sensitive_values == (url,)
    with session_factory() as session:
        run = session.get(Run, run_id)
        assert run is not None
        assert run.status == RunStatus.SUCCEEDED
        assert run.stats_json == {
            "new_items": 1,
            "delisted_items": 0,
            "targets_queued": 0,
            "targets_remaining": 0,
            "downloaded": 0,
            "failed": 0,
            "blocked": 0,
            "empty_result": False,
        }
        assert session.scalar(select(func.count()).select_from(Item)) == 1


def test_discover_handler_empty_result_is_success_without_delisting(session_factory):
    playlist_id, run_id, _ = _records(session_factory)
    with session_factory() as session:
        session.add(
            Item(
                playlist_id=playlist_id,
                source_id="existing",
                source_url="https://example.com/watch/existing",
            )
        )
        session.commit()
    runner = _Runner(RunResult(returncode=0, classification=Classification.OK))
    handler = DiscoverHandler(session_factory, runner=runner)  # type: ignore[arg-type]

    result = handler.run(
        {"playlist_id": playlist_id, "_execution": {"run_id": run_id}}, lambda _: None
    )

    assert result.outcome == TaskOutcome.SUCCEEDED
    with session_factory() as session:
        item = session.scalar(select(Item))
        run = session.get(Run, run_id)
        assert item is not None and item.membership.value == "active"
        assert run is not None and run.stats_json is not None
        assert run.stats_json["empty_result"] is True


def test_discover_handler_dry_run_does_not_create_item(session_factory):
    playlist_id, run_id, _ = _records(session_factory)
    runner = _Runner(
        RunResult(
            returncode=0,
            classification=Classification.OK,
            stdout_lines=[
                json.dumps(
                    {
                        "id": "one",
                        "url": "https://example.com/watch/one",
                    }
                )
            ],
        )
    )
    handler = DiscoverHandler(session_factory, runner=runner)  # type: ignore[arg-type]

    result = handler.run(
        {
            "playlist_id": playlist_id,
            "dry_run": True,
            "_execution": {"run_id": run_id},
        },
        lambda _: None,
    )

    assert result.outcome == TaskOutcome.SUCCEEDED
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Item)) == 0
        run = session.get(Run, run_id)
        assert run is not None and run.stats_json is not None
        assert run.stats_json["new_items"] == 1


def test_discover_error_records_empty_result_without_domain_changes(session_factory):
    playlist_id, run_id, _ = _records(session_factory)
    with session_factory() as session:
        session.add(
            Item(
                playlist_id=playlist_id,
                source_id="existing",
                source_url="https://example.com/watch/existing",
            )
        )
        session.commit()
    runner = _Runner(
        RunResult(
            returncode=1,
            classification=Classification.UNAVAILABLE,
            stderr_tail="playlist unavailable",
        )
    )
    handler = DiscoverHandler(session_factory, runner=runner)  # type: ignore[arg-type]

    result = handler.run(
        {"playlist_id": playlist_id, "_execution": {"run_id": run_id}}, lambda _: None
    )

    assert result.outcome == TaskOutcome.UNAVAILABLE
    with session_factory() as session:
        item = session.scalar(select(Item))
        run = session.get(Run, run_id)
        assert item is not None and item.membership.value == "active"
        assert run is not None and run.status == RunStatus.FAILED
        assert run.stats_json is not None and run.stats_json["empty_result"] is True
