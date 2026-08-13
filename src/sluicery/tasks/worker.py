"""DB-backed Taskワーカー、graceful shutdown、heartbeat、stale回収。"""

from __future__ import annotations

import logging
import os
import random
import signal
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from sluicery.core.settings import OperationalSettings
from sluicery.core.target_state import sync_target_after_task
from sluicery.db.models import Task, TaskStatus, WorkerClass
from sluicery.db.repositories.task import TaskRepository
from sluicery.tasks.handlers import DUMMY_HANDLER_FACTORIES, TaskHandler
from sluicery.tasks.progress import ProgressWriter
from sluicery.tasks.queue import TaskOutcome, TaskResult, retry_delay_sec

logger = logging.getLogger(__name__)
HandlerFactory = Callable[[], TaskHandler]


@dataclass(frozen=True)
class WorkerConfig:
    poll_interval_sec: float
    poll_jitter_sec: float
    heartbeat_interval_sec: float
    stale_threshold_sec: float
    retry_base_sec: float
    retry_max_sec: float
    max_attempts: int
    blocked_retry_sec: float
    blocked_retry_403_sec: float
    progress_write_interval_sec: float
    progress_write_percent_step: float
    shutdown_grace_sec: float
    enable_test_tasks: bool

    def __post_init__(self) -> None:
        positive = {
            "poll_interval_sec": self.poll_interval_sec,
            "heartbeat_interval_sec": self.heartbeat_interval_sec,
            "stale_threshold_sec": self.stale_threshold_sec,
            "retry_base_sec": self.retry_base_sec,
            "retry_max_sec": self.retry_max_sec,
            "blocked_retry_sec": self.blocked_retry_sec,
            "blocked_retry_403_sec": self.blocked_retry_403_sec,
            "progress_write_interval_sec": self.progress_write_interval_sec,
            "progress_write_percent_step": self.progress_write_percent_step,
            "shutdown_grace_sec": self.shutdown_grace_sec,
        }
        invalid = [key for key, value in positive.items() if value <= 0]
        if self.poll_jitter_sec < 0:
            invalid.append("poll_jitter_sec")
        if self.max_attempts < 1:
            invalid.append("max_attempts")
        if self.stale_threshold_sec < self.heartbeat_interval_sec * 3:
            invalid.append("stale_threshold_sec")
        if invalid:
            raise ValueError(f"worker設定が不正です: {', '.join(invalid)}")

    @classmethod
    def from_session(cls, session: Session) -> WorkerConfig:
        settings = OperationalSettings(session)
        return cls(
            poll_interval_sec=settings.worker_poll_interval_sec,
            poll_jitter_sec=settings.worker_poll_jitter_sec,
            heartbeat_interval_sec=settings.worker_heartbeat_interval_sec,
            stale_threshold_sec=settings.worker_stale_threshold_sec,
            retry_base_sec=settings.worker_retry_base_sec,
            retry_max_sec=settings.worker_retry_max_sec,
            max_attempts=settings.worker_max_attempts,
            blocked_retry_sec=settings.worker_blocked_retry_sec,
            blocked_retry_403_sec=settings.worker_blocked_retry_403_sec,
            progress_write_interval_sec=settings.worker_progress_write_interval_sec,
            progress_write_percent_step=settings.worker_progress_write_percent_step,
            shutdown_grace_sec=settings.worker_shutdown_grace_sec,
            enable_test_tasks=settings.worker_enable_test_tasks,
        )


def make_worker_id(worker_class: WorkerClass) -> str:
    host = socket.gethostname().split(".", 1)[0][:12]
    invocation = uuid4().hex[:8]
    return f"worker-{worker_class.value}:{host}:{os.getpid()}:{invocation}"


