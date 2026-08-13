from __future__ import annotations

import argparse

import pytest

from sluicery import cli_sync
from sluicery.db.models import Playlist, PlaylistKindHint, Run, RunStatus, RunTrigger


def _playlist(session_factory, *, name: str, enabled: bool = True, paused: bool = False):
    with session_factory() as session:
        playlist = Playlist(
            name=name,
            folder_name=name,
            url=f"https://example.com/{name}",
            enabled=enabled,
            paused=paused,
            kind_hint=PlaylistKindHint.VIDEO,
        )
        session.add(playlist)
        session.commit()
        return playlist.id


def _run(kind: str, playlist_id: int, *, empty: bool = False) -> Run:
    return Run(
        id=100 if kind == "discover" else 101,
        trigger=RunTrigger.MANUAL,
        kind=kind,
        playlist_id=playlist_id,
        status=RunStatus.SUCCEEDED,
        stats_json={
            "new_items": 0,
            "delisted_items": 0,
            "targets_queued": 0,
            "targets_remaining": 0,
            "downloaded": 0,
            "failed": 0,
            "blocked": 0,
            "empty_result": empty,
        },
    )


def _args(command: str, *, playlist: str | None = None, all_: bool = False, dry=False):
    values = {
        "command": "sync",
        "sync_command": command,
        "playlist": playlist,
        "all": all_,
    }
    if command in {"discover", "run"}:
        values["dry_run"] = dry
    return argparse.Namespace(**values)


def test_sync_all_skips_disabled_and_paused_playlists(session_factory, monkeypatch, capsys):
    active = _playlist(session_factory, name="active")
    _playlist(session_factory, name="disabled", enabled=False)
    _playlist(session_factory, name="paused", paused=True)
    seen = []

    def discover(_open_session, playlist_id, **_kwargs):
        seen.append(playlist_id)
        return _run("discover", playlist_id)

    monkeypatch.setattr(cli_sync, "_execute_discover", discover)

    result = cli_sync.dispatch(
        _args("discover", all_=True), open_session=session_factory, poll_interval_sec=0
    )

    assert result == 0
    assert seen == [active]
    assert "Run 100" in capsys.readouterr().out


def test_sync_run_stops_after_empty_discover(session_factory, monkeypatch, capsys):
    playlist_id = _playlist(session_factory, name="active")
    monkeypatch.setattr(
        cli_sync,
        "_execute_discover",
        lambda *_args, **_kwargs: _run("discover", playlist_id, empty=True),
    )
    downloads = []
    monkeypatch.setattr(
        cli_sync,
        "execute_download_run",
        lambda *_args, **_kwargs: downloads.append(True),
    )

    result = cli_sync.dispatch(
        _args("run", playlist=str(playlist_id)),
        open_session=session_factory,
        poll_interval_sec=0,
    )

    assert result == 0
    assert downloads == []
    assert "discover結果が空" in capsys.readouterr().out


def test_sync_run_dry_run_never_starts_download(session_factory, monkeypatch):
    playlist_id = _playlist(session_factory, name="active")
    monkeypatch.setattr(
        cli_sync,
        "_execute_discover",
        lambda *_args, **_kwargs: _run("discover", playlist_id),
    )
    downloads = []
    monkeypatch.setattr(
        cli_sync,
        "execute_download_run",
        lambda *_args, **_kwargs: downloads.append(True),
    )

    result = cli_sync.dispatch(
        _args("run", playlist="active", dry=True),
        open_session=session_factory,
        poll_interval_sec=0,
    )

    assert result == 0
    assert downloads == []


def test_sync_download_prints_run_stats(session_factory, monkeypatch, capsys):
    playlist_id = _playlist(session_factory, name="active")
    monkeypatch.setattr(
        cli_sync,
        "execute_download_run",
        lambda _session, selected: _run("download", selected),
    )

    result = cli_sync.dispatch(
        _args("download", playlist="active"),
        open_session=session_factory,
    )

    assert result == 0
    output = capsys.readouterr().out
    assert f"Playlist {playlist_id}" in output
    assert "targets_queued: 0" in output


def test_sync_unknown_playlist_returns_error(session_factory, capsys):
    result = cli_sync.dispatch(
        _args("download", playlist="unknown"),
        open_session=session_factory,
    )

    assert result == 1
    assert "有効なPlaylistが見つかりません" in capsys.readouterr().err


def test_sync_run_all_discovers_every_playlist_before_any_download(session_factory, monkeypatch):
    first = _playlist(session_factory, name="first")
    second = _playlist(session_factory, name="second")
    events = []

    def discover(_open_session, playlist_id, **_kwargs):
        events.append(("discover", playlist_id))
        return _run("discover", playlist_id)

    def download(_session, playlist_id):
        events.append(("download", playlist_id))
        return _run("download", playlist_id)

    monkeypatch.setattr(cli_sync, "_execute_discover", discover)
    monkeypatch.setattr(cli_sync, "execute_download_run", download)

    result = cli_sync.dispatch(
        _args("run", all_=True), open_session=session_factory, poll_interval_sec=0
    )

    assert result == 0
    assert events == [
        ("discover", first),
        ("discover", second),
        ("download", first),
        ("download", second),
    ]


def test_interrupted_discover_cancels_task_and_run(session_factory, monkeypatch):
    playlist_id = _playlist(session_factory, name="active")
    monkeypatch.setattr(
        cli_sync,
        "_wait_for_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    with pytest.raises(KeyboardInterrupt):
        cli_sync._execute_discover(
            session_factory,
            playlist_id,
            dry_run=False,
            poll_interval_sec=0,
        )

    with session_factory() as session:
        run = session.query(Run).one()
        assert run.status == RunStatus.CANCELLED
