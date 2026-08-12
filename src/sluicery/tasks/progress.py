"""SQLiteへの進捗書き込みを時間・進捗率でスロットリングする。"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


class ProgressThrottler:
    def __init__(
        self,
        *,
        interval_sec: float,
        percent_step: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval_sec = interval_sec
        self._percent_step = percent_step
        self._clock = clock
        self._last_write_at: float | None = None
        self._last_percent: float | None = None

    def should_write(self, percent: float | None, *, final: bool = False) -> bool:
        now = self._clock()
        elapsed = (
            self._last_write_at is None or now - self._last_write_at >= self._interval_sec
        )
        advanced = (
            percent is not None
            and (
                self._last_percent is None
                or percent - self._last_percent >= self._percent_step
            )
        )
        if not (final or elapsed or advanced):
            return False
        self._last_write_at = now
        if percent is not None:
            self._last_percent = percent
        return True


class ProgressWriter:
    def __init__(
        self,
        write: Callable[[dict[str, Any]], None],
        *,
        interval_sec: float,
        percent_step: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._write = write
        self._throttler = ProgressThrottler(
            interval_sec=interval_sec,
            percent_step=percent_step,
            clock=clock,
        )

    def emit(self, progress: dict[str, Any], *, final: bool = False) -> bool:
        raw_percent = progress.get("percent")
        percent = float(raw_percent) if isinstance(raw_percent, int | float) else None
        if not self._throttler.should_write(percent, final=final):
            return False
        self._write(progress)
        return True


__all__ = ["ProgressThrottler", "ProgressWriter"]
