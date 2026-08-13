from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from sluicery.db.models import Item, ItemMembership
from sluicery.db.repositories.base import BaseRepository


class ItemRepository(BaseRepository[Item]):
    model = Item

    def get_by_source_id(self, playlist_id: int, source_id: str) -> Item | None:
        stmt = select(Item).where(Item.playlist_id == playlist_id, Item.source_id == source_id)
        return self.session.scalars(stmt).first()

    def upsert_many(self, playlist_id: int, items: list[dict[str, Any]]) -> list[Item]:
        """`source_id` が既存なら更新、なければ作成する（discover 用の下準備）。

        membership の遷移判定（active/delisted の切り替え）はここでは行わない。
        呼び出し側（Phase 7〜8 の core/sync.py）の責務とする。
        """
        result: list[Item] = []
        for data in items:
            source_id = data["source_id"]
            existing = self.get_by_source_id(playlist_id, source_id)
            if existing is None:
                obj = Item(playlist_id=playlist_id, **data)
                self.session.add(obj)
            else:
                for key, value in data.items():
                    setattr(existing, key, value)
                obj = existing
            result.append(obj)
        self.session.commit()
        for obj in result:
            self.session.refresh(obj)
        return result

    def list_by_membership(self, playlist_id: int, membership: ItemMembership) -> list[Item]:
        stmt = select(Item).where(Item.playlist_id == playlist_id, Item.membership == membership)
        return list(self.session.scalars(stmt))

    def compare_and_set_membership(
        self,
        item_id: int,
        expected: set[ItemMembership],
        membership: ItemMembership,
        *,
        now: datetime | None = None,
        commit: bool = True,
    ) -> bool:
        """遷移規則を持たない、membership のCAS更新プリミティブ。"""
        changed_at = now or datetime.now(UTC)
        values: dict[str, Any] = {"membership": membership}
        if membership == ItemMembership.DELISTED:
            values["delisted_at"] = changed_at
        else:
            values["delisted_at"] = None
            values["last_seen_at"] = changed_at
        result = self.session.execute(
            update(Item).where(Item.id == item_id, Item.membership.in_(expected)).values(**values)
        )
        if commit:
            self.session.commit()
        return bool(getattr(result, "rowcount", 0) or 0)


__all__ = ["ItemRepository"]
