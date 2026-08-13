"""app専用のPlaylistスケジューラ。"""

from __future__ import annotations

import logging
import random
import threading
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
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from sluicery.core.settings import OperationalSettings
from sluicery.core.sync import (
    SyncAlreadyRunningError,
    SyncStats,
    enqueue_discover_run,
    execute_download_run,
)
from sluicery.db.models import Playlist, RunStatus, RunTrigger
from sluicery.db.repositories.playlist import PlaylistRepository
from sluicery.db.repositories.run import RunRepository

logger = logging.getLogger(__name__)

_JOB_PREFIX = "sluicery:playlist:"
_active_service: SchedulerService | None = None
_active_service_lock = threading.Lock()


class SymmetricCronTrigger(CronTrigger):
    """APSchedulerの正方向だけのjitterを、±同幅へ置き換える。"""

    __slots__ = ("expression",)

    @classmethod
    def from_expression(
        cls,
        expression: str,
        *,
        timezone: ZoneInfo,
        jitter_minutes: int,
    ) -> SymmetricCronTrigger:
        trigger = cls.from_crontab(expression, timezone=timezone)
        trigger.expression = expression
        trigger.jitter = jitter_minutes * 60
        return trigger

    def _apply_jitter(
        self,
        next_fire_time: datetime | None,
        jitter: int | None,
        now: datetime,
    ) -> datetime | None:
        if next_fire_time is None or not jitter:
            return next_fire_time
        return next_fire_time + timedelta(seconds=random.uniform(-jitter, jitter))

    def __getstate__(self) -> dict[str, Any]:
        state = super().__getstate__()
        state["sluicery_expression"] = self.expression
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        copied = dict(state)
        self.expression = str(copied.pop("sluicery_expression"))
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


class SchedulerService:
    """SQLAlchemyJobStoreを使い、appプロセス内だけで動くscheduler境界。"""

    def __init__(
        self,
        engine: Engine,
        session_factory: sessionmaker[Session],
        timezone_name: str,
    ) -> None:
        self._session_factory = session_factory
        self.timezone = ZoneInfo(timezone_name)
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
            self._scheduler.shutdown(wait=False)
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
                self._scheduler.add_job(
                    _dispatch_scheduled_job,
                    trigger=trigger,
                    id=job_id,
                    args=(playlist.id, kind),
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=24 * 60 * 60,
                )
                desired.add(job_id)

        for job in self._managed_jobs():
            if job.id not in desired:
                self._scheduler.remove_job(job.id)

    def execute_job(self, playlist_id: int, kind: str) -> None:
        """永続ジョブの実行入口。秘密値をjob argsへ保存しない。"""
        try:
            with self._session_factory() as session:
                if kind == "discover":
                    enqueue_discover_run(
                        session,
                        playlist_id,
                        trigger=RunTrigger.SCHEDULE,
                    )
                elif kind == "download":
                    execute_download_run(
                        session,
                        playlist_id,
                        trigger=RunTrigger.SCHEDULE,
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

    def _record_skipped(self, playlist_id: int, kind: str, reason: str) -> None:
        with self._session_factory() as session:
            playlist = session.get(Playlist, playlist_id)
            if playlist is None:
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


__all__ = [
    "NextRun",
    "SchedulerService",
    "SymmetricCronTrigger",
    "playlist_job_id",
    "validate_cron_expression",
]
