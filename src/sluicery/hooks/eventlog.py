"""現バージョン唯一の組み込みHook: event_log記録。"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from sqlalchemy.orm import Session, sessionmaker

from sluicery.db.repositories.event_log import EventLogRepository
from sluicery.hooks.base import EVENT_TYPES, sanitize_event_payload

logger = logging.getLogger(__name__)
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sluicery-hook")
_CAPACITY = threading.BoundedSemaphore(1000)
_PENDING_LOCK = threading.Lock()
_PENDING: set[Future[None]] = set()


def load_event_log_subscriptions(path: Path = Path("config/hooks.yaml")) -> frozenset[str]:
    if not path.exists():
        logger.warning("Hook config not found; event_log defaults to all events")
        return EVENT_TYPES
    try:
        raw = path.read_bytes()
        if not raw or len(raw) > 64 * 1024:
            raise ValueError
        if any(
            isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
            for token in yaml.scan(raw)
        ):
            raise ValueError
        parsed = yaml.safe_load(raw)
        event_log = parsed["subscriptions"]["event_log"]
        if set(parsed) != {"version", "subscriptions"} or parsed["version"] != 1:
            raise ValueError
        if set(parsed["subscriptions"]) != {"event_log"}:
            raise ValueError
        if set(event_log) != {"enabled", "events"}:
            raise ValueError
        if not isinstance(event_log["enabled"], bool) or not isinstance(
            event_log["events"], list
        ):
            raise ValueError
        events = frozenset(event_log["events"])
        if not all(isinstance(item, str) for item in events) or not events <= EVENT_TYPES:
            raise ValueError
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        logger.error(
            "Hook config is invalid; event_log subscriptions disabled",
            exc_info=exc,
        )
        return frozenset()
    return events if event_log["enabled"] else frozenset()


def _completed(future: Future[None]) -> None:
    with _PENDING_LOCK:
        _PENDING.discard(future)
    _CAPACITY.release()
    try:
        future.result()
    except Exception:  # noqa: BLE001 - Hook障害は本体と分離する
        logger.exception("Async event_log hook failed")


class EventLogHook:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        config_path: Path = Path("config/hooks.yaml"),
        enabled_events: frozenset[str] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._enabled_events = (
            load_event_log_subscriptions(config_path)
            if enabled_events is None
            else enabled_events
        )

    def emit(self, event_type: str, payload: dict) -> None:
        if event_type not in self._enabled_events:
            return
        safe_payload = sanitize_event_payload(event_type, payload)
        if not _CAPACITY.acquire(blocking=False):
            logger.error("Hook queue full; event dropped", extra={"event_type": event_type})
            return

        def record() -> None:
            with self._session_factory() as session:
                EventLogRepository(session).record(event_type, safe_payload)

        try:
            future = _EXECUTOR.submit(record)
        except Exception:  # noqa: BLE001 - submit失敗も本体へ返さない
            _CAPACITY.release()
            logger.exception("Hook submission failed", extra={"event_type": event_type})
            return
        with _PENDING_LOCK:
            _PENDING.add(future)
        future.add_done_callback(_completed)


def flush_pending_hooks(timeout: float = 5.0) -> bool:
    with _PENDING_LOCK:
        pending = tuple(_PENDING)
    if not pending:
        return True
    _done, not_done = wait(pending, timeout=timeout)
    return not not_done


def event_log_hook_for_session(session: Session) -> EventLogHook:
    return EventLogHook(sessionmaker(bind=session.get_bind(), expire_on_commit=False))


__all__ = [
    "EventLogHook",
    "event_log_hook_for_session",
    "flush_pending_hooks",
    "load_event_log_subscriptions",
]
