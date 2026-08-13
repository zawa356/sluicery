from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from argon2 import PasswordHasher
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from sluicery.config import Settings
from sluicery.db.models import AuthSession, Playlist, PlaylistKindHint
from sluicery.db.repositories.user import UserRepository
from sluicery.web.app import create_app, require_csrf
from sluicery.web.auth import (
    LOGIN_ERROR_MESSAGE,
    SESSION_COOKIE_NAME,
    AuthService,
    ensure_initial_user,
    hash_session_token,
)


def _settings(base_env) -> Settings:
    return Settings()


def _create_admin(session_factory, settings: Settings, password: str = "correct-password") -> None:
    settings.ADMIN_PASSWORD = password
    result = ensure_initial_user(session_factory, settings)
    assert result.created is True


def _csrf(response) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def _login(client: TestClient, settings: Settings, password: str = "correct-password") -> None:
    csrf_token = _csrf(client.get("/login"))
    response = client.post(
        "/login",
        data={
            "csrf_token": csrf_token,
            "username": settings.ADMIN_USERNAME,
            "password": password,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_initial_user_is_created_once_with_argon2(
    base_env, session_factory, capsys
) -> None:
    settings = _settings(base_env)
    settings.ADMIN_PASSWORD = "first-password"

    first = ensure_initial_user(session_factory, settings)
    second = ensure_initial_user(session_factory, settings)

    assert first.created is True
    assert first.generated_password is None
    assert second.created is False
    with session_factory() as db:
        user = UserRepository(db).get_single()
        assert user is not None
        assert user.password_hash.startswith("$argon2")
        assert PasswordHasher().verify(user.password_hash, "first-password")
        assert "first-password" not in user.password_hash
    assert capsys.readouterr().out == ""


def test_random_initial_password_is_returned_only_when_created(base_env, session_factory) -> None:
    settings = _settings(base_env)
    settings.ADMIN_PASSWORD = None

    first = ensure_initial_user(session_factory, settings)
    second = ensure_initial_user(session_factory, settings)

    assert first.generated_password is not None
    assert second.generated_password is None
    with session_factory() as db:
        user = UserRepository(db).get_single()
        assert user is not None
        assert first.generated_password not in user.password_hash


def test_session_stores_only_token_hash(base_env, session_factory) -> None:
    settings = _settings(base_env)
    service = AuthService(session_factory, settings)

    created = service.create_session()

    assert created.token not in created.signed_cookie
    with session_factory() as db:
        row = db.get(AuthSession, created.identity.session_id)
        assert row is not None
        assert row.token_hash == hash_session_token(created.token)
        assert created.token not in row.token_hash
        assert row.expires_at > datetime.now(UTC)


def test_session_creation_removes_expired_rows(base_env, session_factory) -> None:
    settings = _settings(base_env)
    service = AuthService(session_factory, settings)
    expired = service.create_session()
    with session_factory() as db:
        row = db.get(AuthSession, expired.identity.session_id)
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

    service.create_session()

    with session_factory() as db:
        assert db.scalar(
            select(AuthSession).where(
                AuthSession.token_hash == hash_session_token(expired.token)
            )
        ) is None


def test_authentication_is_whitelist_based_and_login_rotates_session(
    base_env, session_factory
) -> None:
    settings = _settings(base_env)
    _create_admin(session_factory, settings)
    client = TestClient(create_app(settings=settings, session_factory=session_factory))

    health = client.get("/healthz")
    assert health.status_code == 200
    assert SESSION_COOKIE_NAME not in health.cookies

    protected = client.get("/", follow_redirects=False)
    assert protected.status_code == 303
    assert protected.headers["location"] == "/login"
    anonymous_cookie = protected.cookies[SESSION_COOKIE_NAME]
    csrf_token = _csrf(client.get("/login"))

    response = client.post(
        "/login",
        data={
            "csrf_token": csrf_token,
            "username": settings.ADMIN_USERNAME,
            "password": "correct-password",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    authenticated_cookie = response.cookies[SESSION_COOKIE_NAME]
    assert authenticated_cookie != anonymous_cookie
    set_cookie = response.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie

    assert client.get("/").status_code == 200
    service = AuthService(session_factory, settings)
    assert service.load_session(anonymous_cookie) is None
    assert service.load_session(authenticated_cookie).authenticated is True


def test_secure_cookie_can_be_enabled(base_env, session_factory) -> None:
    settings = _settings(base_env)
    settings.AUTH_COOKIE_SECURE = True
    _create_admin(session_factory, settings)
    client = TestClient(create_app(settings=settings, session_factory=session_factory))

    response = client.get("/login")

    assert "Secure" in response.headers["set-cookie"]


def test_five_failures_lock_account_across_service_restart(base_env, session_factory) -> None:
    settings = _settings(base_env)
    _create_admin(session_factory, settings)
    service = AuthService(session_factory, settings)
    anonymous = service.create_session()

    for _ in range(5):
        result = service.authenticate(anonymous.token, settings.ADMIN_USERNAME, "wrong")
        assert result.ok is False

    restarted_service = AuthService(session_factory, settings)
    result = restarted_service.authenticate(
        anonymous.token,
        settings.ADMIN_USERNAME,
        "correct-password",
    )
    assert result.ok is False
    with session_factory() as db:
        user = UserRepository(db).get_single()
        assert user is not None
        assert user.failed_login_attempts == 5
        assert user.locked_until is not None
        assert user.locked_until > datetime.now(UTC)


def test_login_error_does_not_reveal_username_existence(base_env, session_factory) -> None:
    settings = _settings(base_env)
    _create_admin(session_factory, settings)
    client = TestClient(create_app(settings=settings, session_factory=session_factory))
    csrf_token = _csrf(client.get("/login"))

    unknown = client.post(
        "/login",
        data={"csrf_token": csrf_token, "username": "unknown", "password": "wrong"},
    )
    known = client.post(
        "/login",
        data={
            "csrf_token": csrf_token,
            "username": settings.ADMIN_USERNAME,
            "password": "wrong",
        },
    )

    assert unknown.status_code == known.status_code == 401
    assert LOGIN_ERROR_MESSAGE in unknown.text
    assert LOGIN_ERROR_MESSAGE in known.text


def test_password_change_revokes_all_sessions(base_env, session_factory) -> None:
    settings = _settings(base_env)
    _create_admin(session_factory, settings)
    service = AuthService(session_factory, settings)
    first_anonymous = service.create_session()
    first = service.authenticate(
        first_anonymous.token, settings.ADMIN_USERNAME, "correct-password"
    ).session
    second_anonymous = service.create_session()
    second = service.authenticate(
        second_anonymous.token, settings.ADMIN_USERNAME, "correct-password"
    ).session
    assert first is not None and second is not None

    assert service.change_password(first.identity.user_id, "correct-password", "new-password")

    assert service.load_session(first.signed_cookie) is None
    assert service.load_session(second.signed_cookie) is None
    replacement = service.authenticate(
        service.create_session().token,
        settings.ADMIN_USERNAME,
        "new-password",
    )
    assert replacement.ok is True


def test_password_change_and_session_revocation_are_atomic(
    base_env, session_factory, engine
) -> None:
    settings = _settings(base_env)
    _create_admin(session_factory, settings)
    service = AuthService(session_factory, settings)
    anonymous = service.create_session()
    authenticated = service.authenticate(
        anonymous.token, settings.ADMIN_USERNAME, "correct-password"
    ).session
    assert authenticated is not None

    def reject_session_delete(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE FROM auth_session"):
            raise RuntimeError("injected delete failure")

    event.listen(engine, "before_cursor_execute", reject_session_delete)
    try:
        with pytest.raises(RuntimeError, match="injected delete failure"):
            service.change_password(
                authenticated.identity.user_id,
                "correct-password",
                "new-password",
            )
    finally:
        event.remove(engine, "before_cursor_execute", reject_session_delete)

    assert service.load_session(authenticated.signed_cookie) is not None
    retry = service.authenticate(
        service.create_session().token,
        settings.ADMIN_USERNAME,
        "correct-password",
    )
    assert retry.ok is True


def test_csrf_is_required_for_login_and_bound_to_session(base_env, session_factory) -> None:
    settings = _settings(base_env)
    _create_admin(session_factory, settings)
    app = create_app(settings=settings, session_factory=session_factory)
    first = TestClient(app)
    second = TestClient(app)
    first_token = _csrf(first.get("/login"))
    second.get("/login")

    missing = first.post(
        "/login",
        data={"username": settings.ADMIN_USERNAME, "password": "correct-password"},
    )
    tampered = first.post(
        "/login",
        data={
            "csrf_token": f"{first_token}x",
            "username": settings.ADMIN_USERNAME,
            "password": "correct-password",
        },
    )
    other_session = second.post(
        "/login",
        data={
            "csrf_token": first_token,
            "username": settings.ADMIN_USERNAME,
            "password": "correct-password",
        },
    )

    assert missing.status_code == 403
    assert tampered.status_code == 403
    assert other_session.status_code == 403
    with session_factory() as db:
        user = UserRepository(db).get_single()
        assert user is not None
        assert user.failed_login_attempts == 0


def test_csrf_header_is_accepted_and_all_mutations_reject_missing_token(
    base_env, session_factory
) -> None:
    settings = _settings(base_env)
    _create_admin(session_factory, settings)
    client = TestClient(create_app(settings=settings, session_factory=session_factory))
    csrf_token = _csrf(client.get("/login"))
    login = client.post(
        "/login",
        data={
            "csrf_token": csrf_token,
            "username": settings.ADMIN_USERNAME,
            "password": "correct-password",
        },
        follow_redirects=False,
    )
    assert login.status_code == 303
    authenticated_csrf = _csrf(client.get("/"))

    assert client.post("/logout", follow_redirects=False).status_code == 403
    assert client.post(
        "/settings/password",
        data={"current_password": "correct-password", "new_password": "new-password"},
    ).status_code == 403

    logout = client.post(
        "/logout",
        headers={"X-CSRF-Token": authenticated_csrf},
        follow_redirects=False,
    )
    assert logout.status_code == 303


def test_csrf_dependency_treats_only_get_as_exempt(base_env, session_factory) -> None:
    settings = _settings(base_env)
    _create_admin(session_factory, settings)
    client = TestClient(create_app(settings=settings, session_factory=session_factory))
    client.get("/login")

    assert client.request("HEAD", "/").status_code == 403
    assert client.request("OPTIONS", "/").status_code == 403


def test_every_state_changing_api_route_has_global_csrf_dependency(
    base_env, session_factory
) -> None:
    settings = _settings(base_env)
    _create_admin(session_factory, settings)
    app = create_app(settings=settings, session_factory=session_factory)

    mutation_routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.methods != {"GET"}
    ]

    assert mutation_routes
    for route in mutation_routes:
        assert any(dependency.call is require_csrf for dependency in route.dependant.dependencies)


def test_ui_uses_local_assets_and_has_seven_navigation_groups(base_env, session_factory) -> None:
    settings = _settings(base_env)
    _create_admin(session_factory, settings)
    client = TestClient(create_app(settings=settings, session_factory=session_factory))
    _login(client, settings)

    response = client.get("/")

    assert response.status_code == 200
    assert response.text.count("<nav") == 1
    for path in ("/playlists", "/profiles", "/storages", "/runs", "/reports", "/settings"):
        assert f'href="{path}"' in response.text
    assert "/static/app.css" in response.text
    assert "/static/vendor/htmx-2.0.10.min.js" in response.text
    assert "https://" not in response.text
    assert "X-CSRF-Token" in response.text
    assert client.get("/static/app.css").status_code == 200
    htmx = client.get("/static/vendor/htmx-2.0.10.min.js")
    assert htmx.status_code == 200
    assert len(htmx.content) == 51_238


def test_html_error_pages_hide_exception_detail(base_env, session_factory) -> None:
    settings = _settings(base_env)
    _create_admin(session_factory, settings)
    app = create_app(settings=settings, session_factory=session_factory)

    @app.get("/test-internal-error")
    def test_internal_error() -> None:
        raise RuntimeError("sensitive-stack-detail")

    client = TestClient(app, raise_server_exceptions=False)
    _login(client, settings)

    not_found = client.get("/does-not-exist")
    forbidden = client.post("/logout")
    internal = client.get("/test-internal-error")

    assert not_found.status_code == 404
    assert "ページが見つかりません" in not_found.text
    assert forbidden.status_code == 403
    assert "この操作は許可されていません" in forbidden.text
    assert internal.status_code == 500
    assert "処理中にエラーが発生しました" in internal.text
    assert "sensitive-stack-detail" not in internal.text
    assert "Traceback" not in internal.text


def test_flash_message_is_displayed_once_after_password_change(base_env, session_factory) -> None:
    settings = _settings(base_env)
    _create_admin(session_factory, settings)
    client = TestClient(create_app(settings=settings, session_factory=session_factory))
    _login(client, settings)
    password_page = client.get("/settings/password")
    csrf_token = _csrf(password_page)

    changed = client.post(
        "/settings/password",
        data={
            "csrf_token": csrf_token,
            "current_password": "correct-password",
            "new_password": "new-password-value",
        },
        follow_redirects=False,
    )
    assert changed.status_code == 303

    first = client.get("/login")
    second = client.get("/login")
    assert "パスワードを変更しました" in first.text
    assert "パスワードを変更しました" not in second.text


def test_playlist_cookie_ui_is_write_only_and_requires_risk_confirmation(
    base_env, session_factory
) -> None:
    settings = _settings(base_env)
    _create_admin(session_factory, settings)
    with session_factory() as db:
        playlist = Playlist(
            name="Cookie Playlist",
            folder_name="cookie-playlist",
            url="https://example.com/list",
            kind_hint=PlaylistKindHint.VIDEO,
        )
        db.add(playlist)
        db.commit()
        playlist_id = playlist.id
    client = TestClient(create_app(settings=settings, session_factory=session_factory))
    _login(client, settings)
    page = client.get(f"/playlists/{playlist_id}/cookies")
    csrf_token = _csrf(page)
    cookie_content = """# Netscape HTTP Cookie File
.example.com\tTRUE\t/\tTRUE\t2147483647\tSID\tui-cookie-secret
"""

    unconfirmed = client.post(
        f"/playlists/{playlist_id}/cookies",
        data={"csrf_token": csrf_token, "action": "save_enable"},
        files={"cookie_file": ("cookies.txt", cookie_content, "text/plain")},
    )
    assert unconfirmed.status_code == 400
    assert "停止リスク" in unconfirmed.text

    saved = client.post(
        f"/playlists/{playlist_id}/cookies",
        data={
            "csrf_token": csrf_token,
            "action": "save_enable",
            "risk_confirmed": "yes",
        },
        files={"cookie_file": ("cookies.txt", cookie_content, "text/plain")},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    status_page = client.get(saved.headers["location"])
    assert "設定済み" in status_page.text
    assert "有効" in status_page.text
    assert "ui-cookie-secret" not in status_page.text
