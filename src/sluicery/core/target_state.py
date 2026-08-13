"""Item / Target 状態遷移のドメインルール。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from sluicery.db.models import ItemMembership, TargetStatus, Task, TaskStatus
from sluicery.db.repositories.item import ItemRepository
from sluicery.db.repositories.target import TargetRepository


class InvalidStateTransition(ValueError):
    """許可されていないドメイン状態遷移。"""


class StateTransitionConflict(RuntimeError):
    """状態を読み取った後、CAS更新前に他の処理が更新した。"""


_TARGET_NORMAL_TRANSITIONS: dict[TargetStatus, set[TargetStatus]] = {
    TargetStatus.PENDING: {TargetStatus.QUEUED},
    TargetStatus.QUEUED: {TargetStatus.DOWNLOADING},
    TargetStatus.DOWNLOADING: {TargetStatus.PROCESSING},
    TargetStatus.PROCESSING: {TargetStatus.DOWNLOADED},
    TargetStatus.FAILED: {TargetStatus.PENDING},
    TargetStatus.BLOCKED: {TargetStatus.PENDING},
    TargetStatus.DOWNLOADED: {TargetStatus.MISSING},
    TargetStatus.UNAVAILABLE: set(),
    TargetStatus.MISSING: set(),
    TargetStatus.IGNORED: set(),
}

# 指示書§15「任意状態からの終端/保留遷移」。同一状態への更新も
# エラー詳細や blocked_reason の更新に利用するため許可する。
_TARGET_ANY_TRANSITIONS = {
    TargetStatus.FAILED,
    TargetStatus.UNAVAILABLE,
    TargetStatus.BLOCKED,
    TargetStatus.IGNORED,
}


def transition_target(
    session: Session,
    target_id: int,
    status: TargetStatus,
    *,
    error: str | None = None,
    blocked_reason: str | None = None,
    increment_retry: bool = False,
    extra_values: dict[str, object] | None = None,
    commit: bool = True,
) -> bool:
    """遷移表を検証し、読み取った現在状態を所有権とするCASで更新する。"""
    target = TargetRepository(session).get(target_id)
    if target is None:
        raise LookupError(f"Target {target_id} が見つかりません")
    current = target.status
    allowed = _TARGET_NORMAL_TRANSITIONS[current] | _TARGET_ANY_TRANSITIONS
    if status not in allowed:
        raise InvalidStateTransition(f"Target: {current.value} -> {status.value}")
    changed = TargetRepository(session).compare_and_set_status(
        target_id,
        {current},
        status,
        error=error,
        blocked_reason=blocked_reason,
        increment_retry=increment_retry,
        extra_values=extra_values,
        commit=commit,
    )
    if not changed:
        session.rollback()
        raise StateTransitionConflict(f"Target {target_id} の状態が同時に更新されました")
    return True


def transition_item(
    session: Session,
    item_id: int,
    membership: ItemMembership,
    *,
    commit: bool = True,
) -> bool:
    """Item membership の active <-> delisted だけをCASで更新する。"""
    item = ItemRepository(session).get(item_id)
    if item is None:
        raise LookupError(f"Item {item_id} が見つかりません")
    current = item.membership
    if current == membership:
        raise InvalidStateTransition(f"Item: {current.value} -> {membership.value}")
    changed = ItemRepository(session).compare_and_set_membership(
        item_id, {current}, membership, commit=commit
    )
    if not changed:
        session.rollback()
        raise StateTransitionConflict(f"Item {item_id} のmembershipが同時に更新されました")
    return True


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


__all__ = [
    "InvalidStateTransition",
    "StateTransitionConflict",
    "sync_target_after_task",
    "transition_item",
    "transition_target",
]
