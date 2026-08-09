"""rclone `--use-json-log` の転送進捗パーサ。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MAX_LINE_LEN = 1_000_000


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _floating(value: Any) -> float | None:
    number = _number(value)
    return float(number) if number is not None else None


@dataclass(frozen=True)
class TransferProgressEvent:
    bytes_transferred: int | None
    total_bytes: int | None
    speed: float | None
    eta_seconds: float | None
    transfers: int | None
    total_transfers: int | None
    errors: int | None
    elapsed_seconds: float | None
    raw: dict[str, Any]


def parse_rclone_log_line(line: str) -> TransferProgressEvent | None:
    """stats を持つ JSON 行だけを進捗へ変換し、壊れた入力は捨てる。"""
    if not line or len(line) > MAX_LINE_LEN:
        return None
    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    stats = payload.get("stats")
    if not isinstance(stats, dict):
        return None
    return TransferProgressEvent(
        bytes_transferred=_integer(stats.get("bytes")),
        total_bytes=_integer(stats.get("totalBytes")),
        speed=_floating(stats.get("speed")),
        eta_seconds=_floating(stats.get("eta")),
        transfers=_integer(stats.get("transfers")),
        total_transfers=_integer(stats.get("totalTransfers")),
        errors=_integer(stats.get("errors")),
        elapsed_seconds=_floating(stats.get("elapsedTime")),
        raw=payload,
    )


__all__ = ["MAX_LINE_LEN", "TransferProgressEvent", "parse_rclone_log_line"]
