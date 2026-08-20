"""app専用のPlaylistスケジューラ。"""

from __future__ import annotations

import logging
import random
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.job import Job  # type: ignore[import-untyped]
from apscheduler.jobstores.sqlalchemy import (  # type: ignore[import-untyped]
    SQLAlchemyJobStore,
)
from apscheduler.schedulers.background import (  # type: ignore[import-untyped]
    BackgroundScheduler,
)
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from sluicery.core.integrity import check_integrity
from sluicery.core.settings import OperationalSettings
from sluicery.core.sync import (
    SyncAlreadyRunningError,
    SyncStats,
    enqueue_discover_run,
    execute_download_run,
)
from sluicery.db.models import Playlist, Run, RunStatus, RunTrigger, Storage, Task, TaskStatus
from sluicery.db.repositories.playlist import PlaylistRepository
from sluicery.db.repositories.run import RunRepository
from sluicery.hooks import EventLogHook, Hook, emit_safely
from sluicery.storage import create_storage_adapter

logger = logging.getLogger(__name__)

_JOB_PREFIX = "sluicery:playlist:"
_RECONCILE_JOB_ID = "sluicery:maintenance:reconcile"
_INTEGRITY_JOB_ID = "sluicery:maintenance:integrity"
_YTDLP_UPDATE_JOB_ID = "sluicery:maintenance:ytdlp-update"
_MISFIRE_GRACE_SEC = 24 * 60 * 60
_TASKLESS_DOWNLOAD_RECOVERY_SEC = 24 * 60 * 60
_active_service: SchedulerService | None = None
_active_service_lock = threading.Lock()
_WINDOW_RE = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$")


@dataclass(frozen=True)
class DownloadWindow:
    start_minute: int
    end_minute: int

    def contains(self, moment: datetime) -> bool:
        current = moment.hour * 60 + moment.minute
        if self.start_minute == self.end_minute:
            return True
        if self.start_minute < self.end_minute:
            return self.start_minute <= current < self.end_minute
        return current >= self.start_minute or current < self.end_minute


def parse_download_window(value: str | None) -> DownloadWindow | None:
    if value is None or not value.strip():
        return None
    match = _WINDOW_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError("download windowはHH:MM-HH:MM形式で指定してください")
    start_hour, start_minute, end_hour, end_minute = (int(part) for part in match.groups())
    if start_hour > 23 or end_hour > 23 or start_minute > 59 or end_minute > 59:
        raise ValueError("download windowの時刻が範囲外です")
    return DownloadWindow(
        start_minute=start_hour * 60 + start_minute,
        end_minute=end_hour * 60 + end_minute,
    )


class SymmetricCronTrigger(CronTrigger):
    """jobごとのランダムな位相でcron全体を±同幅へずらす。"""

    __slots__ = ("algorithm_version", "expression", "jitter_seconds", "offset_seconds")

    @classmethod
    def from_expression(
        cls,
        expression: str,
        *,
        timezone: ZoneInfo,
        jitter_minutes: int,
    ) -> SymmetricCronTrigger:
        trigger = cls.from_crontab(expression, timezone=timezone)
        trigger.algorithm_version = 2
        trigger.expression = expression
        trigger.jitter_seconds = jitter_minutes * 60
        trigger.offset_seconds = (
            random.uniform(-trigger.jitter_seconds, trigger.jitter_seconds)
            if trigger.jitter_seconds
            else 0.0
        )
        return trigger

    def get_next_fire_time(
        self,
        previous_fire_time: datetime | None,
        now: datetime,
    ) -> datetime | None:
        """前回の実時刻をcron基準時刻へ戻し、同じ基準時刻の再発火を防ぐ。"""
        offset = timedelta(seconds=self.offset_seconds)
        base_previous = (
            (previous_fire_time.astimezone(UTC) - offset).astimezone(self.timezone)
            if previous_fire_time is not None
            else None
        )
        base_now = (now.astimezone(UTC) - offset).astimezone(self.timezone)
        base_fire_time = super().get_next_fire_time(base_previous, base_now)
        if base_fire_time is None:
            return None
        fire_time = (base_fire_time.astimezone(UTC) + offset).astimezone(self.timezone)
        if self.end_date is not None and fire_time > self.end_date:
            return None
        return fire_time

    def __getstate__(self) -> dict[str, Any]:
        state = super().__getstate__()
        state["sluicery_expression"] = self.expression
        state["sluicery_jitter_seconds"] = self.jitter_seconds
        state["sluicery_offset_seconds"] = self.offset_seconds
        state["sluicery_algorithm_version"] = self.algorithm_version
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        copied = dict(state)
        self.expression = str(copied.pop("sluicery_expression"))
        legacy_jitter = int(copied.get("jitter") or 0)
        self.jitter_seconds = int(copied.pop("sluicery_jitter_seconds", legacy_jitter))
        self.offset_seconds = float(copied.pop("sluicery_offset_seconds", 0.0))
        self.algorithm_version = int(copied.pop("sluicery_algorithm_version", 1))
        # v1はCronTrigger._apply_jitterを上書きしていた。v2は基準時刻と実時刻を
        # 相互変換するため、親クラスの正方向jitterは常に無効化する。
        copied["jitter"] = None
        super().__setstate__(copied)


