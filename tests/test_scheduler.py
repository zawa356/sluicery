from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
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
    TaskStatus,
    TaskType,
    WorkerClass,
)
from sluicery.scheduler import (
    SchedulerService,
    SymmetricCronTrigger,
    parse_download_window,
    playlist_job_id,
)


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
    monkeypatch.setattr("sluicery.scheduler.random.uniform", lambda _low, _high: -300)
    early_trigger = SymmetricCronTrigger.from_expression(
        "0 6 * * *",
        timezone=timezone,
        jitter_minutes=5,
    )
    monkeypatch.setattr("sluicery.scheduler.random.uniform", lambda _low, _high: 300)
    late_trigger = SymmetricCronTrigger.from_expression(
        "0 6 * * *",
        timezone=timezone,
        jitter_minutes=5,
    )

    early = early_trigger.get_next_fire_time(None, now)
    late = late_trigger.get_next_fire_time(None, now)

    assert early == datetime(2026, 8, 13, 5, 55, tzinfo=timezone)
    assert late == datetime(2026, 8, 13, 6, 5, tzinfo=timezone)


def test_negative_jitter_advances_to_the_next_cron_occurrence(monkeypatch) -> None:
    timezone = ZoneInfo("Asia/Tokyo")
    monkeypatch.setattr("sluicery.scheduler.random.uniform", lambda _low, _high: -300)
    trigger = SymmetricCronTrigger.from_expression(
        "0 */6 * * *",
        timezone=timezone,
        jitter_minutes=5,
    )
    now = datetime(2026, 8, 13, 0, 0, tzinfo=timezone)

    first = trigger.get_next_fire_time(None, now)
    assert first == datetime(2026, 8, 13, 5, 55, tzinfo=timezone)

    second = trigger.get_next_fire_time(first, first)
    assert second == datetime(2026, 8, 13, 11, 55, tzinfo=timezone)


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


def test_scheduler_registers_daily_integrity_job_without_arguments(
    engine, session_factory, monkeypatch
) -> None:
    with session_factory() as session:
        core_settings.set_override(session, "schedule.jitter_minutes", 0)
        core_settings.set_override(session, "schedule.integrity_cron", "15 3 * * *")
    service = SchedulerService(engine, session_factory, "Asia/Tokyo")

    service.start(paused=True)
    try:
        job = service._scheduler.get_job(  # noqa: SLF001 - 永続job境界の検査
            "sluicery:maintenance:integrity"
        )
        assert job is not None
        assert tuple(job.args) == ()
        assert isinstance(job.trigger, SymmetricCronTrigger)
        assert job.trigger.expression == "15 3 * * *"
        assert job.next_run_time.hour == 3
        assert job.next_run_time.minute == 15
        assert job.coalesce is True
        assert job.max_instances == 1
        assert job.misfire_grace_time == 24 * 60 * 60
        calls: list[bool] = []
        monkeypatch.setattr(service, "execute_integrity_job", lambda: calls.append(True))
        job.func()
        assert calls == [True]
    finally:
        service.shutdown()


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


def test_paused_playlist_does_not_record_outside_window_skip(engine, session_factory) -> None:
    playlist_id = _playlist(session_factory, paused=True)
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
        assert list(session.scalars(select(Run))) == []
        assert list(session.scalars(select(Task))) == []


def test_reconcile_removes_deleted_playlist_jobs_and_sets_misfire_policy(
    engine, session_factory
) -> None:
    playlist_id = _playlist(session_factory)
    with session_factory() as session:
        core_settings.set_override(session, "schedule.jitter_minutes", 0)
    service = SchedulerService(engine, session_factory, "UTC")
    service.start(paused=True)
    try:
        jobs = [
            job
            for job in service._scheduler.get_jobs()  # noqa: SLF001 - policy inspection
            if job.id.startswith("sluicery:playlist:")
        ]
        assert len(jobs) == 2
        assert all(job.coalesce is True for job in jobs)
        assert all(job.misfire_grace_time == 24 * 60 * 60 for job in jobs)
        assert all(job.max_instances == 1 for job in jobs)
        with session_factory() as session:
            playlist = session.get(Playlist, playlist_id)
            assert playlist is not None
            session.delete(playlist)
            session.commit()
        service.reconcile()
        assert service.next_runs(playlist_id) == []
    finally:
        service.shutdown()


