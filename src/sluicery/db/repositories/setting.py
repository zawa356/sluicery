from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from sluicery.db.models import Setting
from sluicery.db.repositories.base import BaseRepository


class SettingRepository(BaseRepository[Setting]):
    """`setting` テーブルの生の CRUD のみを担う。

    キーの妥当性検証・型変換・既定値の解決は `core.settings` の責務とする
    （このリポジトリは特定のキー体系を知らない）。
    """

    model = Setting

    def get_all_overrides(self) -> list[Setting]:
        return list(self.session.scalars(select(Setting)))

    def set_override(self, key: str, value_json: str) -> Setting:
        row = self.session.get(Setting, key)
        if row is None:
            row = Setting(key=key, value_json=value_json, updated_at=datetime.now(UTC))
            self.session.add(row)
        else:
            row.value_json = value_json
            row.updated_at = datetime.now(UTC)
        self.session.commit()
        self.session.refresh(row)
        return row

    def delete_override(self, key: str) -> None:
        row = self.session.get(Setting, key)
        if row is not None:
            self.session.delete(row)
            self.session.commit()


__all__ = ["SettingRepository"]