class Worker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        worker_class: WorkerClass,
        config: WorkerConfig,
        *,
        worker_id: str | None = None,
        handler_factories: Mapping[str, HandlerFactory] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        random_fraction: Callable[[], float] = random.random,
    ) -> None:
        self._session_factory = session_factory
        self.worker_class = worker_class
        self.config = config
        self.worker_id = worker_id or make_worker_id(worker_class)
        if handler_factories is None:
            handler_factories = DUMMY_HANDLER_FACTORIES if config.enable_test_tasks else {}
        self._handler_factories = dict(handler_factories)
        self._clock = clock
        self._random_fraction = random_fraction
        self._shutdown = threading.Event()
        self._handler_lock = threading.Lock()
        self._current_handler: TaskHandler | None = None

    def request_shutdown(self) -> None:
        self._shutdown.set()
        with self._handler_lock:
            handler = self._current_handler
        if handler is not None:
            handler.cancel()

    def run(self, *, install_signal_handlers: bool = True) -> None:
        previous_handlers: dict[signal.Signals, Any] = {}
        if install_signal_handlers:
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, lambda _signum, _frame: self.request_shutdown())
        logger.info("Task worker started", extra={"worker_id": self.worker_id})
        try:
            while not self._shutdown.is_set():
                claimed = self.run_once()
                if not claimed:
                    delay = self.config.poll_interval_sec + (
                        self.config.poll_jitter_sec * self._random_fraction()
                    )
                    self._shutdown.wait(delay)
        finally:
            self.request_shutdown()
            if install_signal_handlers:
                for signum, previous in previous_handlers.items():
                    signal.signal(signum, previous)
            logger.info("Task worker stopped", extra={"worker_id": self.worker_id})

    def run_once(self) -> bool:
        if self._shutdown.is_set():
            return False
        with self._session_factory() as session:
            task = TaskRepository(session).claim_next(
                self.worker_class,
                worker_id=self.worker_id,
                now=self._clock(),
            )
        if task is None:
            return False
        logger.info(
            "Task claimed",
            extra={"task_id": task.id, "task_type": task.type.value, "worker_id": self.worker_id},
        )
        self._execute(task)
        return True

    def _execute(self, task: Task) -> None:
        factory = self._handler_factories.get(task.type.value)
        if factory is None:
            self._finish_unavailable(task.id, f"Task type {task.type.value} のハンドラが未登録です")
            return
        handler = factory()
        with self._handler_lock:
            self._current_handler = handler

        heartbeat_stop = threading.Event()
        cancel_seen = threading.Event()
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(task.id, handler, heartbeat_stop, cancel_seen),
            daemon=True,
            name=f"heartbeat-task-{task.id}",
        )
        heartbeat_thread.start()

        progress = ProgressWriter(
            lambda value: self._write_progress(task.id, value),
            interval_sec=self.config.progress_write_interval_sec,
            percent_step=self.config.progress_write_percent_step,
        )
        result_holder: list[TaskResult] = []

        def invoke_handler() -> None:
            try:
                execution_payload = dict(task.payload_json or {})
                execution_payload["_execution"] = {
                    "task_id": task.id,
                    "depends_on_task_id": task.depends_on_task_id,
                    "target_ref_id": task.target_ref_id,
                    "run_id": task.run_id,
                }
                result_holder.append(handler.run(execution_payload, progress.emit))
            except Exception as exc:  # noqa: BLE001 - 1件の例外でworker全体を停止させない
                logger.exception("Task handler raised", extra={"task_id": task.id})
                result_holder.append(TaskResult(TaskOutcome.FAILED, str(exc)))

        handler_thread = threading.Thread(
            target=invoke_handler,
            daemon=True,
            name=f"handler-task-{task.id}",
        )
        handler_thread.start()
        shutdown_deadline: float | None = None
        while handler_thread.is_alive():
            handler_thread.join(timeout=0.1)
            if self._shutdown.is_set():
                handler.cancel()
                if shutdown_deadline is None:
                    shutdown_deadline = time.monotonic() + self.config.shutdown_grace_sec
                elif time.monotonic() >= shutdown_deadline:
                    logger.error("Task handler exceeded shutdown grace", extra={"task_id": task.id})
                    break

        heartbeat_stop.set()
        heartbeat_thread.join(timeout=min(2.0, self.config.heartbeat_interval_sec + 0.1))
        with self._handler_lock:
            self._current_handler = None

        if self._shutdown.is_set():
            with self._session_factory() as session:
                TaskRepository(session).release_for_shutdown(
                    task.id, self.worker_id, now=self._clock()
                )
            return

        result = (
            result_holder[0]
            if result_holder
            else TaskResult(TaskOutcome.FAILED, "ハンドラが結果を返しませんでした")
        )
        if cancel_seen.is_set():
            result = TaskResult(TaskOutcome.CANCELLED)
        final_percent = 100.0 if result.outcome == TaskOutcome.SUCCEEDED else None
        progress.emit(
            {"status": result.outcome.value, "percent": final_percent},
            final=True,
        )
        self._apply_result(task, result)

    def _heartbeat_loop(
        self,
        task_id: int,
        handler: TaskHandler,
        stop: threading.Event,
        cancel_seen: threading.Event,
    ) -> None:
        while not stop.wait(self.config.heartbeat_interval_sec):
            try:
                with self._session_factory() as session:
                    cancel_requested = TaskRepository(session).heartbeat(
                        task_id, self.worker_id, now=self._clock()
                    )
            except Exception:  # noqa: BLE001 - 一時的DB競合でheartbeatスレッドを失わない
                logger.warning(
                    "Task heartbeat failed",
                    exc_info=True,
                    extra={"task_id": task_id, "worker_id": self.worker_id},
                )
                continue
            if cancel_requested:
                cancel_seen.set()
                handler.cancel()
                return
            if cancel_requested is None:
                return

    def _apply_result(self, task: Task, result: TaskResult) -> None:
        with self._session_factory() as session:
            repo = TaskRepository(session)
            final_status: TaskStatus | None = None
            if result.payload_update:
                repo.write_result_payload(task.id, self.worker_id, result.payload_update)
            if result.outcome == TaskOutcome.SUCCEEDED:
                repo.mark_succeeded(task.id, self.worker_id, now=self._clock())
            elif result.outcome == TaskOutcome.CANCELLED:
                if repo.mark_cancelled(task.id, self.worker_id, now=self._clock()):
                    final_status = TaskStatus.CANCELLED
            elif result.outcome == TaskOutcome.UNAVAILABLE:
                if repo.mark_unavailable(
                    task.id,
                    self.worker_id,
                    error_message=result.message,
                    now=self._clock(),
                ):
                    final_status = TaskStatus.UNAVAILABLE
            elif result.outcome == TaskOutcome.BLOCKED:
                retry_after_sec = (
                    self.config.blocked_retry_403_sec
                    if result.reason_code in {"http_403", "bot_check"}
                    else self.config.blocked_retry_sec
                )
                if repo.mark_blocked(
                    task.id,
                    self.worker_id,
                    retry_after_sec=retry_after_sec,
                    reason=result.message,
                    now=self._clock(),
                ):
                    settled = session.get(Task, task.id)
                    final_status = settled.status if settled is not None else None
            else:
                attempt = task.attempts + 1
                delay = retry_delay_sec(
                    attempt,
                    base_sec=self.config.retry_base_sec,
                    max_sec=self.config.retry_max_sec,
                    random_fraction=self._random_fraction(),
                )
                final_status = repo.mark_failed_for_retry(
                    task.id,
                    self.worker_id,
                    retry_delay_sec=delay,
                    error_message=result.message,
                    now=self._clock(),
                )
            if final_status is not None:
                sync_target_after_task(
                    session,
                    task,
                    final_status,
                    error=result.message,
                    failed_attempt=result.outcome == TaskOutcome.FAILED,
                )

    def _finish_unavailable(self, task_id: int, message: str) -> None:
        with self._session_factory() as session:
            task = session.get(Task, task_id)
            if task is not None and TaskRepository(session).mark_unavailable(
                task_id, self.worker_id, error_message=message, now=self._clock()
            ):
                sync_target_after_task(
                    session, task, TaskStatus.UNAVAILABLE, error=message
                )

    def _write_progress(self, task_id: int, progress: dict[str, Any]) -> None:
        with self._session_factory() as session:
            TaskRepository(session).write_progress(
                task_id,
                progress,
                worker_id=self.worker_id,
            )


class StaleTaskReaper:
    """appサービスから定期実行するstale task回収器。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        config: WorkerConfig,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._clock = clock

    def run_once(self) -> list[int]:
        now = self._clock()
        stale_before = now - timedelta(seconds=self._config.stale_threshold_sec)
        with self._session_factory() as session:
            recovered = TaskRepository(session).recover_stale(
                stale_before=stale_before,
                now=now,
            )
            for task_id in recovered:
                task = session.get(Task, task_id)
                if task is not None and task.status == TaskStatus.UNAVAILABLE:
                    sync_target_after_task(
                        session,
                        task,
                        TaskStatus.UNAVAILABLE,
                        error=task.error_message or "stale Taskが再試行上限に達しました",
                    )
        if recovered:
            logger.warning("Recovered stale tasks", extra={"task_ids": recovered})
        return recovered

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - 一時的DB競合で回収ループを失わない
                logger.warning("Stale task recovery failed", exc_info=True)
            stop.wait(self._config.heartbeat_interval_sec)


__all__ = ["StaleTaskReaper", "Worker", "WorkerConfig", "make_worker_id"]
