from __future__ import annotations

from sqlalchemy import select

from sluicery.db.models import Storage
from sluicery.db.repositories.base import BaseRepository


class StorageRepository(BaseRepository[Storage]):
    model = Storage

    def list_enabled(self) -> list[Storage]:
        stmt = select(Storage).where(Storage.enabled.is_(True))
        return list(self.session.scalars(stmt))


__all__ = ["StorageRepository"]
