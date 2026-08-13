from __future__ import annotations

from sqlalchemy import select

from sluicery.db.models import PlaylistProfile
from sluicery.db.repositories.base import BaseRepository


class PlaylistProfileRepository(BaseRepository[PlaylistProfile]):
    model = PlaylistProfile

    def list_enabled_for_playlist(self, playlist_id: int) -> list[PlaylistProfile]:
        stmt = (
            select(PlaylistProfile)
            .where(
                PlaylistProfile.playlist_id == playlist_id,
                PlaylistProfile.enabled.is_(True),
            )
            .order_by(PlaylistProfile.sort_order, PlaylistProfile.id)
        )
        return list(self.session.scalars(stmt))


__all__ = ["PlaylistProfileRepository"]
