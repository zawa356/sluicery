from __future__ import annotations

from sqlalchemy import select

from sluicery.db.models import Run
from sluicery.db.repositories.base import BaseRepository


class RunRepository(BaseRepository[Run]):
    model = Run

    def list_recent(self, limit: int = 20) -> list[Run]:
        stmt = select(Run).order_by(Run.started_at.desc()).limit(limit)
        return list(self.session.scalars(stmt))


__all__ = ["RunRepository"]
