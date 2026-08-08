"""汎用リポジトリ基底クラス（docs/phase2_指示書.md §7.1）。

- セッションは外部から受け取る。リポジトリが自前でセッションを作らない
- クエリはリポジトリの内側に閉じる。呼び出し側に SQLAlchemy の式を書かせない
- 状態遷移のロジックを書かない。永続化のみを担う
  （遷移の妥当性判定は Phase 7〜8 の `core/` に置く）
- **レコード削除はファイル削除を引き起こさない。** 実ファイル操作コードは
  このリポジトリ層はもちろん、Phase 2 の時点ではコードベース全体に存在しない
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session


class BaseRepository[ModelT]:
    model: type[ModelT]

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, id_: Any) -> ModelT | None:
        return self.session.get(self.model, id_)

    def list(self, *, limit: int | None = None, offset: int | None = None) -> list[ModelT]:
        stmt = select(self.model)
        if offset is not None:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt))

    def create(self, **kwargs: Any) -> ModelT:
        obj = self.model(**kwargs)
        self.session.add(obj)
        self.session.commit()
        self.session.refresh(obj)
        return obj

    def update(self, obj: ModelT, **kwargs: Any) -> ModelT:
        for key, value in kwargs.items():
            setattr(obj, key, value)
        self.session.commit()
        self.session.refresh(obj)
        return obj

    def delete(self, obj: ModelT) -> None:
        self.session.delete(obj)
        self.session.commit()

    def count(self) -> int:
        stmt = select(func.count()).select_from(self.model)
        return self.session.scalar(stmt) or 0


__all__ = ["BaseRepository"]
