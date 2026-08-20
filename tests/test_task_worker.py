from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sluicery.db.models import (
    Run,
    RunStatus,
    RunTrigger,
    Task,
    TaskStatus,
    TaskType,
    WorkerClass,
)
from sluicery.db.repositories.task import TaskRepository
from sluicery.tasks.handlers.dummy import DUMMY_HANDLER_FACTORIES
from sluicery.tasks.queue import TaskOutcome, TaskResult
from sluicery.tasks.worker import (
    StaleTaskReaper,
    Worker,
    WorkerConfig,
    cleanup_expired_run_logs,
    make_worker_id,
)

NOW = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)


class _Hook:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


class _ParallelProbeHandler:
    def __init__(
        self,
        started: threading.Barrier,
        release: threading.Event,
        active: list[int],
        maximum: list[int],
        lock: threading.Lock,
    ) -> None:
        self._started = started
        self._release = release
        self._active = active
        self._maximum = maximum
        self._lock = lock

    @property
    def log_paths(self) -> tuple[Path, ...]:
        return ()

    def cancel(self) -> None:
        self._release.set()

    def run(self, payload: dict, on_progress) -> TaskResult:
        del payload, on_progress
        with self._lock:
            self._active[0] += 1
            self._maximum[0] = max(self._maximum[0], self._active[0])
        self._started.wait(timeout=3)
        self._release.wait(timeout=3)
        with self._lock:
            self._active[0] -= 1
        return TaskResult(TaskOutcome.SUCCEEDED)


def _config(**overrides) -> WorkerConfig:
    values = {
        "poll_interval_sec": 0.01,
        "poll_jitter_sec": 0,
        "heartbeat_interval_sec": 0.05,
        "stale_threshold_sec": 0.16,
        "retry_base_sec": 60,
        "retry_max_sec": 3600,
        "max_attempts": 5,
        "blocked_retry_sec": 300,
        "blocked_retry_403_sec": 3600,
        "progress_write_interval_sec": 2,
        "progress_write_percent_step": 5,
        "shutdown_grace_sec": 1,
        "enable_test_tasks": True,
    }
    values.update(overrides)
    return WorkerConfig(**values)


def _enqueue(session_factory, task_type: TaskType, **overrides) -> int:
    with session_factory() as session:
        values = {
            "type": task_type,
            "target_ref_type": "phase6_test",
            "target_ref_id": 1,
            "worker_class": WorkerClass.NETWORK,
            "status": TaskStatus.QUEUED,
            "max_attempts": 5,
            "payload_json": {},
        }
        values.update(overrides)
        return TaskRepository(session).create(**values).id


def test_log_retention_deletes_only_expired_finished_run_log(
    session_factory, tmp_path: Path
) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    with session_factory() as session:
        old = Run(
            trigger=RunTrigger.MANUAL,
            kind="download",
            status=RunStatus.SUCCEEDED,
            finished_at=NOW - timedelta(days=31),
        )
        recent = Run(
            trigger=RunTrigger.MANUAL,
            kind="download",
            status=RunStatus.SUCCEEDED,
            finished_at=NOW - timedelta(days=1),
        )
        session.add_all([old, recent])
        session.flush()
        old_path = log_dir / f"run-{old.id}.log"
        recent_path = log_dir / f"run-{recent.id}.log"
        old_path.write_text("old\n", encoding="utf-8")
        recent_path.write_text("recent\n", encoding="utf-8")
        old.log_path = str(old_path)
        recent.log_path = str(recent_path)
        retention = Run(
            trigger=RunTrigger.MANUAL,
            kind="retention",
            status=RunStatus.SUCCEEDED,
            finished_at=NOW - timedelta(days=31),
        )
        unfinished = Run(
            trigger=RunTrigger.MANUAL,
            kind="retention",
            status=RunStatus.FAILED,
            finished_at=NOW - timedelta(days=31),
        )
        active_run = Run(
            trigger=RunTrigger.MANUAL,
            kind="download",
            status=RunStatus.FAILED,
            finished_at=NOW - timedelta(days=31),
        )
        session.add_all([retention, unfinished, active_run])
        session.flush()
        retention_path = log_dir / f"retention-{'a' * 32}.log"
        unfinished_path = log_dir / f"retention-{'b' * 32}.log"
        intent = {"event": "delete_intent", "artifact_id": 1, "path": "a.mkv"}
        deleted = {"event": "deleted", "artifact_id": 1, "path": "a.mkv"}
        retention_path.write_text(
            json.dumps(intent) + "\n" + json.dumps(deleted) + "\n",
            encoding="utf-8",
        )
        unfinished_path.write_text(json.dumps(intent) + "\n", encoding="utf-8")
        retention.log_path = str(retention_path)
        unfinished.log_path = str(unfinished_path)
        active_path = log_dir / f"run-{active_run.id}.log"
        active_path.write_text("still active\n", encoding="utf-8")
        active_run.log_path = str(active_path)
        session.add(
            Task(
                type=TaskType.DOWNLOAD,
                target_ref_type="target",
                target_ref_id=999,
                worker_class=WorkerClass.NETWORK,
                status=TaskStatus.RUNNING,
                run_id=active_run.id,
            )
        )
        session.commit()
        old_id = old.id
        recent_id = recent.id

    assert cleanup_expired_run_logs(
        session_factory, log_dir, retention_days=30, now=NOW
    ) == 2
    assert not old_path.exists()
    assert not retention_path.exists()
    assert unfinished_path.exists()
    assert active_path.exists()
    assert recent_path.exists()
    with session_factory() as session:
        assert session.get(Run, old_id).log_path is None
        assert session.get(Run, recent_id).log_path == str(recent_path)


