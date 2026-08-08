"""`--progress-template` の JSON 行パース（要件定義 §5.2, docs/phase3_指示書.md §4）。

パーサはどんな壊れた入力でも例外を投げない（§4.3）。DB への書き込みは行わない
（Phase 6/7 のワーカー側の責務。§4.1）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# 病的に長い行（yt-dlp の不具合や破損出力）で JSON パースに時間を掛けないための上限。
MAX_LINE_LEN = 1_000_000


@dataclass
class ProgressEvent:
    status: str
    downloaded_bytes: int | None
    total_bytes: int | None
    speed: float | None
    eta: int | None
    fragment_index: int | None
    fragment_count: int | None
    filename: str | None
    raw: dict[str, Any]


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped.upper() == "NA":
            return None
        try:
            return int(float(stripped))
        except ValueError:
            return None
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped.upper() == "NA":
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def parse_progress_line(line: str) -> ProgressEvent | None:
    """1行を `ProgressEvent` にパースする。解釈できない行は None を返す（§4.3）。

    - 不完全な JSON 行
    - プレフィックスの無い行
    - `status` を欠く、または数値であるべきフィールドに `NA` / 空文字が入っている行
    - 極端に長い行

    いずれも例外にせず None として扱い、呼び出し側で「解釈できない行は破棄して
    ログに残す」判断ができるようにする。
    """
    if len(line) > MAX_LINE_LEN:
        return None

    try:
        payload = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(payload, dict):
        return None

    status = payload.get("status")
    if not isinstance(status, str) or not status:
        return None

    return ProgressEvent(
        status=status,
        downloaded_bytes=_as_int(payload.get("downloaded_bytes")),
        total_bytes=_as_int(payload.get("total_bytes") or payload.get("total_bytes_estimate")),
        speed=_as_float(payload.get("speed")),
        eta=_as_int(payload.get("eta")),
        fragment_index=_as_int(payload.get("fragment_index")),
        fragment_count=_as_int(payload.get("fragment_count")),
        filename=_as_str(payload.get("filename")),
        raw=payload,
    )


__all__ = ["MAX_LINE_LEN", "ProgressEvent", "parse_progress_line"]
