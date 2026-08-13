from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select

from sluicery.core import settings as core_settings
from sluicery.core.sync import SyncAlreadyRunningError, enqueue_discover_run, execute_download_run
from sluicery.db.models import (
    Playlist,
    PlaylistKindHint,
    Run,
    RunStatus,
    RunTrigger,
    Task,
)
from sluicery.scheduler import SchedulerService, SymmetricCronTrigger, parse_download_window


def _playlist(
    session_factory,
    *,
    discover_cron: str | None = None,
    download_cron: str | None = None,
    paused: bool = False,
) -> int:
    with session_factory() as session:
        playlist = Playlist(
            name="scheduled",
            folder_name="scheduled",
            url="https://example.com/playlist",
            kind_hint=PlaylistKindHint.VIDEO,
            discover_cron=discover_cron,
            download_cron=download_cron,
            paused=paused,
        )
        session.add(playlist)
        session.commit()
        return playlist.id


def test_symmetric_cron_jitter_moves_in_both_directions(monkeypatch) -> None:
    timezone = ZoneInfo("Asia/Tokyo")
    now = datetime(2026, 8, 13, 0, 0, tzinfo=timezone)
    trigger = SymmetricCronTrigger.from_expression(
        "0 6 * * *",
        timezone=timezone,
        jitter_minutes=5,
    )

    monkeypatch.setattr("sluicery.scheduler.random.uniform", lambda _low, _high: -300)
    early = trigger.get_next_fire_time(None, now)
    monkeypatch.setattr("sluicery.scheduler.random.uniform", lambda _low, _high: 300)
    late = trigger.get_next_fire_time(None, now)

    assert early == datetime(2026, 8, 13, 5, 55, tzinfo=timezone)
    assert late == datetime(2026, 8, 13, 6, 5, tzinfo=timezone)


def test_scheduler_registers_independent_persistent_jobs_in_configured_timezone(
    engine, session_factory
) -> None:
    playlist_id = _playlist(
        session_factory,
        discover_cron="1 * * * *",
        download_cron="2 * * * *",
    )
    with session_factory() as session:
        core_settings.set_override(session, "schedule.jitter_minutes", 0)
    service = SchedulerService(engine, session_factory, "Asia/Tokyo")

    service.start(paused=True)
    try:
        rows = service.next_runs(playlist_id)
        tables = set(inspect(engine).get_table_names())
    finally:
        service.shutdown()

    assert [row.kind for row in rows] == ["discover", "download"]
    assert [row.scheduled_at.minute for row in rows] == [1, 2]
    assert all(row.scheduled_at.tzinfo == ZoneInfo("Asia/Tokyo") for row in rows)
    assert "apscheduler_jobs" in tables


def test_paused_playlist_is_removed_from_schedule(engine, session_factory) -> None:
    playlist_id = _playlist(session_factory)
    with session_factory() as session:
        core_settings.set_override(session, "schedule.jitter_minutes", 0)
    service = SchedulerService(engine, session_factory, "UTC")
    service.start(paused=True)
    try:
        assert len(service.next_runs(playlist_id)) == 2
        with session_factory() as session:
            playlist = session.get(Playlist, playlist_id)
            assert playlist is not None
            playlist.paused = True
            session.commit()
        service.reconcile()
        assert service.next_runs(playlist_id) == []
    finally:
        service.shutdown()


def test_manual_and_scheduled_sync_share_playlist_exclusion(engine, session_factory) -> None:
    playlist_id = _playlist(session_factory)
    with session_factory() as session:
        manual_run, _task = enqueue_discover_run(session, playlist_id)

    with session_factory() as session:
        try:
            execute_download_run(session, playlist_id)
        except SyncAlreadyRunningError:
            pass
        else:
            raise AssertionError("同一Playlistのdownloadが開始されました")

    service = SchedulerService(engine, session_factory, "UTC")
    service.execute_job(playlist_id, "download")

    with session_factory() as session:
        scheduled = session.scalar(
            select(Run).where(Run.trigger == RunTrigger.SCHEDULE).order_by(Run.id.desc())
        )
        assert scheduled is not None
        assert scheduled.status == RunStatus.SKIPPED
        assert scheduled.stats_json is not None
        assert scheduled.stats_json["skip_reason"] == "active_sync"
        assert session.scalar(select(Task).where(Task.run_id == manual_run.id)) is not None


def test_download_window_supports_daytime_overnight_and_equal_boundaries() -> None:
    daytime = parse_download_window("08:00-17:00")
    overnight = parse_download_window("23:00-05:00")
    all_day = parse_download_window("00:00-00:00")
    assert daytime is not None and overnight is not None and all_day is not None

    assert daytime.contains(datetime(2026, 8, 14, 8, 0))
    assert not daytime.contains(datetime(2026, 8, 14, 17, 0))
    assert overnight.contains(datetime(2026, 8, 14, 23, 30))
    assert overnight.contains(datetime(2026, 8, 15, 4, 59))
    assert not overnight.contains(datetime(2026, 8, 14, 12, 0))
    assert all_day.contains(datetime(2026, 8, 14, 12, 0))


def test_invalid_download_window_is_rejected() -> None:
    for value in ("23:00", "24:00-05:00", "23:60-05:00"):
        try:
            parse_download_window(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"不正なwindowを受理しました: {value}")


def test_scheduled_download_outside_window_records_skip_without_tasks(
    engine, session_factory
) -> None:
    playlist_id = _playlist(session_factory)
    with session_factory() as session:
        core_settings.set_override(session, "schedule.download_window", "23:00-05:00")
    service = SchedulerService(
        engine,
        session_factory,
        "Asia/Tokyo",
        clock=lambda: datetime(2026, 8, 14, 3, 0, tzinfo=ZoneInfo("UTC")),
    )

    service.execute_job(playlist_id, "download")

    with session_factory() as session:
        run = session.scalar(select(Run).order_by(Run.id.desc()))
        assert run is not None
        assert run.status == RunStatus.SKIPPED
        assert run.stats_json is not None
        assert run.stats_json["skip_reason"] == "outside_download_window"
        assert list(session.scalars(select(Task))) == []
