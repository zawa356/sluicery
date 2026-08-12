from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from sluicery.db.models import Artifact
from sluicery.db.repositories.base import BaseRepository


class ArtifactRepository(BaseRepository[Artifact]):
    model = Artifact

    def find_by_source_id(self, storage_id: int, source_id: str) -> Artifact | None:
        """relink 用の下準備。

        `artifact` テーブルは `source_id` を直接持たない。ファイル名末尾
        （拡張子直前）に付与される `[<source_id>]` の規約（AISTATE.md 参照）
        に従い、`relative_path` を照合する。
        """
        pattern = f"%[{source_id}]%"
        stmt = select(Artifact).where(
            Artifact.storage_id == storage_id, Artifact.relative_path.like(pattern)
        )
        return self.session.scalars(stmt).first()

    def create_source_if_missing(self, **values) -> Artifact:
        """index再実行時も同じArtifactを重複作成しない。"""
        stmt = (
            insert(Artifact)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[
                    Artifact.target_id,
                    Artifact.role,
                    Artifact.storage_id,
                    Artifact.relative_path,
                ]
            )
        )
        self.session.execute(stmt)
        self.session.commit()
        artifact = self.session.scalar(
            select(Artifact).where(
                Artifact.target_id == values["target_id"],
                Artifact.role == values["role"],
                Artifact.storage_id == values["storage_id"],
                Artifact.relative_path == values["relative_path"],
            )
        )
        assert artifact is not None
        return artifact


__all__ = ["ArtifactRepository"]
