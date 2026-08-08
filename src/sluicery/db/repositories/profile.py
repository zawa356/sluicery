from __future__ import annotations

from sluicery.db.models import Profile
from sluicery.db.repositories.base import BaseRepository


class ProfileRepository(BaseRepository[Profile]):
    model = Profile


__all__ = ["ProfileRepository"]
