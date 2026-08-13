from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update

from sluicery.db.models import Playlist, PlaylistProfile
from sluicery.db.repositories.base import BaseRepository


class PlaylistRepository(BaseRepository[Playlist]):
    model = Playlist

    def list_enabled(self) -> list[Playlist]:
        stmt = select(Playlist).where(Playlist.enabled.is_(True))
        return list(self.session.scalars(stmt))

    def get_with_profiles(self, playlist_id: int) -> tuple[Playlist, list[PlaylistProfile]] | None:
        """Playlist と、それに紐づく PlaylistProfile 一覧を返す。

        `Playlist` は relationship を持たないため（models.py は素の FK のみ）、
        明示的な2クエリで構成する。
        """
        playlist = self.session.get(Playlist, playlist_id)
        if playlist is None:
            return None
        stmt = select(PlaylistProfile).where(PlaylistProfile.playlist_id == playlist_id)
        profiles = list(self.session.scalars(stmt))
        return playlist, profiles

    def set_last_discover_at(self, playlist_id: int, at: datetime, *, commit: bool = True) -> bool:
        result = self.session.execute(
            update(Playlist).where(Playlist.id == playlist_id).values(last_discover_at=at)
        )
        if commit:
            self.session.commit()
        return bool(getattr(result, "rowcount", 0) or 0)


__all__ = ["PlaylistRepository"]
