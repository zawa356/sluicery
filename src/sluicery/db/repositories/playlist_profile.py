from __future__ import annotations

from sluicery.db.models import PlaylistProfile
from sluicery.db.repositories.base import BaseRepository


class PlaylistProfileRepository(BaseRepository[PlaylistProfile]):
    model = PlaylistProfile


__all__ = ["PlaylistProfileRepository"]
