"""単一ユーザー認証と、DB に状態を持つ署名付き Cookie セッション。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy.orm import Session, sessionmaker

from sluicery.config import Settings
from sluicery.core.settings import OperationalSettings
from sluicery.db.models import User
from sluicery.db.repositories.auth_session import AuthSessionRepository
from sluicery.db.repositories.user import UserRepository

SESSION_COOKIE_NAME = "sluicery_session"
LOGIN_ERROR_MESSAGE = "ユーザー名またはパスワードが違います"
_SESSION_TOKEN_BYTES = 32
_PASSWORD_HASHER = PasswordHasher()


def _now() -> datetime:
    return datetime.now(UTC)


def _derive_key(secret_key: str, *, purpose: bytes) -> bytes:
    """Fernet 用の SECRET_KEY から、用途を分離した鍵を HKDF で導出する。"""
    source = base64.urlsafe_b64decode(secret_key.encode("ascii"))
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"sluicery-auth-v1",
        info=purpose,
    ).derive(source)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class SessionSigner:
    def __init__(self, secret_key: str) -> None:
        signing_key = _derive_key(secret_key, purpose=b"session-cookie-signing")
        self._serializer = URLSafeSerializer(
            signing_key,
            salt="sluicery-session-cookie-v1",
        )

    def dumps(self, token: str) -> str:
        return self._serializer.dumps(token)

    def loads(self, signed_token: str) -> str | None:
        try:
            value = self._serializer.loads(signed_token)
        except BadSignature:
            return None
        if not isinstance(value, str):
            return None
        return value


@dataclass(frozen=True)
class SessionIdentity:
    token: str
    session_id: int
    user_id: int | None
    username: str | None
    expires_at: datetime

    @property
    def authenticated(self) -> bool:
        return self.user_id is not None


@dataclass(frozen=True)
class NewSession:
    token: str
    signed_cookie: str
    identity: SessionIdentity


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    session: NewSession | None = None


@dataclass(frozen=True)
class InitialUserResult:
    created: bool
    generated_password: str | None = None


class AuthService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._signer = SessionSigner(settings.SECRET_KEY)

    def create_session(self, *, user_id: int | None = None) -> NewSession:
        token = secrets.token_urlsafe(_SESSION_TOKEN_BYTES)
        with self._session_factory() as db:
            max_age = OperationalSettings(db).auth_session_max_age_sec
            expires_at = _now() + timedelta(seconds=max_age)
            row = AuthSessionRepository(db).create(
                token_hash=hash_session_token(token),
                user_id=user_id,
                expires_at=expires_at,
            )
            user = db.get(User, user_id) if user_id is not None else None
            if user_id is not None and user is None:
                raise RuntimeError("セッションに指定されたユーザーが存在しません")
            username = user.username if user is not None else None
        identity = SessionIdentity(token, row.id, user_id, username, expires_at)
        return NewSession(token, self._signer.dumps(token), identity)

    def load_session(self, signed_cookie: str | None) -> SessionIdentity | None:
        if not signed_cookie:
            return None
        token = self._signer.loads(signed_cookie)
        if token is None:
            return None

        with self._session_factory() as db:
            repo = AuthSessionRepository(db)
            row = repo.get_by_token_hash(hash_session_token(token))
            if row is None:
                return None
            if row.expires_at <= _now():
                repo.delete(row)
                return None
            user = db.get(User, row.user_id) if row.user_id is not None else None
            if row.user_id is not None and user is None:
                repo.delete(row)
                return None
            return SessionIdentity(
                token=token,
                session_id=row.id,
                user_id=row.user_id,
                username=user.username if user is not None else None,
                expires_at=row.expires_at,
            )

    def authenticate(self, current_token: str, username: str, password: str) -> LoginResult:
        now = _now()
        with self._session_factory() as db:
            user = UserRepository(db).get_single()
            if user is None:
                # 初期化異常時にも応答時間からユーザー不在を推測しにくくする。
                _verify_password(_dummy_password_hash(), password)
                return LoginResult(ok=False)

            limits = OperationalSettings(db)
            if user.locked_until is not None and user.locked_until > now:
                return LoginResult(ok=False)
            if user.locked_until is not None:
                user.locked_until = None
                user.failed_login_attempts = 0

            username_ok = hmac.compare_digest(username.encode(), user.username.encode())
            password_ok = _verify_password(user.password_hash, password)
            if not (username_ok and password_ok):
                user.failed_login_attempts += 1
                if user.failed_login_attempts >= limits.auth_max_login_attempts:
                    user.locked_until = now + timedelta(seconds=limits.auth_lockout_sec)
                db.commit()
                return LoginResult(ok=False)

            if _PASSWORD_HASHER.check_needs_rehash(user.password_hash):
                user.password_hash = _PASSWORD_HASHER.hash(password)
            user.failed_login_attempts = 0
            user.locked_until = None
            user.last_login_at = now
            db.commit()
            user_id = user.id

        self.delete_session(current_token)
        return LoginResult(ok=True, session=self.create_session(user_id=user_id))

    def change_password(self, user_id: int, current_password: str, new_password: str) -> bool:
        with self._session_factory() as db:
            user = db.get(User, user_id)
            if user is None or not _verify_password(user.password_hash, current_password):
                return False
            user.password_hash = _PASSWORD_HASHER.hash(new_password)
            user.failed_login_attempts = 0
            user.locked_until = None
            db.commit()
            AuthSessionRepository(db).delete_for_user(user_id)
        return True

    def delete_session(self, token: str) -> None:
        with self._session_factory() as db:
            AuthSessionRepository(db).delete_by_token_hash(hash_session_token(token))


_dummy_hash: str | None = None


def _dummy_password_hash() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = _PASSWORD_HASHER.hash(secrets.token_urlsafe(24))
    return _dummy_hash


def _verify_password(password_hash: str, password: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False


def ensure_initial_user(
    session_factory: sessionmaker[Session],
    settings: Settings,
) -> InitialUserResult:
    """ユーザーが0件のときだけ初期管理者を作る。平文は DB に保存しない。"""
    with session_factory() as db:
        repo = UserRepository(db)
        if repo.get_single() is not None:
            return InitialUserResult(created=False)
        generated_password = None
        password = settings.ADMIN_PASSWORD
        if not password:
            generated_password = secrets.token_urlsafe(24)
            password = generated_password
        repo.create_single(
            username=settings.ADMIN_USERNAME,
            password_hash=_PASSWORD_HASHER.hash(password),
            failed_login_attempts=0,
        )
        return InitialUserResult(created=True, generated_password=generated_password)


__all__ = [
    "AuthService",
    "InitialUserResult",
    "LOGIN_ERROR_MESSAGE",
    "LoginResult",
    "NewSession",
    "SESSION_COOKIE_NAME",
    "SessionIdentity",
    "SessionSigner",
    "ensure_initial_user",
    "hash_session_token",
]
