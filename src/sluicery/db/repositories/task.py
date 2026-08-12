from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, exists, func, or_, select, update
from sqlalchemy.orm import aliased

from sluicery.db.models import Task, TaskStatus, WorkerClass
from sluicery.db.repositories.base import BaseRepository

CLAIMABLE_STATUSES = (TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.BLOCKED)
WAITING_STATUSES = (TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.BLOCKED)


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


class TaskRepository(BaseRepository[Task]):
    model = Task

    def list_pending(self) -> list[Task]:
        stmt = select(Task).where(Task.status.in_(WAITING_STATUSES))
        return list(self.session.scalars(stmt))

    def list_filtered(
        self,
        *,
        status: TaskStatus | None = None,
        worker_class: WorkerClass | None = None,
    ) -> list[Task]:
        stmt = select(Task)
        if status is not None:
            stmt = stmt.where(Task.status == status)
        if worker_class is not None:
            stmt = stmt.where(Task.worker_class == worker_class)
        return list(self.session.scalars(stmt.order_by(Task.id.desc())))

    def claim_next(
        self,
        worker_class: WorkerClass,
        *,
        worker_id: str = "unidentified-worker",
        now: datetime | None = None,
    ) -> Task | None:
        """実行可能な Task を単一の UPDATE ... RETURNING でclaimする。"""
        claimed_at = now or datetime.now(UTC)
        dependency = aliased(Task)
        dependency_succeeded = or_(
            Task.depends_on_task_id.is_(None),
            exists(
                select(dependency.id).where(
                    dependency.id == Task.depends_on_task_id,
                    dependency.status == TaskStatus.SUCCEEDED,
                )
            ),
        )
        time_eligible = and_(
            or_(Task.available_at.is_(None), Task.available_at <= claimed_at),
            or_(
                and_(Task.status != TaskStatus.BLOCKED, Task.blocked_until.is_(None)),
                Task.blocked_until <= claimed_at,
            ),
        )
        candidate = (
            select(Task.id)
            .where(
                Task.status.in_(CLAIMABLE_STATUSES),
                Task.worker_class == worker_class,
                Task.cancel_requested.is_(False),
                time_eligible,
                dependency_succeeded,
            )
            .order_by(Task.priority.desc(), Task.scheduled_at.asc(), Task.id.asc())
            .limit(1)
        )
        stmt = (
            update(Task)
            .where(Task.id.in_(candidate), Task.status.in_(CLAIMABLE_STATUSES))
            .values(
                status=TaskStatus.RUNNING,
                worker_id=worker_id,
                started_at=claimed_at,
                heartbeat_at=claimed_at,
                finished_at=None,
                available_at=None,
                blocked_until=None,
                blocked_reason=None,
            )
            .returning(Task.id)
        )
        claimed_id = self.session.execute(stmt).scalars().first()
        self.session.commit()
        if claimed_id is None:
            return None
        return self.session.get(Task, claimed_id)

    def heartbeat(
        self,
        task_id: int,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> bool | None:
        """所有中ならheartbeatを単一UPDATEし、cancel要求の有無を返す。"""
        stmt = (
            update(Task)
            .where(
                Task.id == task_id,
                Task.status == TaskStatus.RUNNING,
                Task.worker_id == worker_id,
            )
            .values(heartbeat_at=now or datetime.now(UTC))
            .returning(Task.cancel_requested)
        )
        result = self.session.execute(stmt).scalar_one_or_none()
        self.session.commit()
        return result

    def write_result_payload(
        self,
        task_id: int,
        worker_id: str,
        values: dict[str, Any],
    ) -> bool:
        """ハンドラ結果を、進捗を保持したまま所有権付きで payload へ統合する。"""
        task = self.session.scalar(
            select(Task).where(
                Task.id == task_id,
                Task.status == TaskStatus.RUNNING,
                Task.worker_id == worker_id,
            )
        )
        if task is None:
            return False
        payload = dict(task.payload_json or {})
        payload.update(values)
        result = self.session.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.status == TaskStatus.RUNNING,
                Task.worker_id == worker_id,
            )
            .values(payload_json=payload)
        )
        self.session.commit()
        return bool(_rowcount(result))

    def mark_succeeded(self, task_id: int, worker_id: str, *, now: datetime | None = None) -> bool:
        return self._finish(
            task_id,
            worker_id,
            status=TaskStatus.SUCCEEDED,
            now=now,
        )

    def mark_unavailable(
        self,
        task_id: int,
        worker_id: str,
        *,
        error_message: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        changed = self._finish(
            task_id,
            worker_id,
            status=TaskStatus.UNAVAILABLE,
            error_message=error_message,
            now=now,
        )
        if changed:
            self.cancel_descendants(task_id, now=now)
        return changed

    def mark_failed(
        self,
        task_id: int,
        worker_id: str,
        *,
        error_message: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """再試行対象外のfailedを確定し、後続Taskをcancelledへ伝播する。"""
        changed = self._finish(
            task_id,
            worker_id,
            status=TaskStatus.FAILED,
            error_message=error_message,
            now=now,
        )
        if changed:
            self.cancel_descendants(task_id, now=now)
        return changed

    def mark_cancelled(self, task_id: int, worker_id: str, *, now: datetime | None = None) -> bool:
        return self._finish(
            task_id,
            worker_id,
            status=TaskStatus.CANCELLED,
            now=now,
        )

    def mark_failed_for_retry(
        self,
        task_id: int,
        worker_id: str,
        *,
        retry_delay_sec: float,
        error_message: str | None = None,
        now: datetime | None = None,
    ) -> TaskStatus | None:
        """所有権付き単一UPDATEでretryまたは終端状態を確定する。"""
        failed_at = now or datetime.now(UTC)
        next_attempts = Task.attempts + 1
        cancel_requested = Task.cancel_requested.is_(True)
        attempts_exhausted = next_attempts >= Task.max_attempts
        status_value = case(
            (cancel_requested, TaskStatus.CANCELLED),
            (attempts_exhausted, TaskStatus.UNAVAILABLE),
            else_=TaskStatus.PENDING,
        )
        stmt = (
            update(Task)
            .where(
                Task.id == task_id,
                Task.status == TaskStatus.RUNNING,
                Task.worker_id == worker_id,
            )
            .values(
                status=status_value,
                attempts=case(
                    (cancel_requested, Task.attempts),
                    else_=next_attempts,
                ),
                available_at=case(
                    (or_(cancel_requested, attempts_exhausted), None),
                    else_=failed_at + timedelta(seconds=retry_delay_sec),
                ),
                finished_at=case(
                    (or_(cancel_requested, attempts_exhausted), failed_at),
                    else_=None,
                ),
                error_message=error_message,
                worker_id=None,
                started_at=None,
                heartbeat_at=None,
                cancel_requested=False,
            )
            .returning(Task.status)
        )
        status = self.session.execute(stmt).scalar_one_or_none()
        self.session.commit()
        if status == TaskStatus.UNAVAILABLE:
            self.cancel_descendants(task_id, now=failed_at)
        return status

    def mark_blocked(
        self,
        task_id: int,
        worker_id: str,
        *,
        retry_after_sec: float,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """外的要因をblockedにし、attemptsを消費せず再claim時刻を設定する。"""
        blocked_at = now or datetime.now(UTC)
        cancel_requested = Task.cancel_requested.is_(True)
        stmt = (
            update(Task)
            .where(
                Task.id == task_id,
                Task.status == TaskStatus.RUNNING,
                Task.worker_id == worker_id,
            )
            .values(
                status=case(
                    (cancel_requested, TaskStatus.CANCELLED),
                    else_=TaskStatus.BLOCKED,
                ),
                blocked_until=case(
                    (cancel_requested, None),
                    else_=blocked_at + timedelta(seconds=retry_after_sec),
                ),
                blocked_reason=case((cancel_requested, None), else_=reason),
                worker_id=None,
                started_at=None,
                heartbeat_at=None,
                finished_at=case((cancel_requested, blocked_at), else_=None),
                available_at=None,
                cancel_requested=False,
                error_message=case((cancel_requested, None), else_=reason),
            )
        )
        result = self.session.execute(stmt)
        self.session.commit()
        return bool(_rowcount(result))

    def release_for_shutdown(
        self, task_id: int, worker_id: str, *, now: datetime | None = None
    ) -> bool:
        """正常停止時にattemptsを増やさず即時再実行可能なpendingへ戻す。"""
        stmt = (
            update(Task)
            .where(
                Task.id == task_id,
                Task.status == TaskStatus.RUNNING,
                Task.worker_id == worker_id,
            )
            .values(
                status=TaskStatus.PENDING,
                available_at=now or datetime.now(UTC),
                worker_id=None,
                started_at=None,
                heartbeat_at=None,
                finished_at=None,
                cancel_requested=False,
            )
        )
        result = self.session.execute(stmt)
        self.session.commit()
        return bool(_rowcount(result))

    def request_cancel(self, task_id: int, *, now: datetime | None = None) -> bool:
        """claimとの競合を単一の状態条件付きUPDATEで直列化する。"""
        requested_at = now or datetime.now(UTC)
        is_running = Task.status == TaskStatus.RUNNING
        stmt = (
            update(Task)
            .where(
                Task.id == task_id,
                or_(is_running, Task.status.in_(WAITING_STATUSES)),
            )
            .values(
                status=case((is_running, Task.status), else_=TaskStatus.CANCELLED),
                finished_at=case((is_running, Task.finished_at), else_=requested_at),
                cancel_requested=case((is_running, True), else_=False),
                worker_id=case((is_running, Task.worker_id), else_=None),
                started_at=case((is_running, Task.started_at), else_=None),
                heartbeat_at=case((is_running, Task.heartbeat_at), else_=None),
                available_at=case((is_running, Task.available_at), else_=None),
                blocked_until=case((is_running, Task.blocked_until), else_=None),
                blocked_reason=case((is_running, Task.blocked_reason), else_=None),
            )
        )
        result = self.session.execute(stmt)
        self.session.commit()
        return bool(_rowcount(result))

    def request_cancel_run(self, run_id: int, *, now: datetime | None = None) -> int:
        """Phase 8のRun生成経路から使うRun単位キャンセルのDBインターフェース。"""
        requested_at = now or datetime.now(UTC)
        waiting_result = self.session.execute(
            update(Task)
            .where(Task.run_id == run_id, Task.status.in_(WAITING_STATUSES))
            .values(
                status=TaskStatus.CANCELLED,
                finished_at=requested_at,
                cancel_requested=False,
                worker_id=None,
                started_at=None,
                heartbeat_at=None,
            )
        )
        running_result = self.session.execute(
            update(Task)
            .where(Task.run_id == run_id, Task.status == TaskStatus.RUNNING)
            .values(cancel_requested=True)
        )
        self.session.commit()
        return _rowcount(waiting_result) + _rowcount(running_result)

    def retry(self, task_id: int, *, now: datetime | None = None) -> bool:
        stmt = (
            update(Task)
            .where(Task.id == task_id, Task.status != TaskStatus.RUNNING)
            .values(
                status=TaskStatus.PENDING,
                attempts=0,
                available_at=now or datetime.now(UTC),
                blocked_until=None,
                blocked_reason=None,
                cancel_requested=False,
                finished_at=None,
                error_message=None,
                worker_id=None,
                started_at=None,
                heartbeat_at=None,
            )
        )
        result = self.session.execute(stmt)
        self.session.commit()
        return bool(_rowcount(result))

    def cancel_descendants(self, task_id: int, *, now: datetime | None = None) -> int:
        """失敗した依存元より後ろの未実行Taskを全てcancelledにする。"""
        cancelled_at = now or datetime.now(UTC)
        frontier = [task_id]
        total = 0
        while frontier:
            child_ids = list(
                self.session.scalars(
                    select(Task.id).where(
                        Task.depends_on_task_id.in_(frontier),
                        Task.status.in_(WAITING_STATUSES),
                    )
                )
            )
            if not child_ids:
                break
            result = self.session.execute(
                update(Task)
                .where(Task.id.in_(child_ids), Task.status.in_(WAITING_STATUSES))
                .values(
                    status=TaskStatus.CANCELLED,
                    finished_at=cancelled_at,
                    cancel_requested=False,
                )
            )
            total += _rowcount(result)
            frontier = child_ids
        self.session.commit()
        return total

    def recover_stale(self, *, stale_before: datetime, now: datetime | None = None) -> list[int]:
        """staleなrunningを回収する。所有者競合を避けるため更新時にも時刻を再検査する。"""
        recovered_at = now or datetime.now(UTC)
        stale_condition = or_(
            Task.heartbeat_at < stale_before,
            and_(Task.heartbeat_at.is_(None), Task.started_at < stale_before),
        )
        candidates = list(
            self.session.scalars(
                select(Task).where(Task.status == TaskStatus.RUNNING, stale_condition)
            )
        )
        recovered: list[int] = []
        terminal_ids: list[int] = []
        for task in candidates:
            attempts = task.attempts + 1
            terminal = attempts >= task.max_attempts
            status = TaskStatus.UNAVAILABLE if terminal else TaskStatus.PENDING
            result = self.session.execute(
                update(Task)
                .where(Task.id == task.id, Task.status == TaskStatus.RUNNING, stale_condition)
                .values(
                    status=status,
                    attempts=attempts,
                    available_at=None if terminal else recovered_at,
                    finished_at=recovered_at if terminal else None,
                    worker_id=None,
                    started_at=None,
                    heartbeat_at=None,
                )
            )
            if _rowcount(result):
                recovered.append(task.id)
                if terminal:
                    terminal_ids.append(task.id)
        self.session.commit()
        for terminal_id in terminal_ids:
            self.cancel_descendants(terminal_id, now=recovered_at)
        return recovered

    def write_progress(
        self,
        task_id: int,
        progress: dict[str, Any],
        *,
        worker_id: str | None = None,
    ) -> bool:
        """payload_json.progressだけを単一UPDATE・短いtransactionで更新する。"""
        patch = json.dumps({"progress": progress}, ensure_ascii=False, separators=(",", ":"))
        stmt = update(Task).where(Task.id == task_id)
        if worker_id is not None:
            stmt = stmt.where(
                Task.status == TaskStatus.RUNNING,
                Task.worker_id == worker_id,
            )
        stmt = stmt.values(
            payload_json=func.json_patch(func.coalesce(Task.payload_json, "{}"), patch)
        )
        result = self.session.execute(stmt)
        self.session.commit()
        return bool(_rowcount(result))

    def _finish(
        self,
        task_id: int,
        worker_id: str,
        *,
        status: TaskStatus,
        now: datetime | None,
        error_message: str | None = None,
    ) -> bool:
        stmt = (
            update(Task)
            .where(
                Task.id == task_id,
                Task.status == TaskStatus.RUNNING,
                Task.worker_id == worker_id,
            )
            .values(
                status=status,
                finished_at=now or datetime.now(UTC),
                worker_id=None,
                heartbeat_at=None,
                cancel_requested=False,
                error_message=error_message,
            )
        )
        result = self.session.execute(stmt)
        self.session.commit()
        return bool(_rowcount(result))

    @staticmethod
    def _clear_ownership(task: Task) -> None:
        task.worker_id = None
        task.started_at = None
        task.heartbeat_at = None


__all__ = ["CLAIMABLE_STATUSES", "TaskRepository", "WAITING_STATUSES"]