def test_network_worker_runs_downloads_in_parallel_when_configured(
    session_factory,
) -> None:
    first_id = _enqueue(session_factory, TaskType.DOWNLOAD)
    second_id = _enqueue(session_factory, TaskType.DOWNLOAD, target_ref_id=2)
    started = threading.Barrier(3)
    release = threading.Event()
    active = [0]
    maximum = [0]
    lock = threading.Lock()

    def factory() -> _ParallelProbeHandler:
        return _ParallelProbeHandler(started, release, active, maximum, lock)

    worker = Worker(
        session_factory,
        WorkerClass.NETWORK,
        _config(item_concurrency=2),
        handler_factories={TaskType.DOWNLOAD.value: factory},
        hook=_Hook(),
    )
    thread = threading.Thread(
        target=worker.run,
        kwargs={"install_signal_handlers": False},
        daemon=True,
    )
    thread.start()
    started.wait(timeout=3)
    assert maximum[0] == 2
    release.set()
    for _index in range(100):
        with session_factory() as session:
            states = {
                session.get(Task, first_id).status,
                session.get(Task, second_id).status,
            }
        if states == {TaskStatus.SUCCEEDED}:
            break
        threading.Event().wait(0.01)
    worker.request_shutdown()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert states == {TaskStatus.SUCCEEDED}


def test_stale_terminal_discover_finishes_its_run(session_factory) -> None:
    with session_factory() as session:
        run = Run(trigger=RunTrigger.SCHEDULE, kind="discover", status=RunStatus.RUNNING)
        session.add(run)
        session.commit()
        task_id = _enqueue(
            session_factory,
            TaskType.DISCOVER,
            target_ref_type="playlist",
            status=TaskStatus.RUNNING,
            worker_id="gone",
            started_at=NOW - timedelta(seconds=200),
            heartbeat_at=NOW - timedelta(seconds=200),
            max_attempts=1,
            run_id=run.id,
        )
        run_id = run.id

    hook = _Hook()
    reaper = StaleTaskReaper(
        session_factory, _config(), hook=hook, clock=lambda: NOW
    )

    assert reaper.run_once() == [task_id]
    with session_factory() as session:
        run = session.get(Run, run_id)
        assert run is not None and run.status == RunStatus.FAILED
        assert run.stats_json is not None
        assert run.stats_json["stale_recovered"] is True
    assert [event_type for event_type, _payload in hook.events] == ["run_failed"]


def test_terminal_target_failure_emits_hook(session_factory) -> None:
    hook = _Hook()
    task_id = _enqueue(
        session_factory,
        TaskType.FAIL_UNAVAILABLE,
        target_ref_type="target",
        target_ref_id=42,
    )
    worker = Worker(
        session_factory,
        WorkerClass.NETWORK,
        _config(),
        worker_id="worker:test:target-failure",
        hook=hook,
        clock=lambda: NOW,
    )

    assert worker.run_once()
    assert hook.events == [
        (
            "target_failed",
            {"target_id": 42, "task_id": task_id, "reason_code": "unavailable"},
        )
    ]


