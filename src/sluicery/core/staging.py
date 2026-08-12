"""Staging上の孤立ファイルを読み取り専用で検出する。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from sluicery.db.models import Task


@dataclass(frozen=True)
class OrphanFile:
    path: Path
    relative_path: str
    size: int


def find_orphans(session: Session, staging_dir: Path) -> list[OrphanFile]:
    root = staging_dir.resolve(strict=False)
    active_work_ids: set[str] = set()
    for payload in session.scalars(select(Task.payload_json)):
        if isinstance(payload, dict) and isinstance(payload.get("work_id"), str):
            active_work_ids.add(payload["work_id"])
    result: list[OrphanFile] = []
    if not root.exists():
        return result
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] in active_work_ids:
            continue
        result.append(OrphanFile(path, relative.as_posix(), path.stat().st_size))
    return sorted(result, key=lambda item: item.relative_path)


__all__ = ["OrphanFile", "find_orphans"]
