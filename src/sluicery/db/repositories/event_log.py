from __future__ import annotations

from sluicery.db.models import EventLog
from sluicery.db.repositories.base import BaseRepository


class EventLogRepository(BaseRepository[EventLog]):
    model = EventLog

    def record(self, event_type: str, payload: dict) -> EventLog:
        event = EventLog(event_type=event_type, payload_json=payload)
        self.session.add(event)
        self.session.commit()
        self.session.refresh(event)
        return event


__all__ = ["EventLogRepository"]
