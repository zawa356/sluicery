from __future__ import annotations

from datetime import UTC, datetime

from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from sluicery.config import Settings
from sluicery.db.models import AuthSession
from sluicery.db.repositories.user import UserRepository
from sluicery.web.app import create_app
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

    response = client.post(
        "/login",
        data={"username": settings.ADMIN_USERNAME, "password": "correct-password"},
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
    client.get("/login")

    unknown = client.post("/login", data={"username": "unknown", "password": "wrong"})
    known = client.post(
        "/login",
        data={"username": settings.ADMIN_USERNAME, "password": "wrong"},
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
