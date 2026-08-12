"""現バージョン唯一の組み込みHook: event_log記録。"""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from sluicery.db.repositories.event_log import EventLogRepository


class EventLogHook:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def emit(self, event_type: str, payload: dict) -> None:
        with self._session_factory() as session:
            EventLogRepository(session).record(event_type, payload)


__all__ = ["EventLogHook"]
