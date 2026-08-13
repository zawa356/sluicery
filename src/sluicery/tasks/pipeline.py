"""Target単位の5段パイプライン生成と依存payload参照。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from sluicery.db.models import TargetStatus, Task, TaskStatus, TaskType, WorkerClass
from sluicery.db.repositories.target import TargetRepository


@dataclass(frozen=True)
class PipelineTasks:
    work_id: str
    download: Task
    verify: Task
    postprocess: Task
    publish: Task
    index: Task

    @property
    def all(self) -> tuple[Task, ...]:
        return (self.download, self.verify, self.postprocess, self.publish, self.index)


PIPELINE_STEPS: tuple[tuple[TaskType, WorkerClass], ...] = (
    (TaskType.DOWNLOAD, WorkerClass.NETWORK),
    (TaskType.VERIFY, WorkerClass.COMPUTE),
    (TaskType.POSTPROCESS, WorkerClass.COMPUTE),
    (TaskType.PUBLISH, WorkerClass.NETWORK),
    (TaskType.INDEX, WorkerClass.NETWORK),
)


def enqueue_pipeline(
    session: Session,
    target_id: int,
    *,
    run_id: int | None = None,
    work_id: str | None = None,
    max_attempts: int = 5,
) -> PipelineTasks:
    """チェーン全体を同一transactionで投入する。"""
    resolved_work_id = work_id or f"target-{target_id}-{uuid4().hex}"
    tasks: list[Task] = []
    dependency: Task | None = None
    for task_type, worker_class in PIPELINE_STEPS:
        task = Task(
            type=task_type,
            target_ref_type="target",
            target_ref_id=target_id,
            payload_json={"work_id": resolved_work_id, "target_id": target_id},
            worker_class=worker_class,
            status=TaskStatus.QUEUED,
            depends_on_task_id=dependency.id if dependency is not None else None,
            max_attempts=max_attempts,
            run_id=run_id,
        )
        session.add(task)
        session.flush()
        tasks.append(task)
        dependency = task
    session.commit()
    for task in tasks:
        session.refresh(task)
    return PipelineTasks(resolved_work_id, *tasks)


def enqueue_target_pipeline(
    session: Session,
    target_id: int,
    *,
    run_id: int | None = None,
    work_id: str | None = None,
    max_attempts: int = 5,
) -> PipelineTasks | None:
    """pending Target の queued 遷移と5 Task生成を同じtransactionで確定する。"""
    if not TargetRepository(session).compare_and_set_status(
        target_id,
        {TargetStatus.PENDING},
        TargetStatus.QUEUED,
        commit=False,
    ):
        session.rollback()
        return None
    try:
        return enqueue_pipeline(
            session,
            target_id,
            run_id=run_id,
            work_id=work_id,
            max_attempts=max_attempts,
        )
    except Exception:
        session.rollback()
        raise


def dependency_payload(session: Session, task_id: int) -> dict[str, Any]:
    """Taskの直前依存が成功済みであることを確認してpayloadを返す。"""
    task = session.get(Task, task_id)
    if task is None:
        raise LookupError(f"Task {task_id} が見つかりません")
    if task.depends_on_task_id is None:
        return {}
    dependency = session.get(Task, task.depends_on_task_id)
    if dependency is None:
        raise LookupError(f"依存Task {task.depends_on_task_id} が見つかりません")
    if dependency.status != TaskStatus.SUCCEEDED:
        raise RuntimeError(f"依存Task {dependency.id} は成功していません")
    return dict(dependency.payload_json or {})


def execution_task_id(payload: dict[str, Any]) -> int:
    execution = payload.get("_execution")
    if not isinstance(execution, dict) or not isinstance(execution.get("task_id"), int):
        raise ValueError("Task実行コンテキストがありません")
    return execution["task_id"]


__all__ = [
    "PIPELINE_STEPS",
    "PipelineTasks",
    "dependency_payload",
    "enqueue_pipeline",
    "enqueue_target_pipeline",
    "execution_task_id",
]
