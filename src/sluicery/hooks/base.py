"""Phase 18の購読機構が実装するHook契約と発火点。"""

from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class Hook(Protocol):
    def emit(self, event_type: str, payload: dict) -> None: ...


def emit_safely(hook: Hook, event_type: str, payload: dict) -> None:
    """フック失敗で本体処理を失敗させない。"""
    try:
        hook.emit(event_type, payload)
    except Exception:  # noqa: BLE001 - 拡張点の障害を本体から隔離する
        logger.exception("Hook emission failed", extra={"event_type": event_type})


__all__ = ["Hook", "emit_safely"]
