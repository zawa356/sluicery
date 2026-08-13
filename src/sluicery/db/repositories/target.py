from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update

from sluicery.db.models import Item, PlaylistProfile, Target, TargetStatus
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
        commit: bool = True,
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
        if commit:
            self.session.commit()
        return bool(getattr(result, "rowcount", 0) or 0)

    def create_missing_for_items(
        self,
        item_ids: list[int],
        playlist_profile_ids: list[int],
        *,
        commit: bool = True,
    ) -> list[Target]:
        """Item x PlaylistProfile の未作成分だけを追加する。"""
        if not item_ids or not playlist_profile_ids:
            return []
        existing_stmt = select(Target.item_id, Target.playlist_profile_id).where(
            Target.item_id.in_(item_ids),
            Target.playlist_profile_id.in_(playlist_profile_ids),
        )
        existing = set(self.session.execute(existing_stmt).all())
        created = [
            Target(item_id=item_id, playlist_profile_id=profile_id)
            for item_id in item_ids
            for profile_id in playlist_profile_ids
            if (item_id, profile_id) not in existing
        ]
        self.session.add_all(created)
        self.session.flush()
        if commit:
            self.session.commit()
            for target in created:
                self.session.refresh(target)
        return created

    def list_for_playlist(self, playlist_id: int) -> list[Target]:
        stmt = (
            select(Target)
            .join(Item, Target.item_id == Item.id)
            .join(PlaylistProfile, Target.playlist_profile_id == PlaylistProfile.id)
            .where(Item.playlist_id == playlist_id, PlaylistProfile.playlist_id == playlist_id)
        )
        return list(self.session.scalars(stmt))


__all__ = ["TargetRepository"]
