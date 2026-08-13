from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select

from sluicery.db.models import AuthSession
from sluicery.db.repositories.base import BaseRepository


class AuthSessionRepository(BaseRepository[AuthSession]):
    model = AuthSession

    def get_by_token_hash(self, token_hash: str) -> AuthSession | None:
        return self.session.scalar(
            select(AuthSession).where(AuthSession.token_hash == token_hash)
        )

    def delete_by_token_hash(self, token_hash: str) -> None:
        self.session.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash))
        self.session.commit()

    def delete_for_user(self, user_id: int) -> None:
        self.session.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
        self.session.commit()

    def delete_expired(self, now: datetime) -> None:
        self.session.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
        self.session.commit()


__all__ = ["AuthSessionRepository"]
