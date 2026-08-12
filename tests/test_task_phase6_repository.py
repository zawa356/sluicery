from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sluicery.db.models import Run, RunStatus, RunTrigger, TaskStatus, TaskType, WorkerClass
from sluicery.db.repositories.task import TaskRepository

NOW = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)


def _task(repo: TaskRepository, **overrides):
    values = {
        "type": TaskType.NOOP,
        "target_ref_type": "phase6_test",
        "target_ref_id": 1,
        "worker_class": WorkerClass.NETWORK,
        "status": TaskStatus.PENDING,
        "max_attempts": 3,
    }
    values.update(overrides)
    return repo.create(**values)


def test_claim_respects_time_dependency_and_records_owner(db_session) -> None:
    repo = TaskRepository(db_session)
    _task(repo, target_ref_id=1, available_at=NOW + timedelta(seconds=1), priority=100)
    _task(repo, target_ref_id=2, blocked_until=NOW + timedelta(seconds=1), priority=90)
    dependency = _task(repo, target_ref_id=3, status=TaskStatus.RUNNING)
    _task(repo, target_ref_id=4, depends_on_task_id=dependency.id, priority=80)
    succeeded = _task(repo, target_ref_id=5, status=TaskStatus.SUCCEEDED)
    expected = _task(repo, target_ref_id=6, depends_on_task_id=succeeded.id, priority=70)

    claimed = repo.claim_next(WorkerClass.NETWORK, worker_id="network:test:1", now=NOW)

    assert claimed is not None
    assert claimed.id == expected.id
    assert claimed.status == TaskStatus.RUNNING
    assert claimed.worker_id == "network:test:1"
    assert claimed.started_at == NOW
    assert claimed.heartbeat_at == NOW


def test_elapsed_blocked_task_is_claimed_without_consuming_attempt(db_session) -> None:
    repo = TaskRepository(db_session)
    task = _task(
        repo,
        status=TaskStatus.BLOCKED,
        attempts=2,
        blocked_until=NOW - timedelta(seconds=1),
        blocked_reason="storage unreachable",
    )

    claimed = repo.claim_next(WorkerClass.NETWORK, worker_id="worker", now=NOW)

    assert claimed is not None and claimed.id == task.id
    assert claimed.attempts == 2
    assert claimed.blocked_until is None
    assert claimed.blocked_reason is None


def test_failed_retries_with_backoff_then_becomes_unavailable(db_session) -> None:
    repo = TaskRepository(db_session)
    task = _task(repo, status=TaskStatus.RUNNING, worker_id="worker", attempts=1, max_attempts=3)

    status = repo.mark_failed_for_retry(
        task.id,
        "worker",
        retry_delay_sec=120,
        error_message="temporary",
        now=NOW,
    )
    db_session.refresh(task)
    assert status == TaskStatus.PENDING
    assert task.attempts == 2
    assert task.available_at == NOW + timedelta(seconds=120)
    assert task.worker_id is None

    task.status = TaskStatus.RUNNING
    task.worker_id = "worker"
    db_session.commit()
    status = repo.mark_failed_for_retry(
        task.id,
        "worker",
        retry_delay_sec=240,
        now=NOW,
    )
    db_session.refresh(task)
    assert status == TaskStatus.UNAVAILABLE
    assert task.attempts == 3
    assert task.available_at is None


def test_blocked_does_not_increment_attempts(db_session) -> None:
    repo = TaskRepository(db_session)
    task = _task(repo, status=TaskStatus.RUNNING, worker_id="worker", attempts=2)

    assert repo.mark_blocked(
        task.id,
        "worker",
        retry_after_sec=300,
        reason="external",
        now=NOW,
    )
    db_session.refresh(task)
    assert task.status == TaskStatus.BLOCKED
    assert task.attempts == 2
    assert task.blocked_until == NOW + timedelta(seconds=300)


def test_terminal_dependency_failure_cancels_all_descendants(db_session) -> None:
    repo = TaskRepository(db_session)
    parent = _task(repo, status=TaskStatus.RUNNING, worker_id="worker")
    child = _task(repo, depends_on_task_id=parent.id, status=TaskStatus.QUEUED)
    grandchild = _task(repo, depends_on_task_id=child.id)

    assert repo.mark_failed(parent.id, "worker", now=NOW)
    db_session.refresh(child)
    db_session.refresh(grandchild)
    assert child.status == TaskStatus.CANCELLED
    assert grandchild.status == TaskStatus.CANCELLED


def test_stale_owner_cannot_overwrite_progress(db_session) -> None:
    repo = TaskRepository(db_session)
    task = _task(repo, status=TaskStatus.RUNNING, worker_id="new-worker")

    assert not repo.write_progress(task.id, {"percent": 90}, worker_id="stale-worker")
    db_session.refresh(task)
    assert task.payload_json is None


def test_heartbeat_updates_and_returns_cancel_flag(db_session) -> None:
    repo = TaskRepository(db_session)
    task = _task(repo, status=TaskStatus.RUNNING, worker_id="worker")

    assert repo.heartbeat(task.id, "worker", now=NOW) is False
    task.cancel_requested = True
    db_session.commit()
    assert repo.heartbeat(task.id, "worker", now=NOW + timedelta(seconds=1)) is True


def test_stale_recovery_increments_attempts_but_recent_task_is_untouched(db_session) -> None:
    repo = TaskRepository(db_session)
    stale = _task(
        repo,
        target_ref_id=1,
        status=TaskStatus.RUNNING,
        worker_id="dead-worker",
        heartbeat_at=NOW - timedelta(seconds=181),
    )
    recent = _task(
        repo,
        target_ref_id=2,
        status=TaskStatus.RUNNING,
        worker_id="live-worker",
        heartbeat_at=NOW - timedelta(seconds=179),
    )

    assert repo.recover_stale(stale_before=NOW - timedelta(seconds=180), now=NOW) == [stale.id]
    db_session.refresh(stale)
    db_session.refresh(recent)
    assert stale.status == TaskStatus.PENDING
    assert stale.attempts == 1
    assert stale.worker_id is None
    assert recent.status == TaskStatus.RUNNING


def test_cancel_waiting_and_run_interface(db_session) -> None:
    repo = TaskRepository(db_session)
    run = Run(trigger=RunTrigger.MANUAL, kind="phase6_test", status=RunStatus.RUNNING)
    db_session.add(run)
    db_session.commit()
    waiting = _task(repo, run_id=run.id)
    running = _task(repo, status=TaskStatus.RUNNING, worker_id="worker", run_id=run.id)

    assert repo.request_cancel_run(run.id, now=NOW) == 2
    db_session.refresh(waiting)
    db_session.refresh(running)
    assert waiting.status == TaskStatus.CANCELLED
    assert running.cancel_requested is True


def test_progress_is_merged_into_payload_with_single_update(db_session) -> None:
    repo = TaskRepository(db_session)
    task = _task(repo, payload_json={"sec": 30})

    assert repo.write_progress(task.id, {"status": "running", "percent": 25.0})
    db_session.refresh(task)
    assert task.payload_json == {
        "sec": 30,
        "progress": {"status": "running", "percent": 25.0},
    }
