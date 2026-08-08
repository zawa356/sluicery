from __future__ import annotations

from sqlalchemy import func, select

from sluicery.db.models import Item, Target, TargetStatus
from sluicery.db.repositories.base import BaseRepository


class TargetRepository(BaseRepository[Target]):
    model = Target

    def list_by_status(self, status: TargetStatus) -> list[Target]:
        stmt = select(Target).where(Target.status == status)
        return list(self.session.scalars(stmt))

    def count_by_status(self, playlist_id: int) -> dict[TargetStatus, int]:
        stmt = (
            select(Target.status, func.count())
            .join(Item, Target.item_id == Item.id)
            .where(Item.playlist_id == playlist_id)
            .group_by(Target.status)
        )
        return {status: count for status, count in self.session.execute(stmt).all()}


__all__ = ["TargetRepository"]
