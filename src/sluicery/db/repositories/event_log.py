from __future__ import annotations

from sluicery.db.models import EventLog
from sluicery.db.repositories.base import BaseRepository


class EventLogRepository(BaseRepository[EventLog]):
    model = EventLog


__all__ = ["EventLogRepository"]
