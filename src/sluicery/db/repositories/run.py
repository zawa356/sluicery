from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from sluicery.db.models import Run, RunStatus, RunTrigger
from sluicery.db.repositories.base import BaseRepository


class RunRepository(BaseRepository[Run]):
    model = Run

    def list_recent(self, limit: int = 20) -> list[Run]:
        stmt = select(Run).order_by(Run.started_at.desc()).limit(limit)
        return list(self.session.scalars(stmt))

    def start(
        self,
        *,
        trigger: RunTrigger,
        kind: str,
        playlist_id: int,
        commit: bool = True,
    ) -> Run:
        run = Run(trigger=trigger, kind=kind, playlist_id=playlist_id, status=RunStatus.RUNNING)
        self.session.add(run)
        self.session.flush()
        if commit:
            self.session.commit()
            self.session.refresh(run)
        return run

    def finish(
        self,
        run_id: int,
        status: RunStatus,
        stats: dict[str, Any],
        *,
        now: datetime | None = None,
        commit: bool = True,
    ) -> bool:
        result = self.session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(status=status, stats_json=stats, finished_at=now or datetime.now(UTC))
        )
        if commit:
            self.session.commit()
        return bool(getattr(result, "rowcount", 0) or 0)


__all__ = ["RunRepository"]
