from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from sluicery.config import Settings
from sluicery.core.ytdlp_update import UpdateResult
from sluicery.db.models import (
    YtdlpRelease,
    YtdlpReleaseSource,
    YtdlpReleaseStatus,
)
from sluicery.downloader.version import InstallStatus, StatusResult
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
    login = client.get("/login")
    client.post(
        "/login",
        data={
            "csrf_token": _csrf(login),
            "username": settings.ADMIN_USERNAME,
            "password": "correct-password",
        },
    )
    return client


def test_ytdlp_page_shows_history_and_manual_actions(
    base_env, session_factory, monkeypatch
) -> None:
    client = _client(base_env, session_factory)
    with session_factory() as db:
        release = YtdlpRelease(
            version="2026.02.01",
            source=YtdlpReleaseSource.AUTO,
            status=YtdlpReleaseStatus.ACTIVE,
            smoketest_result_json={
                "success": True,
                "reason_code": "ok",
                "checked_at": "2026-08-16T00:00:00+00:00",
            },
        )
        prior = YtdlpRelease(
            version="2026.01.01",
            source=YtdlpReleaseSource.INITIAL,
            status=YtdlpReleaseStatus.INSTALLED,
            deactivated_at=datetime.now(UTC) - timedelta(days=1),
        )
        db.add_all([release, prior])
        db.commit()
    monkeypatch.setattr(
        "sluicery.web.app.get_status",
        lambda _root: StatusResult(InstallStatus.READY, "2026.02.01", "2026.02.01"),
    )

    page = client.get("/ytdlp")

    assert page.status_code == 200
    assert "2026.02.01" in page.text and "2026.01.01" in page.text
    assert "手動更新・再検証" in page.text
    assert "手動ロールバック" in page.text
    assert "成功 / ok" in page.text


def test_manual_update_endpoint_uses_update_service(
    base_env, session_factory, monkeypatch
) -> None:
    client = _client(base_env, session_factory)
    calls: list[bool] = []

    def fake_update(*_args, **_kwargs) -> UpdateResult:
        calls.append(True)
        return UpdateResult("no_change", "2026.02.01", "2026.02.01", None)

    monkeypatch.setattr("sluicery.web.app.update_ytdlp", fake_update)
    page = client.get("/ytdlp")
    response = client.post(
        "/ytdlp/update",
        data={"csrf_token": _csrf(page)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ytdlp"
    assert calls == [True]
