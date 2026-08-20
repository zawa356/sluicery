from __future__ import annotations

import threading

from sluicery.db.models import TaskStatus, TaskType, WorkerClass
from sluicery.db.repositories.task import TaskRepository


def _make_queued_task(session_factory, *, target_ref_id: int) -> int:
    session = session_factory()
    task = TaskRepository(session).create(
        type=TaskType.DOWNLOAD,
        target_ref_type="target",
        target_ref_id=target_ref_id,
        worker_class=WorkerClass.NETWORK,
        status=TaskStatus.QUEUED,
    )
    task_id = task.id
    session.close()
    return task_id


def test_claim_next_single_task_not_double_claimed(session_factory) -> None:
    task_id = _make_queued_task(session_factory, target_ref_id=1)

    claimed: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        session = session_factory()
        task = TaskRepository(session).claim_next(WorkerClass.NETWORK)
        if task is not None:
            with lock:
                claimed.append(task.id)
        session.close()

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert claimed == [task_id]


def test_claim_next_many_tasks_each_claimed_exactly_once(session_factory) -> None:
    task_count = 10
    ids = [_make_queued_task(session_factory, target_ref_id=i) for i in range(task_count)]

    results: list[int | None] = []
    lock = threading.Lock()

    def worker() -> None:
        session = session_factory()
        task = TaskRepository(session).claim_next(WorkerClass.NETWORK)
        with lock:
            results.append(task.id if task is not None else None)
        session.close()

    threads = [threading.Thread(target=worker) for _ in range(task_count * 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    claimed = [r for r in results if r is not None]
    assert len(claimed) == task_count
    assert len(set(claimed)) == task_count
    assert set(claimed) == set(ids)


def test_claim_next_returns_none_when_nothing_queued(session_factory) -> None:
    session = session_factory()
    task = TaskRepository(session).claim_next(WorkerClass.NETWORK)
    assert task is None
    session.close()


def test_claim_next_respects_worker_class(session_factory) -> None:
    session = session_factory()
    TaskRepository(session).create(
        type=TaskType.POSTPROCESS,
        target_ref_type="target",
        target_ref_id=1,
        worker_class=WorkerClass.COMPUTE,
        status=TaskStatus.QUEUED,
    )
    session.close()

    session2 = session_factory()
    task = TaskRepository(session2).claim_next(WorkerClass.NETWORK)
    assert task is None
    session2.close()


def test_claim_next_applies_global_download_item_concurrency(session_factory) -> None:
    first_id = _make_queued_task(session_factory, target_ref_id=1)
    second_id = _make_queued_task(session_factory, target_ref_id=2)
    with session_factory() as session:
        first = TaskRepository(session).claim_next(
            WorkerClass.NETWORK, item_concurrency=1
        )
        assert first is not None and first.id == first_id
    with session_factory() as session:
        assert (
            TaskRepository(session).claim_next(
                WorkerClass.NETWORK, item_concurrency=1
            )
            is None
        )
        second = TaskRepository(session).claim_next(
            WorkerClass.NETWORK, item_concurrency=2
        )
        assert second is not None and second.id == second_id