def validate_cron_expression(expression: str, timezone_name: str) -> None:
    SymmetricCronTrigger.from_expression(
        expression,
        timezone=ZoneInfo(timezone_name),
        jitter_minutes=0,
    )


def playlist_job_id(playlist_id: int, kind: str) -> str:
    if kind not in {"discover", "download"}:
        raise ValueError(f"未対応のスケジュール種別です: {kind}")
    return f"{_JOB_PREFIX}{playlist_id}:{kind}"


@dataclass(frozen=True)
class NextRun:
    playlist_id: int
    kind: str
    scheduled_at: datetime
    display: str


def _dispatch_scheduled_job(playlist_id: int, kind: str) -> None:
    service = _active_service
    if service is None:
        logger.error(
            "Scheduled job fired without an active app scheduler",
            extra={"playlist_id": playlist_id, "kind": kind},
        )
        return
    service.execute_job(playlist_id, kind)


def _reconcile_active_scheduler() -> None:
    service = _active_service
    if service is None:
        return
    try:
        service.reconcile()
    except Exception:  # noqa: BLE001 - 定期整合ジョブを一時的DB競合で失わない
        logger.warning("Periodic scheduler reconciliation failed", exc_info=True)


def _dispatch_integrity_job() -> None:
    service = _active_service
    if service is None:
        logger.error("Integrity job fired without an active app scheduler")
        return
    service.execute_integrity_job()


def _dispatch_ytdlp_update_job() -> None:
    service = _active_service
    if service is None:
        logger.error("yt-dlp update job fired without an active app scheduler")
        return
    service.execute_ytdlp_update_job()


