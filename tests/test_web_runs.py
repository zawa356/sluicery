from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from sluicery.config import Settings
from sluicery.db.models import (
    Item,
    Playlist,
    PlaylistKindHint,
    PlaylistProfile,
    Profile,
    Run,
    RunStatus,
    RunTrigger,
    Storage,
    StorageKind,
    Target,
    TargetStatus,
    Task,
    TaskStatus,
    TaskType,
    WorkerClass,
)
from sluicery.db.repositories.run import RunRepository
from sluicery.web.app import create_app
from sluicery.web.auth import ensure_initial_user


def _csrf(response) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def _client(base_env, session_factory) -> TestClient:
    settings = Settings()
    settings.ADMIN_PASSWORD = "correct-password"
    ensure_initial_user(session_factory, settings)
    client = TestClient(create_app(settings=settings, session_factory=session_factory))
    csrf = _csrf(client.get("/login"))
    client.post(
        "/login",
        data={
            "csrf_token": csrf,
            "username": settings.ADMIN_USERNAME,
            "password": "correct-password",
        },
    )
    return client


def _run(*, status: RunStatus, kind: str = "discover", offset: int = 0) -> Run:
    started = datetime.now(UTC) - timedelta(minutes=offset + 1)
    return Run(
        trigger=RunTrigger.MANUAL,
        kind=kind,
        status=status,
        started_at=started,
        finished_at=started + timedelta(seconds=4),
        stats_json={"new_items": offset, "targets_queued": 1},
    )


def test_run_list_filters_and_paginates_50_rows(base_env, session_factory) -> None:
    with session_factory() as db:
        db.add_all(
            [
                _run(
                    status=RunStatus.FAILED if index == 0 else RunStatus.SUCCEEDED,
                    offset=index,
                )
                for index in range(55)
            ]
        )
        db.commit()
    client = _client(base_env, session_factory)

    first = client.get("/runs")
    second = client.get("/runs?page=2")
    failed = client.get("/runs?status_filter=failed")

    assert first.status_code == 200 and "1 / 2" in first.text
    assert second.status_code == 200 and "2 / 2" in second.text
    assert failed.status_code == 200 and "1件" in failed.text
    assert "failed" in failed.text


def test_download_run_detail_distinguishes_enqueue_from_task_completion(
    base_env, session_factory
) -> None:
    with session_factory() as db:
        run = _run(status=RunStatus.SUCCEEDED, kind="download")
        db.add(run)
        db.flush()
        db.add_all(
            [
                Task(
                    type=TaskType.DOWNLOAD,
                    target_ref_type="target",
                    target_ref_id=10,
                    worker_class=WorkerClass.NETWORK,
                    status=TaskStatus.SUCCEEDED,
                    max_attempts=5,
                    run_id=run.id,
                ),
                Task(
                    type=TaskType.VERIFY,
                    target_ref_type="target",
                    target_ref_id=10,
                    worker_class=WorkerClass.COMPUTE,
                    status=TaskStatus.FAILED,
                    max_attempts=5,
                    run_id=run.id,
                    error_message="verification failed",
                ),
            ]
        )
        db.commit()
        run_id = run.id
    client = _client(base_env, session_factory)

    detail = client.get(f"/runs/{run_id}")

    assert detail.status_code == 200
    assert "download Runの状態は投入結果です" in detail.text
    assert "実際の取得完了・失敗" in detail.text
    assert "succeeded=1" in detail.text and "failed=1" in detail.text
    assert "verification failed" in detail.text


def test_run_detail_task_status_filter(base_env, session_factory) -> None:
    with session_factory() as db:
        run = _run(status=RunStatus.RUNNING)
        db.add(run)
        db.flush()
        db.add_all(
            [
                Task(
                    type=TaskType.DISCOVER,
                    target_ref_type="playlist",
                    target_ref_id=1,
                    worker_class=WorkerClass.NETWORK,
                    status=status,
                    max_attempts=5,
                    run_id=run.id,
                )
                for status in (TaskStatus.RUNNING, TaskStatus.BLOCKED)
            ]
        )
        db.commit()
        run_id = run.id
    client = _client(base_env, session_factory)

    filtered = client.get(f"/runs/{run_id}?status_filter=blocked")

    assert filtered.status_code == 200
    assert "blocked（cancel要求済み）" not in filtered.text
    assert "blocked" in filtered.text
    assert "running（cancel要求済み）" not in filtered.text


