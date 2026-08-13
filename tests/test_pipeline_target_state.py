from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sluicery import cli
from sluicery.db.models import (
    Item,
    LayoutStrategy,
    Playlist,
    PlaylistKindHint,
    PlaylistProfile,
    Profile,
    ProfileKind,
    Storage,
    StorageKind,
    Target,
    TargetStatus,
    Task,
    TaskStatus,
    TaskType,
    WorkerClass,
)
from sluicery.tasks.queue import TaskOutcome, TaskResult
from sluicery.tasks.worker import StaleTaskReaper, Worker, WorkerConfig

NOW = datetime(2026, 8, 13, tzinfo=UTC)


def _config() -> WorkerConfig:
    return WorkerConfig(
        poll_interval_sec=0.01,
        poll_jitter_sec=0,
        heartbeat_interval_sec=0.05,
        stale_threshold_sec=0.16,
        retry_base_sec=1,
        retry_max_sec=10,
        max_attempts=5,
        blocked_retry_sec=30,
        progress_write_interval_sec=2,
        progress_write_percent_step=5,
        shutdown_grace_sec=1,
        enable_test_tasks=False,
    )


def _target_task(
    session_factory,
    *,
    target_status: TargetStatus,
    task_status: TaskStatus = TaskStatus.QUEUED,
    max_attempts: int = 1,
    heartbeat_at: datetime | None = None,
) -> tuple[int, int]:
    with session_factory() as session:
        storage = Storage(name="s", kind=StorageKind.LOCAL, config_json={"path": "out"})
        profile = Profile(name="p", kind=ProfileKind.VIDEO, layout_strategy=LayoutStrategy.FLAT)
        playlist = Playlist(
            name="p", folder_name="p", url="https://example.com", kind_hint=PlaylistKindHint.VIDEO
        )
        session.add_all([storage, profile, playlist])
        session.flush()
        assignment = PlaylistProfile(
            playlist_id=playlist.id, profile_id=profile.id, storage_id=storage.id
        )
        item = Item(playlist_id=playlist.id, source_id="x", source_url="https://example.com/x")
        session.add_all([assignment, item])
        session.flush()
        target = Target(
            item_id=item.id,
            playlist_profile_id=assignment.id,
            status=target_status,
        )
        session.add(target)
        session.flush()
        task = Task(
            type=TaskType.DOWNLOAD,
            target_ref_type="target",
            target_ref_id=target.id,
            payload_json={"target_id": target.id, "work_id": "work"},
            worker_class=WorkerClass.NETWORK,
            status=task_status,
            max_attempts=max_attempts,
            worker_id="dead" if task_status == TaskStatus.RUNNING else None,
            started_at=heartbeat_at,
            heartbeat_at=heartbeat_at,
        )
        session.add(task)
        session.commit()
        return target.id, task.id


class _ResultHandler:
    def __init__(self, outcome: TaskOutcome) -> None:
        self.outcome = outcome

    def run(self, payload, on_progress) -> TaskResult:
        return TaskResult(self.outcome, "reason")

    def cancel(self) -> None:
        pass


def test_retry_exhaustion_updates_target_unavailable_once(session_factory) -> None:
    target_id, task_id = _target_task(
        session_factory, target_status=TargetStatus.DOWNLOADING
    )
    worker = Worker(
        session_factory,
        WorkerClass.NETWORK,
        _config(),
        worker_id="worker:test",
        handler_factories={"download": lambda: _ResultHandler(TaskOutcome.FAILED)},
        clock=lambda: NOW,
        random_fraction=lambda: 0,
    )

    assert worker.run_once()

    with session_factory() as session:
        assert session.get(Task, task_id).status == TaskStatus.UNAVAILABLE
        target = session.get(Target, target_id)
        assert target.status == TargetStatus.UNAVAILABLE
        assert target.retry_count == 1


def test_direct_unavailable_updates_processing_target(session_factory) -> None:
    target_id, task_id = _target_task(
        session_factory, target_status=TargetStatus.PROCESSING
    )
    worker = Worker(
        session_factory,
        WorkerClass.NETWORK,
        _config(),
        worker_id="worker:test",
        handler_factories={"download": lambda: _ResultHandler(TaskOutcome.UNAVAILABLE)},
        clock=lambda: NOW,
    )

    assert worker.run_once()

    with session_factory() as session:
        assert session.get(Task, task_id).status == TaskStatus.UNAVAILABLE
        target = session.get(Target, target_id)
        assert target.status == TargetStatus.UNAVAILABLE
        assert target.retry_count == 0


def test_cancelled_task_returns_target_to_retryable_failed(session_factory) -> None:
    target_id, task_id = _target_task(
        session_factory, target_status=TargetStatus.PROCESSING
    )
    worker = Worker(
        session_factory,
        WorkerClass.NETWORK,
        _config(),
        worker_id="worker:test",
        handler_factories={"download": lambda: _ResultHandler(TaskOutcome.CANCELLED)},
        clock=lambda: NOW,
    )

    assert worker.run_once()

    with session_factory() as session:
        assert session.get(Task, task_id).status == TaskStatus.CANCELLED
        target = session.get(Target, target_id)
        assert target.status == TargetStatus.FAILED
        assert target.retry_count == 0


def test_cli_cancel_waiting_task_updates_target(
    session_factory, monkeypatch, capsys
) -> None:
    target_id, task_id = _target_task(
        session_factory, target_status=TargetStatus.QUEUED
    )
    monkeypatch.setattr(cli, "_open_session", lambda: session_factory())

    assert cli.main(["task", "cancel", str(task_id)]) == 0
    capsys.readouterr()

    with session_factory() as session:
        assert session.get(Task, task_id).status == TaskStatus.CANCELLED
        assert session.get(Target, target_id).status == TargetStatus.FAILED


def test_stale_exhaustion_updates_target_unavailable(session_factory) -> None:
    target_id, task_id = _target_task(
        session_factory,
        target_status=TargetStatus.PROCESSING,
        task_status=TaskStatus.RUNNING,
        heartbeat_at=NOW - timedelta(seconds=10),
    )
    reaper = StaleTaskReaper(session_factory, _config(), clock=lambda: NOW)

    assert reaper.run_once() == [task_id]

    with session_factory() as session:
        assert session.get(Task, task_id).status == TaskStatus.UNAVAILABLE
        assert session.get(Target, target_id).status == TargetStatus.UNAVAILABLE
