"""Phase 18の購読機構が実装するHook契約と発火点。"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Protocol

from sluicery.runner.base import mask_log_text

logger = logging.getLogger(__name__)

EVENT_PAYLOAD_FIELDS: dict[str, frozenset[str]] = {
    "item_discovered": frozenset({"playlist_id", "item_id", "count"}),
    "item_delisted": frozenset({"playlist_id", "item_id", "count"}),
    "target_downloaded": frozenset(
        {"target_id", "artifact_id", "storage_id", "relative_path"}
    ),
    "target_failed": frozenset({"target_id", "task_id", "reason_code"}),
    "run_started": frozenset({"run_id", "playlist_id", "kind", "trigger"}),
    "run_finished": frozenset({"run_id", "playlist_id", "kind", "status"}),
    "run_failed": frozenset(
        {
            "run_id",
            "playlist_id",
            "kind",
            "component",
            "version",
            "reason_code",
            "rolled_back",
        }
    ),
    "artifact_published": frozenset(
        {"target_id", "artifact_id", "storage_id", "relative_path"}
    ),
    "artifact_missing": frozenset(
        {"artifact_id", "target_id", "storage_id", "relative_path"}
    ),
    "storage_unreachable": frozenset({"storage_id", "reason_code"}),
    "ytdlp_updated": frozenset({"version", "previous_version"}),
    "ytdlp_rollback": frozenset({"version", "previous_version"}),
}
EVENT_TYPES = frozenset(EVENT_PAYLOAD_FIELDS)
_FORBIDDEN_KEY_PARTS = (
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
    "url",
)
_URL_VALUE = re.compile(r"https?://", re.IGNORECASE)


class Hook(Protocol):
    def emit(self, event_type: str, payload: dict) -> None: ...


def sanitize_event_payload(event_type: str, payload: Mapping[str, object]) -> dict:
    """event別ホワイトリストと共通マスク境界を適用する。"""
    allowed = EVENT_PAYLOAD_FIELDS.get(event_type)
    if allowed is None:
        raise ValueError("未定義のHook eventです")
    safe: dict[str, object] = {}
    for key, value in payload.items():
        normalized = key.lower().replace("-", "_")
        if key not in allowed or any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
            continue
        if value is None or isinstance(value, bool | int | float):
            safe[key] = value
        elif isinstance(value, str):
            if _URL_VALUE.search(value):
                safe[key] = "********"
                continue
            masked = mask_log_text(value[:1000])
            safe[key] = masked if masked == value[:1000] else "********"
    return safe


def emit_safely(hook: Hook, event_type: str, payload: dict) -> None:
    """フック失敗で本体処理を失敗させない。"""
    try:
        hook.emit(event_type, payload)
    except Exception:  # noqa: BLE001 - 拡張点の障害を本体から隔離する
        logger.exception("Hook emission failed", extra={"event_type": event_type})


__all__ = [
    "EVENT_PAYLOAD_FIELDS",
    "EVENT_TYPES",
    "Hook",
    "emit_safely",
    "sanitize_event_payload",
]
