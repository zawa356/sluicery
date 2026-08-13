"""Task実行結果とリトライ計算。Phase 7の実ハンドラもこの契約を使う。"""

from __future__ import annotations

import enum
import random
from dataclasses import dataclass
from typing import Any


class TaskOutcome(enum.StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TaskResult:
    outcome: TaskOutcome
    message: str | None = None
    payload_update: dict[str, Any] | None = None
    reason_code: str | None = None


def retry_delay_sec(
    attempt: int,
    *,
    base_sec: float,
    max_sec: float,
    random_fraction: float | None = None,
) -> float:
    """指数バックオフへ最大10%の正のジッターを加え、上限で丸める。"""
    if attempt < 1:
        raise ValueError("attempt は1以上で指定してください")
    exponential = min(max_sec, base_sec * (2 ** (attempt - 1)))
    fraction = random.random() if random_fraction is None else random_fraction
    jitter_room = max(0.0, min(exponential * 0.1, max_sec - exponential))
    return exponential + jitter_room * fraction


__all__ = ["TaskOutcome", "TaskResult", "retry_delay_sec"]