def test_running_task_progress_polls_and_stops_after_completion(base_env, session_factory) -> None:
    with session_factory() as db:
        playlist = Playlist(
            name="Progress list",
            folder_name="progress-list",
            url="https://example.com/progress",
            kind_hint=PlaylistKindHint.VIDEO,
        )
        db.add(playlist)
        db.flush()
        item = Item(
            playlist_id=playlist.id,
            source_id="item-1",
            source_url="https://example.com/watch/item-1",
            title="Progress title",
        )
        db.add(item)
        db.flush()
        profile = Profile(name="Progress profile", kind="video", layout_strategy="flat")
        storage = Storage(
            name="Progress storage", kind=StorageKind.LOCAL, config_json={"path": "out"}
        )
        db.add_all([profile, storage])
        db.flush()
        assignment = PlaylistProfile(
            playlist_id=playlist.id,
            profile_id=profile.id,
            storage_id=storage.id,
            subpath="progress",
        )
        db.add(assignment)
        db.flush()
        target = Target(
            item_id=item.id,
            playlist_profile_id=assignment.id,
            status=TargetStatus.DOWNLOADING,
        )
        db.add(target)
        db.flush()
        run = _run(status=RunStatus.RUNNING, kind="download")
        run.finished_at = None
        db.add(run)
        db.flush()
        task = Task(
            type=TaskType.DOWNLOAD,
            target_ref_type="target",
            target_ref_id=target.id,
            worker_class=WorkerClass.NETWORK,
            status=TaskStatus.RUNNING,
            max_attempts=5,
            run_id=run.id,
            started_at=datetime.now(UTC),
            payload_json={
                "progress": {
                    "status": "downloading",
                    "percent": 42.5,
                    "speed": 2048,
                    "eta": 7,
                }
            },
        )
        db.add(task)
        db.commit()
        run_id = run.id
        task_id = task.id
    client = _client(base_env, session_factory)

    detail = client.get(f"/runs/{run_id}")

    assert detail.status_code == 200
    assert 'hx-trigger="every 3s"' in detail.text
    assert "Progress title" in detail.text
    assert "42.5%" in detail.text and "2.0 KiB/s" in detail.text and "7秒" in detail.text

    with session_factory() as db:
        task = db.get(Task, task_id)
        assert task is not None
        task.status = TaskStatus.SUCCEEDED
        task.finished_at = datetime.now(UTC)
        db.commit()
    completed = client.get(f"/runs/{run_id}/progress")
    assert completed.status_code == 200
    assert "ポーリングを停止しました" in completed.text
    assert "hx-trigger" not in completed.text


def test_run_log_is_db_anchored_data_dir_bounded_and_masked(
    base_env, session_factory, env_data_dirs, tmp_path
) -> None:
    log_dir = env_data_dirs["DATA_DIR"] / "logs"
    log_dir.mkdir()
    log_path = log_dir / "run-safe.log"
    log_path.write_text(
        "visible\npassword=log-secret\nhttps://example.com/watch?v=1&token=query-secret\n",
        encoding="utf-8",
    )
    with session_factory() as db:
        run = _run(status=RunStatus.SUCCEEDED)
        run.log_path = str(log_path)
        db.add(run)
        db.commit()
        run_id = run.id
    client = _client(base_env, session_factory)

    page = client.get(f"/runs/{run_id}/log?lines=2")
    downloaded = client.get(f"/runs/{run_id}/log/download")

    assert page.status_code == 200 and "visible" not in page.text
    assert "log-secret" not in page.text and "query-secret" not in page.text
    assert "password=********" in page.text and "token=********" in page.text
    assert downloaded.status_code == 200
    assert "log-secret" not in downloaded.text and "visible" in downloaded.text
    assert "attachment" in downloaded.headers["content-disposition"]

    outside = tmp_path / "outside.log"
    outside.write_text("must-not-read", encoding="utf-8")
    with session_factory() as db:
        run = db.get(Run, run_id)
        assert run is not None
        run.log_path = str(outside)
        db.commit()
    rejected = client.get(f"/runs/{run_id}/log")
    assert rejected.status_code == 403
    assert "must-not-read" not in rejected.text


def test_task_and_run_cancel_use_confirmation_and_repository_state(
    base_env, session_factory
) -> None:
    with session_factory() as db:
        run = _run(status=RunStatus.SUCCEEDED, kind="download")
        db.add(run)
        db.flush()
        queued = Task(
            type=TaskType.DOWNLOAD,
            target_ref_type="phase6_test",
            target_ref_id=1,
            worker_class=WorkerClass.NETWORK,
            status=TaskStatus.QUEUED,
            max_attempts=5,
            run_id=run.id,
        )
        running = Task(
            type=TaskType.VERIFY,
            target_ref_type="phase6_test",
            target_ref_id=2,
            worker_class=WorkerClass.COMPUTE,
            status=TaskStatus.RUNNING,
            worker_id="worker:test",
            max_attempts=5,
            run_id=run.id,
        )
        db.add_all([queued, running])
        db.commit()
        run_id, queued_id, running_id = run.id, queued.id, running.id
    client = _client(base_env, session_factory)
    detail = client.get(f"/runs/{run_id}")
    assert "confirm(" in detail.text
    assert "Runをキャンセル（2件）" in detail.text

    task_response = client.post(
        f"/tasks/{queued_id}/cancel",
        data={"csrf_token": _csrf(detail)},
        follow_redirects=False,
    )
    assert task_response.status_code == 303
    with session_factory() as db:
        queued = db.get(Task, queued_id)
        assert queued is not None and queued.status == TaskStatus.CANCELLED

    run_page = client.get(f"/runs/{run_id}")
    run_response = client.post(
        f"/runs/{run_id}/cancel",
        data={"csrf_token": _csrf(run_page)},
        follow_redirects=False,
    )
    assert run_response.status_code == 303
    with session_factory() as db:
        run = db.get(Run, run_id)
        running = db.get(Task, running_id)
        assert run is not None and run.status == RunStatus.CANCELLED
        assert running is not None and running.status == TaskStatus.RUNNING
        assert running.cancel_requested is True
        assert RunRepository(db).finish(run_id, RunStatus.SUCCEEDED, {}) is False
        db.refresh(run)
        assert run.status == RunStatus.CANCELLED
