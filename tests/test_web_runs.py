from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from sluicery.config import Settings
from sluicery.db.models import (
    Run,
    RunStatus,
    RunTrigger,
    Task,
    TaskStatus,
    TaskType,
    WorkerClass,
)
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
