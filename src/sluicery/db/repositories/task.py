from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update

from sluicery.db.models import Task, TaskStatus, WorkerClass
from sluicery.db.repositories.base import BaseRepository


class TaskRepository(BaseRepository[Task]):
    model = Task

    def list_pending(self) -> list[Task]:
        stmt = select(Task).where(Task.status == TaskStatus.PENDING)
        return list(self.session.scalars(stmt))

    def claim_next(self, worker_class: WorkerClass) -> Task | None:
        """`queued` な Task を1件だけアトミックに `running` へ遷移させる。

        単一の `UPDATE ... WHERE id IN (SELECT ... LIMIT 1) RETURNING` 文で
        候補選定と状態更新を1つの SQL 文にまとめている。SQLite は書き込みを
        直列化する（本プロジェクトでは `busy_timeout` PRAGMA も設定済み）ため、
        複数プロセス・複数スレッドから同時に呼ばれても、2件目以降の UPDATE は
        対象行が既に status<>queued になっており0件ヒットで安全に空振りする。
        """
        candidate = (
            select(Task.id)
            .where(Task.status == TaskStatus.QUEUED, Task.worker_class == worker_class)
            .order_by(Task.priority.desc(), Task.id.asc())
            .limit(1)
        )
        stmt = (
            update(Task)
            .where(Task.id.in_(candidate))
            .where(Task.status == TaskStatus.QUEUED)
            .values(status=TaskStatus.RUNNING, started_at=datetime.now(UTC))
            .returning(Task.id)
        )
        claimed_id = self.session.execute(stmt).scalars().first()
        self.session.commit()
        if claimed_id is None:
            return None
        return self.session.get(Task, claimed_id)


__all__ = ["TaskRepository"]
