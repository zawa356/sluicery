"""Playlist の一覧だけを取得し、Item 差分を反映する discover Task。"""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from sluicery.core.options import build_discover_args
from sluicery.core.sync import SyncStats, apply_discovery, parse_discover_entries
from sluicery.db.models import Playlist, RunStatus
from sluicery.db.repositories.run import RunRepository
from sluicery.downloader.errors import Classification
from sluicery.downloader.ytdlp import TimeoutPolicy, YtdlpRunner
from sluicery.tasks.handlers.dummy import ProgressCallback
from sluicery.tasks.queue import TaskOutcome, TaskResult


class DiscoverHandler:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        runner: YtdlpRunner,
        env_allow_exec: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._runner = runner
        self._env_allow_exec = env_allow_exec

    def cancel(self) -> None:
        self._runner.cancel()

    def run(self, payload: dict, on_progress: ProgressCallback) -> TaskResult:
        playlist_id = payload.get("playlist_id")
        dry_run = payload.get("dry_run", False)
        run_id = _run_id(payload)
        if not isinstance(playlist_id, int) or not isinstance(dry_run, bool) or run_id is None:
            return self._failed(run_id, "discover payloadが不正です")

        with self._session_factory() as session:
            playlist = session.get(Playlist, playlist_id)
            if playlist is None:
                return self._unavailable(run_id, f"Playlist {playlist_id} が見つかりません")
            built = build_discover_args(
                playlist,
                session=session,
                env_allow_exec=self._env_allow_exec,
            )
            source_url = playlist.url

        on_progress({"status": "discovering", "percent": None})
        result = self._runner.run(
            list(built.args),
            timeout=TimeoutPolicy(
                idle_sec=built.timeout.idle_sec,
                absolute_sec=built.timeout.absolute_sec,
                term_grace_sec=built.timeout.term_grace_sec,
            ),
            sensitive_values=(source_url,),
        )
        if result.terminated_by == "cancel":
            self._finish_run(run_id, RunStatus.CANCELLED, SyncStats())
            return TaskResult(TaskOutcome.CANCELLED)
        if result.classification != Classification.OK:
            message = result.stderr_tail[-4000:] or result.classification.value
            if result.classification == Classification.BLOCKED:
                return TaskResult(
                    TaskOutcome.BLOCKED,
                    message,
                    reason_code=result.reason_code,
                )
            if result.classification == Classification.UNAVAILABLE:
                return self._unavailable(run_id, message)
            return self._failed(run_id, message)

        entries = parse_discover_entries(result.stdout_lines)
        with self._session_factory() as session:
            stats = apply_discovery(session, playlist_id, entries, dry_run=dry_run)
            if not RunRepository(session).finish(
                run_id,
                RunStatus.SUCCEEDED,
                stats.to_dict(),
            ):
                return TaskResult(TaskOutcome.FAILED, f"Run {run_id} が見つかりません")
        on_progress(
            {
                "status": "empty" if stats.empty_result else "discovered",
                "percent": 100.0,
                "new_items": stats.new_items,
                "delisted_items": stats.delisted_items,
            }
        )
        return TaskResult(TaskOutcome.SUCCEEDED, payload_update={"stats": stats.to_dict()})

    def _failed(self, run_id: int | None, message: str) -> TaskResult:
        self._finish_run(run_id, RunStatus.FAILED, SyncStats(empty_result=True))
        return TaskResult(TaskOutcome.FAILED, message[-4000:])

    def _unavailable(self, run_id: int | None, message: str) -> TaskResult:
        self._finish_run(run_id, RunStatus.FAILED, SyncStats(empty_result=True))
        return TaskResult(TaskOutcome.UNAVAILABLE, message[-4000:])

    def _finish_run(self, run_id: int | None, status: RunStatus, stats: SyncStats) -> None:
        if run_id is None:
            return
        with self._session_factory() as session:
            RunRepository(session).finish(run_id, status, stats.to_dict())


def _run_id(payload: dict) -> int | None:
    execution = payload.get("_execution")
    if isinstance(execution, dict) and isinstance(execution.get("run_id"), int):
        return execution["run_id"]
    value = payload.get("run_id")
    return value if isinstance(value, int) else None


__all__ = ["DiscoverHandler"]
