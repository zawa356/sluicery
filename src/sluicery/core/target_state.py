"""Taskの確定結果をTargetへ安全側に同期する補助処理。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from sluicery.db.models import TargetStatus, Task, TaskStatus
from sluicery.db.repositories.target import TargetRepository

_ACTIVE_TARGET_STATUSES = {
    TargetStatus.PENDING,
    TargetStatus.QUEUED,
    TargetStatus.DOWNLOADING,
    TargetStatus.PROCESSING,
    TargetStatus.BLOCKED,
}


def sync_target_after_task(
    session: Session,
    task: Task,
    task_status: TaskStatus,
    *,
    error: str | None = None,
    failed_attempt: bool = False,
) -> bool:
    """所有権付きTask更新後に、必要な終端・保留状態だけTargetへ反映する。

    handlerが既にTargetを更新している場合はCASが不一致となるため、retry_countを
    二重加算しない。shutdown / staleの再実行可能なpendingには作用しない。
    """
    if task.target_ref_type != "target":
        return False
    repo = TargetRepository(session)
    if task_status == TaskStatus.UNAVAILABLE:
        if failed_attempt and repo.compare_and_set_status(
            task.target_ref_id,
            {TargetStatus.FAILED},
            TargetStatus.UNAVAILABLE,
            error=error or "Taskが再試行不能になりました",
        ):
            return True
        return repo.compare_and_set_status(
            task.target_ref_id,
            _ACTIVE_TARGET_STATUSES,
            TargetStatus.UNAVAILABLE,
            error=error or "Taskが再試行不能になりました",
            increment_retry=failed_attempt,
        )
    if task_status == TaskStatus.CANCELLED:
        return repo.compare_and_set_status(
            task.target_ref_id,
            _ACTIVE_TARGET_STATUSES,
            TargetStatus.FAILED,
            error=error or "Taskがキャンセルされました",
        )
    if task_status == TaskStatus.BLOCKED:
        return repo.compare_and_set_status(
            task.target_ref_id,
            _ACTIVE_TARGET_STATUSES - {TargetStatus.BLOCKED},
            TargetStatus.BLOCKED,
            error=error,
            blocked_reason=error,
        )
    if task_status == TaskStatus.PENDING and failed_attempt:
        return repo.compare_and_set_status(
            task.target_ref_id,
            _ACTIVE_TARGET_STATUSES - {TargetStatus.BLOCKED},
            TargetStatus.FAILED,
            error=error,
            increment_retry=True,
        )
    return False


__all__ = ["sync_target_after_task"]