class SchedulerService:
    """SQLAlchemyJobStoreを使い、appプロセス内だけで動くscheduler境界。"""

    def __init__(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        timezone_name: str,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        ytdlp_update_callback: Callable[[], object] | None = None,
        hook: Hook | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.timezone = ZoneInfo(timezone_name)
        self._clock = clock
        self._ytdlp_update_callback = ytdlp_update_callback
        self._hook = hook or EventLogHook(session_factory)
        self._scheduler = BackgroundScheduler(
            timezone=self.timezone,
            jobstores={"default": SQLAlchemyJobStore(engine=engine)},
            job_defaults={"coalesce": True, "max_instances": 1},
        )
        self._started = False

    def start(self, *, paused: bool = False) -> None:
        global _active_service
        with _active_service_lock:
            if _active_service is not None and _active_service is not self:
                raise RuntimeError("同じプロセスで複数のスケジューラは起動できません")
            _active_service = self
        try:
            self._scheduler.start(paused=True)
            self._started = True
            self._scheduler.add_job(
                _reconcile_active_scheduler,
                "interval",
                seconds=60,
                id=_RECONCILE_JOB_ID,
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=60,
            )
            # appがまだリクエストもscheduled jobも受けないpaused状態でだけ回収する。
            # 定期整合から実行すると、Task作成前の長いStorage事前確認中Runを
            # orphanと誤認してPlaylist排他を外し得る。
            self.recover_orphan_runs()
            self.reconcile()
            if not paused:
                self._scheduler.resume()
        except Exception:
            with _active_service_lock:
                if _active_service is self:
                    _active_service = None
            if self._started:
                self._scheduler.shutdown(wait=False)
                self._started = False
            raise
        logger.info("Playlist scheduler started", extra={"timezone": str(self.timezone)})

    def shutdown(self) -> None:
        global _active_service
        if self._started:
            self._scheduler.shutdown(wait=True)
            self._started = False
        with _active_service_lock:
            if _active_service is self:
                _active_service = None
        logger.info("Playlist scheduler stopped")

    def reconcile(self) -> None:
        """DB設定をジョブへ反映し、対象外Playlistのジョブを除去する。"""
        if not self._started:
            return
        with self._session_factory() as session:
            settings = OperationalSettings(session)
            jitter = settings.schedule_jitter_minutes
            if jitter < 0:
                raise ValueError("schedule.jitter_minutesは0以上にしてください")
            cron_defaults = {
                "discover": settings.schedule_discover_cron,
                "download": settings.schedule_download_cron,
            }
            integrity_expression = settings.schedule_integrity_cron
            ytdlp_update_expression = settings.ytdlp_update_cron
            playlists = PlaylistRepository(session).list_runnable()

        desired: set[str] = set()
        for playlist in playlists:
            for kind, default_cron in cron_defaults.items():
                expression = (
                    playlist.discover_cron if kind == "discover" else playlist.download_cron
                ) or default_cron
                job_id = playlist_job_id(playlist.id, kind)
                try:
                    trigger = SymmetricCronTrigger.from_expression(
                        expression,
                        timezone=self.timezone,
                        jitter_minutes=jitter,
                    )
                except ValueError:
                    logger.error(
                        "Invalid playlist cron; job was not registered",
                        extra={"playlist_id": playlist.id, "kind": kind},
                    )
                    continue
                existing = self._scheduler.get_job(job_id)
                if not self._job_matches(existing, trigger, playlist.id, kind):
                    self._scheduler.add_job(
                        _dispatch_scheduled_job,
                        trigger=trigger,
                        id=job_id,
                        args=(playlist.id, kind),
                        replace_existing=True,
                        coalesce=True,
                        max_instances=1,
                        misfire_grace_time=_MISFIRE_GRACE_SEC,
                    )
                desired.add(job_id)

        for job in self._managed_jobs():
            if job.id not in desired:
                self._scheduler.remove_job(job.id)

        try:
            integrity_trigger = SymmetricCronTrigger.from_expression(
                integrity_expression,
                timezone=self.timezone,
                jitter_minutes=jitter,
            )
        except ValueError:
            if self._scheduler.get_job(_INTEGRITY_JOB_ID) is not None:
                self._scheduler.remove_job(_INTEGRITY_JOB_ID)
            logger.error("Invalid integrity cron; job was not registered")
        else:
            existing = self._scheduler.get_job(_INTEGRITY_JOB_ID)
            if not self._integrity_job_matches(existing, integrity_trigger):
                self._scheduler.add_job(
                    _dispatch_integrity_job,
                    trigger=integrity_trigger,
                    id=_INTEGRITY_JOB_ID,
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=_MISFIRE_GRACE_SEC,
                )

        if not ytdlp_update_expression.strip():
            if self._scheduler.get_job(_YTDLP_UPDATE_JOB_ID) is not None:
                self._scheduler.remove_job(_YTDLP_UPDATE_JOB_ID)
        else:
            try:
                update_trigger = SymmetricCronTrigger.from_expression(
                    ytdlp_update_expression,
                    timezone=self.timezone,
                    jitter_minutes=jitter,
                )
            except ValueError:
                if self._scheduler.get_job(_YTDLP_UPDATE_JOB_ID) is not None:
                    self._scheduler.remove_job(_YTDLP_UPDATE_JOB_ID)
                logger.error("Invalid yt-dlp update cron; job was not registered")
            else:
                existing = self._scheduler.get_job(_YTDLP_UPDATE_JOB_ID)
                if not self._maintenance_job_matches(existing, update_trigger):
                    self._scheduler.add_job(
                        _dispatch_ytdlp_update_job,
                        trigger=update_trigger,
                        id=_YTDLP_UPDATE_JOB_ID,
                        replace_existing=True,
                        coalesce=True,
                        max_instances=1,
                        misfire_grace_time=_MISFIRE_GRACE_SEC,
                    )

    def recover_orphan_runs(self) -> list[int]:
        """未完了Taskのないrunning Runを終端し、異常終了の残骸を残さない。"""
        active_statuses = {
            TaskStatus.PENDING,
            TaskStatus.QUEUED,
            TaskStatus.RUNNING,
            TaskStatus.BLOCKED,
        }
        recovered: list[int] = []
        recovered_events: list[tuple[str, dict[str, object]]] = []
        with self._session_factory() as session:
            settings = OperationalSettings(session)
            now = self._clock().astimezone(UTC)
            stale_before = now - timedelta(
                seconds=settings.worker_stale_threshold_sec
            )
            taskless_download_before = now - timedelta(
                seconds=_TASKLESS_DOWNLOAD_RECOVERY_SEC
            )
            running_runs = list(
                session.scalars(
                    select(Run).where(
                        Run.status == RunStatus.RUNNING,
                        Run.started_at < stale_before,
                    )
                )
            )
            for run in running_runs:
                tasks = list(session.scalars(select(Task).where(Task.run_id == run.id)))
                if any(task.status in active_statuses for task in tasks):
                    continue
                # downloadはStorage事前確認中に正当にTask 0件となる。appと別のCLIが
                # 生存している可能性を優先し、短いworker stale閾値では回収しない。
                if (
                    not tasks
                    and run.kind == "download"
                    and run.started_at >= taskless_download_before
                ):
                    continue
                stats = dict(run.stats_json or {})
                stats["recovered_orphan"] = True
                if tasks and all(task.status == TaskStatus.SUCCEEDED for task in tasks):
                    status = RunStatus.SUCCEEDED
                    task_stats = (tasks[-1].payload_json or {}).get("stats")
                    if isinstance(task_stats, dict):
                        stats.update(task_stats)
                elif tasks and any(task.status == TaskStatus.CANCELLED for task in tasks):
                    status = RunStatus.CANCELLED
                else:
                    status = RunStatus.FAILED
                RunRepository(session).finish(run.id, status, stats, commit=False)
                recovered.append(run.id)
                recovered_events.append(
                    (
                        "run_failed" if status == RunStatus.FAILED else "run_finished",
                        {
                            "run_id": run.id,
                            "playlist_id": run.playlist_id,
                            "kind": run.kind,
                            "status": status.value,
                            "reason_code": "orphan_run_recovered"
                            if status == RunStatus.FAILED
                            else None,
                        },
                    )
                )
            if recovered:
                session.commit()
        for event_type, payload in recovered_events:
            emit_safely(self._hook, event_type, payload)
        if recovered:
            logger.warning("Recovered orphan running runs", extra={"run_ids": recovered})
        return recovered

    def execute_job(self, playlist_id: int, kind: str) -> None:
        """永続ジョブの実行入口。秘密値をjob argsへ保存しない。"""
        try:
            with self._session_factory() as session:
                playlist = session.get(Playlist, playlist_id)
                if playlist is None or not playlist.enabled or playlist.paused:
                    return
                if kind == "download":
                    window = parse_download_window(
                        OperationalSettings(session).schedule_download_window
                    )
                else:
                    window = None
            if kind == "download":
                local_now = self._clock().astimezone(self.timezone)
                if window is not None and not window.contains(local_now):
                    self._record_skipped(playlist_id, kind, "outside_download_window")
                    logger.info(
                        "Scheduled download skipped outside the download window",
                        extra={"playlist_id": playlist_id},
                    )
                    return
            with self._session_factory() as session:
                if kind == "discover":
                    enqueue_discover_run(
                        session,
                        playlist_id,
                        trigger=RunTrigger.SCHEDULE,
                        hook=self._hook,
                    )
                elif kind == "download":
                    execute_download_run(
                        session,
                        playlist_id,
                        trigger=RunTrigger.SCHEDULE,
                        hook=self._hook,
                    )
                else:
                    raise ValueError(f"未対応のスケジュール種別です: {kind}")
        except SyncAlreadyRunningError:
            self._record_skipped(playlist_id, kind, "active_sync")
            logger.info(
                "Scheduled sync skipped because the playlist is active",
                extra={"playlist_id": playlist_id, "kind": kind},
            )
        except (LookupError, ValueError):
            logger.warning(
                "Scheduled sync could not start",
                exc_info=True,
                extra={"playlist_id": playlist_id, "kind": kind},
            )

    def execute_integrity_job(self) -> None:
        """全Artifactを定期確認する。永続job引数には秘密値もパスも持たせない。"""
        try:
            with self._session_factory() as session:
                operational = OperationalSettings(session)

                def adapter_factory(storage: Storage):
                    return create_storage_adapter(storage, operational)

                report = check_integrity(
                    session,
                    adapter_factory,
                    rescan_timeout_sec=operational.integrity_rescan_timeout_sec,
                    max_candidates_per_source_id=(
                        operational.integrity_max_candidates_per_source_id
                    ),
                    hook=self._hook,
                )
            error_count = sum(
                issue.kind in {"storage_error", "scan_error", "scan_timeout"}
                for issue in report.issues
            )
            logger.info(
                "Scheduled integrity check completed",
                extra={
                    "checked": report.checked,
                    "relinked": report.relinked,
                    "missing": report.missing,
                    "errors": error_count,
                },
            )
        except Exception:  # noqa: BLE001 - 保守job失敗でapp schedulerを停めない
            logger.warning("Scheduled integrity check failed", exc_info=True)

    def execute_ytdlp_update_job(self) -> None:
        """週次更新をapp内だけで実行する。永続jobには引数を持たせない。"""
        if self._ytdlp_update_callback is None:
            logger.warning("Scheduled yt-dlp update is not configured")
            return
        try:
            result = self._ytdlp_update_callback()
            logger.info(
                "Scheduled yt-dlp update completed",
                extra={"status": getattr(result, "status", "unknown")},
            )
        except Exception:  # noqa: BLE001 - 更新失敗でschedulerを停めない
            logger.warning("Scheduled yt-dlp update failed", exc_info=True)

    def next_runs(self, playlist_id: int | None = None) -> list[NextRun]:
        rows: list[NextRun] = []
        for job in self._managed_jobs():
            if job.next_run_time is None:
                continue
            selected_id = int(job.args[0])
            kind = str(job.args[1])
            if playlist_id is not None and selected_id != playlist_id:
                continue
            scheduled = job.next_run_time.astimezone(self.timezone)
            rows.append(
                NextRun(
                    playlist_id=selected_id,
                    kind=kind,
                    scheduled_at=scheduled,
                    display=scheduled.strftime("%Y-%m-%d %H:%M:%S %Z"),
                )
            )
        return sorted(rows, key=lambda row: (row.scheduled_at, row.playlist_id, row.kind))

    def _managed_jobs(self) -> list[Job]:
        return [job for job in self._scheduler.get_jobs() if job.id.startswith(_JOB_PREFIX)]

    def _job_matches(
        self,
        job: Job | None,
        trigger: SymmetricCronTrigger,
        playlist_id: int,
        kind: str,
    ) -> bool:
        if job is None or not isinstance(job.trigger, SymmetricCronTrigger):
            return False
        existing_trigger = job.trigger
        return (
            existing_trigger.algorithm_version == trigger.algorithm_version
            and existing_trigger.expression == trigger.expression
            and str(existing_trigger.timezone) == str(trigger.timezone)
            and existing_trigger.jitter_seconds == trigger.jitter_seconds
            and tuple(job.args) == (playlist_id, kind)
            and job.coalesce is True
            and job.max_instances == 1
            and job.misfire_grace_time == _MISFIRE_GRACE_SEC
        )

    def _integrity_job_matches(
        self,
        job: Job | None,
        trigger: SymmetricCronTrigger,
    ) -> bool:
        if job is None or not isinstance(job.trigger, SymmetricCronTrigger):
            return False
        existing_trigger = job.trigger
        return (
            existing_trigger.algorithm_version == trigger.algorithm_version
            and existing_trigger.expression == trigger.expression
            and str(existing_trigger.timezone) == str(trigger.timezone)
            and existing_trigger.jitter_seconds == trigger.jitter_seconds
            and tuple(job.args) == ()
            and job.coalesce is True
            and job.max_instances == 1
            and job.misfire_grace_time == _MISFIRE_GRACE_SEC
        )

    def _maintenance_job_matches(
        self,
        job: Job | None,
        trigger: SymmetricCronTrigger,
    ) -> bool:
        if job is None or not isinstance(job.trigger, SymmetricCronTrigger):
            return False
        existing_trigger = job.trigger
        return (
            existing_trigger.algorithm_version == trigger.algorithm_version
            and existing_trigger.expression == trigger.expression
            and str(existing_trigger.timezone) == str(trigger.timezone)
            and existing_trigger.jitter_seconds == trigger.jitter_seconds
            and tuple(job.args) == ()
            and job.coalesce is True
            and job.max_instances == 1
            and job.misfire_grace_time == _MISFIRE_GRACE_SEC
        )

    def _record_skipped(self, playlist_id: int, kind: str, reason: str) -> None:
        with self._session_factory() as session:
            playlist = session.get(Playlist, playlist_id)
            if playlist is None or not playlist.enabled or playlist.paused:
                return
            run = RunRepository(session).start(
                trigger=RunTrigger.SCHEDULE,
                kind=kind,
                playlist_id=playlist_id,
                commit=False,
            )
            stats: dict[str, Any] = SyncStats().to_dict()
            stats.update({"skipped": True, "skip_reason": reason})
            RunRepository(session).finish(
                run.id,
                RunStatus.SKIPPED,
                stats,
                now=datetime.now(UTC),
            )
            payload = {
                "run_id": run.id,
                "playlist_id": playlist_id,
                "kind": kind,
            }
        emit_safely(
            self._hook,
            "run_started",
            {**payload, "trigger": RunTrigger.SCHEDULE.value},
        )
        emit_safely(
            self._hook,
            "run_finished",
            {**payload, "status": RunStatus.SKIPPED.value},
        )


__all__ = [
    "DownloadWindow",
    "NextRun",
    "SchedulerService",
    "SymmetricCronTrigger",
    "playlist_job_id",
    "parse_download_window",
    "validate_cron_expression",
]