def test_reconcile_recovers_only_runs_without_active_tasks(engine, session_factory) -> None:
    playlist_id = _playlist(session_factory)
    now = datetime(2026, 8, 14, 0, 0, tzinfo=UTC)
    with session_factory() as session:
        orphan = Run(
            trigger=RunTrigger.SCHEDULE,
            kind="discover",
            playlist_id=playlist_id,
            status=RunStatus.RUNNING,
            started_at=now - timedelta(minutes=10),
        )
        active = Run(
            trigger=RunTrigger.MANUAL,
            kind="discover",
            playlist_id=playlist_id,
            status=RunStatus.RUNNING,
            started_at=now - timedelta(minutes=10),
        )
        live_taskless_download = Run(
            trigger=RunTrigger.MANUAL,
            kind="download",
            playlist_id=playlist_id,
            status=RunStatus.RUNNING,
            started_at=now - timedelta(minutes=10),
        )
        old_taskless_download = Run(
            trigger=RunTrigger.MANUAL,
            kind="download",
            playlist_id=playlist_id,
            status=RunStatus.RUNNING,
            started_at=now - timedelta(hours=25),
        )
        session.add_all([orphan, active, live_taskless_download, old_taskless_download])
        session.flush()
        session.add(
            Task(
                type=TaskType.DISCOVER,
                target_ref_type="playlist",
                target_ref_id=playlist_id,
                payload_json={"playlist_id": playlist_id},
                worker_class=WorkerClass.NETWORK,
                status=TaskStatus.QUEUED,
                max_attempts=5,
                run_id=active.id,
            )
        )
        session.commit()
        orphan_id, active_id = orphan.id, active.id
        live_download_id = live_taskless_download.id
        old_download_id = old_taskless_download.id
    service = SchedulerService(engine, session_factory, "UTC", clock=lambda: now)

    assert set(service.recover_orphan_runs()) == {orphan_id, old_download_id}

    with session_factory() as session:
        recovered = session.get(Run, orphan_id)
        still_active = session.get(Run, active_id)
        live_download = session.get(Run, live_download_id)
        old_download = session.get(Run, old_download_id)
        assert recovered is not None and recovered.status == RunStatus.FAILED
        assert recovered.stats_json == {"recovered_orphan": True}
        assert still_active is not None and still_active.status == RunStatus.RUNNING
        assert live_download is not None and live_download.status == RunStatus.RUNNING
        assert old_download is not None and old_download.status == RunStatus.FAILED


def test_periodic_reconcile_does_not_recover_a_live_taskless_run(
    engine, session_factory
) -> None:
    playlist_id = _playlist(session_factory)
    now = datetime(2026, 8, 14, 0, 0, tzinfo=UTC)
    service = SchedulerService(engine, session_factory, "UTC", clock=lambda: now)
    service.start(paused=True)
    try:
        with session_factory() as session:
            live = Run(
                trigger=RunTrigger.MANUAL,
                kind="download",
                playlist_id=playlist_id,
                status=RunStatus.RUNNING,
                started_at=now - timedelta(minutes=10),
            )
            session.add(live)
            session.commit()
            live_id = live.id

        service.reconcile()

        with session_factory() as session:
            unchanged = session.get(Run, live_id)
            assert unchanged is not None
            assert unchanged.status == RunStatus.RUNNING
    finally:
        service.shutdown()


def test_reconcile_preserves_overdue_job_when_configuration_is_unchanged(
    engine, session_factory
) -> None:
    playlist_id = _playlist(session_factory)
    with session_factory() as session:
        core_settings.set_override(session, "schedule.jitter_minutes", 0)
    service = SchedulerService(engine, session_factory, "UTC")
    service.start(paused=True)
    overdue = datetime.now(UTC) - timedelta(hours=2)
    job_id = playlist_job_id(playlist_id, "discover")
    try:
        service._scheduler.modify_job(job_id, next_run_time=overdue)  # noqa: SLF001
        service.reconcile()
        job = service._scheduler.get_job(job_id)  # noqa: SLF001
        assert job is not None
        assert job.next_run_time == overdue
    finally:
        service.shutdown()


def test_misfire_coalesces_multiple_due_times_to_one_run(engine, session_factory) -> None:
    playlist_id = _playlist(session_factory, discover_cron="* * * * *")
    with session_factory() as session:
        core_settings.set_override(session, "schedule.jitter_minutes", 0)
        enqueue_discover_run(session, playlist_id)
    service = SchedulerService(engine, session_factory, "UTC")
    service.start(paused=True)
    job_id = playlist_job_id(playlist_id, "discover")
    try:
        service._scheduler.modify_job(  # noqa: SLF001 - deterministic misfire injection
            job_id,
            next_run_time=datetime.now(UTC) - timedelta(minutes=3),
        )
        service.reconcile()
        service._scheduler.resume()  # noqa: SLF001
        deadline = time.monotonic() + 3
        count = 0
        while time.monotonic() < deadline:
            with session_factory() as session:
                count = len(
                    list(session.scalars(select(Run).where(Run.trigger == RunTrigger.SCHEDULE)))
                )
            if count:
                break
            time.sleep(0.05)
        assert count == 1
    finally:
        service.shutdown()
