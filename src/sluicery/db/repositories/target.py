from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update

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

    def compare_and_set_status(
        self,
        target_id: int,
        expected: set[TargetStatus],
        status: TargetStatus,
        *,
        error: str | None = None,
        blocked_reason: str | None = None,
        increment_retry: bool = False,
        now: datetime | None = None,
        extra_values: dict[str, Any] | None = None,
    ) -> bool:
        """遷移規則を持たない、所有権条件付き単一UPDATEのDBプリミティブ。"""
        values: dict[str, Any] = {
            "status": status,
            "last_error": error,
            "blocked_reason": blocked_reason,
            "last_attempt_at": now or datetime.now(UTC),
        }
        if increment_retry:
            values["retry_count"] = Target.retry_count + 1
        if extra_values:
            values.update(extra_values)
        result = self.session.execute(
            update(Target)
            .where(Target.id == target_id, Target.status.in_(expected))
            .values(**values)
        )
        self.session.commit()
        return bool(getattr(result, "rowcount", 0) or 0)


__all__ = ["TargetRepository"]