@pytest.mark.parametrize(
    ("task_type", "expected_status", "expected_attempts", "progress_status"),
    [
        (TaskType.NOOP, TaskStatus.SUCCEEDED, 0, "succeeded"),
        (TaskType.FAIL, TaskStatus.PENDING, 1, "failed"),
        (TaskType.FAIL_UNAVAILABLE, TaskStatus.UNAVAILABLE, 0, "unavailable"),
        (TaskType.FAIL_BLOCKED, TaskStatus.BLOCKED, 0, "blocked"),
    ],
)
def test_worker_applies_dummy_result_classification(
    session_factory, task_type, expected_status, expected_attempts, progress_status
) -> None:
    task_id = _enqueue(session_factory, task_type)
    worker = Worker(
        session_factory,
        WorkerClass.NETWORK,
        _config(),
        worker_id="worker:test:1",
        clock=lambda: NOW,
        random_fraction=lambda: 0,
    )

    assert worker.run_once()

    with session_factory() as session:
        task = TaskRepository(session).get(task_id)
        assert task is not None
        assert task.status == expected_status
        assert task.attempts == expected_attempts
        assert task.payload_json is not None
        assert task.payload_json["progress"]["status"] == progress_status


def test_worker_enforces_task_max_attempts(session_factory) -> None:
    task_id = _enqueue(session_factory, TaskType.FAIL, max_attempts=1)
    worker = Worker(
        session_factory,
        WorkerClass.NETWORK,
        _config(),
        worker_id="worker:test:1",
        clock=lambda: NOW,
        random_fraction=lambda: 0,
    )

    worker.run_once()

    with session_factory() as session:
        task = TaskRepository(session).get(task_id)
        assert task is not None
        assert task.status == TaskStatus.UNAVAILABLE
        assert task.attempts == 1


def test_worker_uses_longer_delay_for_http_403_without_consuming_attempts(
    session_factory,
) -> None:
    class _Http403Handler:
        def run(self, payload, on_progress) -> TaskResult:
            return TaskResult(
                TaskOutcome.BLOCKED,
                "HTTP 403",
                reason_code="http_403",
            )

        def cancel(self) -> None:
            pass

    task_id = _enqueue(session_factory, TaskType.DOWNLOAD)
    worker = Worker(
        session_factory,
        WorkerClass.NETWORK,
        _config(),
        worker_id="worker:test:http403",
        handler_factories={"download": _Http403Handler},
        clock=lambda: NOW,
    )

    assert worker.run_once()

    with session_factory() as session:
        task = TaskRepository(session).get(task_id)
        assert task is not None
        assert task.status == TaskStatus.BLOCKED
        assert task.attempts == 0
        assert task.blocked_until == NOW + timedelta(seconds=3600)


def test_worker_persists_handler_payload_update(session_factory) -> None:
    class _ResultHandler:
        def run(self, payload, on_progress) -> TaskResult:
            assert payload["_execution"]["task_id"] == task_id
            return TaskResult(TaskOutcome.SUCCEEDED, payload_update={"file_path": "/tmp/a"})

        def cancel(self) -> None:
            pass

    task_id = _enqueue(session_factory, TaskType.NOOP, payload_json={"work_id": "work"})
    worker = Worker(
        session_factory,
        WorkerClass.NETWORK,
        _config(),
        worker_id="worker:test:payload",
        handler_factories={"noop": _ResultHandler},
        clock=lambda: NOW,
    )

    assert worker.run_once()
    with session_factory() as session:
        task = TaskRepository(session).get(task_id)
        assert task is not None
        assert task.payload_json is not None
        assert task.payload_json["work_id"] == "work"
        assert task.payload_json["file_path"] == "/tmp/a"
        assert task.payload_json["progress"]["status"] == "succeeded"


