"""将来のトランスコード処理が実装するPostProcessor契約。"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Protocol

from sluicery.db.models import Artifact


class SourceDisposition(StrEnum):
    RETAIN = "retain"
    REPLACE = "replace"


class PostProcessor(Protocol):
    name: str
    worker_class: str
    source_disposition: SourceDisposition

    def applies_to(self, artifact: Artifact) -> bool: ...

    def run(self, artifact: Artifact, workdir: Path) -> list[Artifact]: ...


__all__ = ["PostProcessor", "SourceDisposition"]
