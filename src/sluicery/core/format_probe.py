"""フォーマット検査結果の安全な抽出とUI向けレート制限。"""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from typing import Any


class FormatProbeResultError(ValueError):
    """yt-dlpの検査結果を安全な表示形式へ変換できない。"""


class FormatProbeRateLimited(RuntimeError):
    def __init__(self, retry_after_sec: float) -> None:
        self.retry_after_sec = retry_after_sec
        super().__init__("フォーマット検査の実行間隔が短すぎます")


@dataclass(frozen=True)
class FormatRow:
    format_id: str
    extension: str | None
    resolution: str | None
    video_codec: str | None
    audio_codec: str | None
    estimated_size: int | None


@dataclass(frozen=True)
class FormatProbeResult:
    formats: tuple[FormatRow, ...]
    selected_format_ids: tuple[str, ...]
    estimated_size: int | None


class FormatProbeLimiter:
    """appプロセス内で検査開始を直列化し、全Profile共通の最短間隔を守る。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_started: float | None = None

    def acquire(self, min_interval_sec: float, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        with self._lock:
            if self._last_started is not None:
                remaining = min_interval_sec - (current - self._last_started)
                if remaining > 0:
                    raise FormatProbeRateLimited(remaining)
            self._last_started = current


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _size(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0:
        return None
    return int(numeric)


def _estimated_size(value: dict[str, Any]) -> int | None:
    return _size(value.get("filesize")) or _size(value.get("filesize_approx"))


def parse_format_probe_output(line: str) -> FormatProbeResult:
    try:
        raw = json.loads(line)
    except (TypeError, json.JSONDecodeError) as exc:
        raise FormatProbeResultError("フォーマット検査結果のJSONが不正です") from exc
    if not isinstance(raw, dict):
        raise FormatProbeResultError("フォーマット検査結果がオブジェクトではありません")

    rows: list[FormatRow] = []
    formats = raw.get("formats")
    if isinstance(formats, list):
        for value in formats:
            if not isinstance(value, dict):
                continue
            format_id = _text(value.get("format_id"))
            if format_id is None:
                continue
            resolution = _text(value.get("resolution"))
            if resolution is None:
                width, height = value.get("width"), value.get("height")
                if isinstance(width, int) and isinstance(height, int):
                    resolution = f"{width}x{height}"
            rows.append(
                FormatRow(
                    format_id=format_id,
                    extension=_text(value.get("ext")),
                    resolution=resolution,
                    video_codec=_text(value.get("vcodec")),
                    audio_codec=_text(value.get("acodec")),
                    estimated_size=_estimated_size(value),
                )
            )

    selected_values = raw.get("requested_formats")
    if not isinstance(selected_values, list):
        requested_downloads = raw.get("requested_downloads")
        selected_values = requested_downloads if isinstance(requested_downloads, list) else []
    selected_ids: list[str] = []
    selected_sizes: list[int | None] = []
    for value in selected_values:
        if not isinstance(value, dict):
            continue
        format_id = _text(value.get("format_id"))
        if format_id is not None:
            selected_ids.append(format_id)
        size = _estimated_size(value)
        selected_sizes.append(size)

    if not selected_ids:
        combined = _text(raw.get("format_id"))
        if combined is not None:
            selected_ids.extend(part for part in combined.split("+") if part)
    total_size = (
        sum(size for size in selected_sizes if size is not None)
        if selected_sizes and all(size is not None for size in selected_sizes)
        else _estimated_size(raw)
    )
    return FormatProbeResult(
        formats=tuple(rows),
        selected_format_ids=tuple(dict.fromkeys(selected_ids)),
        estimated_size=total_size,
    )


__all__ = [
    "FormatProbeLimiter",
    "FormatProbeRateLimited",
    "FormatProbeResult",
    "FormatProbeResultError",
    "FormatRow",
    "parse_format_probe_output",
]