def test_worker_aggregates_masked_external_logs_for_run(session_factory, env_data_dirs) -> None:
    log_dir = env_data_dirs["DATA_DIR"] / "logs"
    log_dir.mkdir()
    source_log = log_dir / "runner.log"
    source_log.write_text("password=worker-secret\nnormal line\n", encoding="utf-8")

    class _LoggedHandler:
        @property
        def log_paths(self) -> tuple[Path, ...]:
            return (source_log,)

        def run(self, payload, on_progress) -> TaskResult:
            return TaskResult(TaskOutcome.SUCCEEDED)

        def cancel(self) -> None:
            pass

    with session_factory() as session:
        run = Run(
            trigger=RunTrigger.MANUAL,
            kind="download",
            status=RunStatus.SUCCEEDED,
        )
        session.add(run)
        session.commit()
        run_id = run.id
    task_id = _enqueue(session_factory, TaskType.NOOP, run_id=run_id)
    worker = Worker(
        session_factory,
        WorkerClass.NETWORK,
        _config(),
        worker_id="worker:test:log",
        handler_factories={"noop": _LoggedHandler},
        clock=lambda: NOW,
    )

    assert worker.run_once()

    with session_factory() as session:
        run = session.get(Run, run_id)
        task = TaskRepository(session).get(task_id)
        assert run is not None and run.log_path is not None
        assert task is not None and task.log_excerpt is not None
        aggregate = Path(run.log_path)
    text = aggregate.read_text(encoding="utf-8")
    assert "worker-secret" not in text
    assert "password=********" in text
    assert "normal line" in text


class _BlockingHandler:
    def __init__(self, started: threading.Event, cancelled: threading.Event) -> None:
        self._started = started
        self._cancelled = cancelled

    def run(self, payload, on_progress) -> TaskResult:
        self._started.set()
        assert self._cancelled.wait(timeout=2)
        return TaskResult(TaskOutcome.CANCELLED)

    def cancel(self) -> None:
        self._cancelled.set()


def test_graceful_shutdown_releases_running_task_without_attempt(session_factory) -> None:
    task_id = _enqueue(session_factory, TaskType.SLEEP)
    started = threading.Event()
    cancelled = threading.Event()
    worker = Worker(
        session_factory,
        WorkerClass.NETWORK,
        _config(),
        worker_id="worker:test:shutdown",
        handler_factories={"sleep": lambda: _BlockingHandler(started, cancelled)},
        clock=lambda: NOW,
    )
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    assert started.wait(timeout=2)

    worker.request_shutdown()
    thread.join(timeout=2)

    assert not thread.is_alive()
    with session_factory() as session:
        task = TaskRepository(session).get(task_id)
        assert task is not None
        assert task.status == TaskStatus.PENDING
        assert task.attempts == 0
        assert task.worker_id is None
        assert task.started_at is None
        assert task.heartbeat_at is None
        reclaimed = TaskRepository(session).claim_next(
            WorkerClass.NETWORK, worker_id="worker:test:restart", now=NOW
        )
        assert reclaimed is not None and reclaimed.id == task_id


def test_cancel_request_is_detected_by_heartbeat(session_factory) -> None:
    task_id = _enqueue(session_factory, TaskType.SLEEP)
    started = threading.Event()
    cancelled = threading.Event()
    worker = Worker(
        session_factory,
        WorkerClass.NETWORK,
        _config(),
        worker_id="worker:test:cancel",
        handler_factories={"sleep": lambda: _BlockingHandler(started, cancelled)},
    )
    thread = threading.Thread(target=worker.run_once)
    thread.start()
    assert started.wait(timeout=2)
    with session_factory() as session:
        assert TaskRepository(session).request_cancel(task_id)

    assert cancelled.wait(timeout=2)
    thread.join(timeout=2)

    with session_factory() as session:
        task = TaskRepository(session).get(task_id)
        assert task is not None
        assert task.status == TaskStatus.CANCELLED
        assert task.worker_id is None


def test_spawn_dummy_handler_completes_without_orphan_on_normal_exit() -> None:
    handler = DUMMY_HANDLER_FACTORIES["spawn"]()
    result = handler.run({"sec": 0}, lambda _progress: None)
    assert result.outcome == TaskOutcome.SUCCEEDED


def test_worker_id_is_unique_across_restarts() -> None:
    first = make_worker_id(WorkerClass.NETWORK)
    second = make_worker_id(WorkerClass.NETWORK)
    assert first != second
    assert first.startswith("worker-network:")
