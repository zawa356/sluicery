from __future__ import annotations

from sqlalchemy import select

from sluicery.db.models import YtdlpRelease, YtdlpReleaseStatus
from sluicery.db.repositories.base import BaseRepository


class YtdlpReleaseRepository(BaseRepository[YtdlpRelease]):
    """`ytdlp_release` の CRUD のみを担う。

    `active` を高々1件に保つ切替ロジックは `downloader/version.py` の責務とする
    （このリポジトリは複数行にまたがる不変条件を知らない）。
    """

    model = YtdlpRelease

    def get_by_version(self, version: str) -> YtdlpRelease | None:
        stmt = select(YtdlpRelease).where(YtdlpRelease.version == version)
        return self.session.scalars(stmt).first()

    def get_active(self) -> YtdlpRelease | None:
        stmt = select(YtdlpRelease).where(YtdlpRelease.status == YtdlpReleaseStatus.ACTIVE)
        return self.session.scalars(stmt).first()

    def list_installed(self) -> list[YtdlpRelease]:
        """`removed` を除く、導入済みバージョンを新しい順で返す。"""
        stmt = (
            select(YtdlpRelease)
            .where(YtdlpRelease.status != YtdlpReleaseStatus.REMOVED)
            .order_by(YtdlpRelease.installed_at.desc())
        )
        return list(self.session.scalars(stmt))


__all__ = ["YtdlpReleaseRepository"]
