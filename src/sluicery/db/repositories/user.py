from __future__ import annotations

from typing import Any

from sqlalchemy import select

from sluicery.db.models import User
from sluicery.db.repositories.base import BaseRepository


class DuplicateUserError(RuntimeError):
    """`user` は単一ユーザーのみを許可する（要件定義 §7.1）。"""


class UserRepository(BaseRepository[User]):
    model = User

    def get_single(self) -> User | None:
        return self.session.scalars(select(User)).first()

    def create_single(self, **kwargs: Any) -> User:
        if self.get_single() is not None:
            raise DuplicateUserError("user は既に存在します（2件目の作成は禁止されています）")
        return self.create(**kwargs)


__all__ = ["DuplicateUserError", "UserRepository"]
