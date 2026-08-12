"""Phase 6のキュー検証専用ハンドラ。

worker.enable_test_tasks=true の場合にだけregistryへ追加される。本番のPhase 7
パイプラインからは参照しない。
"""

from __future__ import annotations

import math
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from sluicery.runner.base import BaseRunner, TimeoutPolicy
from sluicery.tasks.queue import TaskOutcome, TaskResult

ProgressCallback = Callable[[dict[str, Any]], object]


class TaskHandler(Protocol):
    def run(self, payload: dict[str, Any], on_progress: ProgressCallback) -> TaskResult: ...

    def cancel(self) -> None: ...


class _CancellableHandler:
    def __init__(self) -> None:
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()


class NoopHandler(_CancellableHandler):
    def run(self, payload: dict[str, Any], on_progress: ProgressCallback) -> TaskResult:
        on_progress({"status": "finished", "percent": 100.0})
        outcome = TaskOutcome.CANCELLED if self._cancelled.is_set() else TaskOutcome.SUCCEEDED
        return TaskResult(outcome)


class SleepHandler(_CancellableHandler):
    def run(self, payload: dict[str, Any], on_progress: ProgressCallback) -> TaskResult:
        try:
            seconds = float(payload.get("sec", 1))
        except (TypeError, ValueError):
            return TaskResult(TaskOutcome.FAILED, "payload.sec は数値で指定してください")
        if seconds < 0 or not math.isfinite(seconds):
            return TaskResult(TaskOutcome.FAILED, "payload.sec は0以上の有限値で指定してください")
        if seconds == 0:
            on_progress({"status": "finished", "percent": 100.0})
            return TaskResult(TaskOutcome.SUCCEEDED)
        started = time.monotonic()
        on_progress({"status": "running", "percent": 0.0, "elapsed_sec": 0.0})
        while True:
            elapsed = time.monotonic() - started
            if elapsed >= seconds:
                break
            if self._cancelled.wait(min(0.2, seconds - elapsed)):
                return TaskResult(TaskOutcome.CANCELLED)
            elapsed = min(seconds, time.monotonic() - started)
            on_progress(
                {
                    "status": "running",
                    "percent": elapsed / seconds * 100,
                    "elapsed_sec": elapsed,
                }
            )
        on_progress({"status": "finished", "percent": 100.0, "elapsed_sec": seconds})
        return TaskResult(TaskOutcome.SUCCEEDED)


class FailHandler(_CancellableHandler):
    def run(self, payload: dict[str, Any], on_progress: ProgressCallback) -> TaskResult:
        return TaskResult(TaskOutcome.FAILED, "検証用の一時的失敗")


class UnavailableHandler(_CancellableHandler):
    def run(self, payload: dict[str, Any], on_progress: ProgressCallback) -> TaskResult:
        return TaskResult(TaskOutcome.UNAVAILABLE, "検証用の回復不能エラー")


class BlockedHandler(_CancellableHandler):
    def run(self, payload: dict[str, Any], on_progress: ProgressCallback) -> TaskResult:
        return TaskResult(TaskOutcome.BLOCKED, "検証用の外的要因")


class _SpawnRunner(BaseRunner):
    def __init__(self) -> None:
        super().__init__(Path(sys.executable), runner_name="dummy-spawn", log_prefix="dummy-spawn")

    def run(self, seconds: float) -> TaskResult:
        child_code = f"import time; time.sleep({seconds!r})"
        parent_code = (
            "import subprocess,sys; "
            f"subprocess.run([sys.executable, '-c', {child_code!r}], check=False)"
        )
        result = self._run_process(
            ["-c", parent_code],
            timeout=TimeoutPolicy(
                idle_sec=None,
                absolute_sec=max(5.0, seconds + 5.0),
                term_grace_sec=2.0,
            ),
        )
        if result.terminated_by == "cancel":
            return TaskResult(TaskOutcome.CANCELLED)
        if result.returncode == 0:
            return TaskResult(TaskOutcome.SUCCEEDED)
        return TaskResult(TaskOutcome.FAILED, result.stderr_tail)


class SpawnHandler:
    def __init__(self) -> None:
        self._runner = _SpawnRunner()

    def cancel(self) -> None:
        self._runner.cancel()

    def run(self, payload: dict[str, Any], on_progress: ProgressCallback) -> TaskResult:
        try:
            seconds = float(payload.get("sec", 30))
        except (TypeError, ValueError):
            return TaskResult(TaskOutcome.FAILED, "payload.sec は数値で指定してください")
        if seconds < 0 or not math.isfinite(seconds):
            return TaskResult(TaskOutcome.FAILED, "payload.sec は0以上の有限値で指定してください")
        on_progress({"status": "running", "percent": 0.0})
        return self._runner.run(seconds)


DUMMY_HANDLER_FACTORIES: dict[str, Callable[[], TaskHandler]] = {
    "noop": NoopHandler,
    "sleep": SleepHandler,
    "fail": FailHandler,
    "fail_unavailable": UnavailableHandler,
    "fail_blocked": BlockedHandler,
    "spawn": SpawnHandler,
}

__all__ = ["DUMMY_HANDLER_FACTORIES", "ProgressCallback", "TaskHandler"]
