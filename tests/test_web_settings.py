from __future__ import annotations

import re

from fastapi.testclient import TestClient

from sluicery.config import Settings
from sluicery.core import settings as core_settings
from sluicery.db.models import Setting
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


def test_settings_page_lists_defaults_but_never_internal_keys(base_env, session_factory) -> None:
    with session_factory() as db:
        db.merge(Setting(key="_internal.test_secret", value_json='"hidden"'))
        db.commit()
    client = _client(base_env, session_factory)

    page = client.get("/settings")

    assert page.status_code == 200
    assert "staging.warn_pct" in page.text
    assert "defaults.video.format_selector" in page.text
    assert "log.retention_days" in page.text
    assert "schedule.discover_cron" in page.text
    assert "_internal" not in page.text
    assert "/settings/password" in page.text


def test_setting_update_and_reset_round_trip(base_env, session_factory) -> None:
    client = _client(base_env, session_factory)
    page = client.get("/settings")

    updated = client.post(
        "/settings/update",
        data={
            "csrf_token": _csrf(page),
            "key": "log.retention_days",
            "value": "45",
        },
        follow_redirects=False,
    )

    assert updated.status_code == 303
    with session_factory() as db:
        assert core_settings.get(db, "log.retention_days") == 45
        assert core_settings.is_overridden(db, "log.retention_days") is True
    reset_page = client.get("/settings")
    reset = client.post(
        "/settings/reset",
        data={
            "csrf_token": _csrf(reset_page),
            "key": "log.retention_days",
        },
        follow_redirects=False,
    )
    assert reset.status_code == 303
    with session_factory() as db:
        assert core_settings.get(db, "log.retention_days") == 30
        assert core_settings.is_overridden(db, "log.retention_days") is False


def test_invalid_setting_value_is_rejected_without_saving(base_env, session_factory) -> None:
    client = _client(base_env, session_factory)
    page = client.get("/settings")

    invalid = client.post(
        "/settings/update",
        data={
            "csrf_token": _csrf(page),
            "key": "staging.warn_pct",
            "value": "101",
        },
    )

    assert invalid.status_code == 422
    assert "0から100" in invalid.text
    with session_factory() as db:
        assert core_settings.get(db, "staging.warn_pct") == 80
        assert core_settings.is_overridden(db, "staging.warn_pct") is False


def test_internal_setting_cannot_be_changed_from_web(base_env, session_factory) -> None:
    client = _client(base_env, session_factory)
    page = client.get("/settings")

    response = client.post(
        "/settings/update",
        data={
            "csrf_token": _csrf(page),
            "key": "_internal.secret_key_fingerprint",
            "value": "replacement",
        },
    )

    assert response.status_code == 404


def test_item_concurrency_two_or_more_requires_explicit_warning_confirmation(
    base_env, session_factory
) -> None:
    client = _client(base_env, session_factory)
    page = client.get("/settings")
    assert "配信元へのアクセスが集中" in page.text

    refused = client.post(
        "/settings/update",
        data={
            "csrf_token": _csrf(page),
            "key": "download.item_concurrency",
            "value": "2",
        },
    )
    assert refused.status_code == 422
    with session_factory() as db:
        assert core_settings.get(db, "download.item_concurrency") == 1

    accepted = client.post(
        "/settings/update",
        data={
            "csrf_token": _csrf(refused),
            "key": "download.item_concurrency",
            "value": "2",
            "confirm_high_concurrency": "yes",
        },
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    with session_factory() as db:
        assert core_settings.get(db, "download.item_concurrency") == 2


def test_invalid_download_window_is_rejected_from_web(base_env, session_factory) -> None:
    client = _client(base_env, session_factory)
    page = client.get("/settings")

    response = client.post(
        "/settings/update",
        data={
            "csrf_token": _csrf(page),
            "key": "schedule.download_window",
            "value": "25:00-05:00",
        },
    )

    assert response.status_code == 422
    with session_factory() as db:
        assert core_settings.get(db, "schedule.download_window") is None
