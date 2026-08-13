"""Playlist Cookieの検証、暗号化保存、tmpfsへの一時展開。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from sluicery.db.models import Playlist

COOKIE_RUNTIME_DIR = Path("/run/sluicery")
MAX_COOKIE_BYTES = 2 * 1024 * 1024


class CookieConfigurationError(ValueError):
    """Cookieの内容を含まない、利用者向けの設定エラー。"""


@dataclass(frozen=True)
class MaterializedCookie:
    path: Path
    sensitive_values: tuple[str, ...]


def validate_cookie_bytes(raw: bytes) -> str:
    """Netscape Cookie形式を検査し、正規化したUTF-8文字列を返す。"""
    if not raw or len(raw) > MAX_COOKIE_BYTES:
        raise CookieConfigurationError("Cookieファイルは1バイト以上2MiB以下にしてください")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CookieConfigurationError("CookieファイルはUTF-8で保存してください") from exc
    if "\x00" in content:
        raise CookieConfigurationError("Cookieファイルの形式が不正です")

    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("# Netscape HTTP Cookie File"):
        raise CookieConfigurationError("Netscape Cookie形式のファイルを指定してください")

    valid_rows = 0
    for line in normalized.splitlines():
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        columns = line.split("\t", 6)
        if len(columns) != 7:
            raise CookieConfigurationError("Cookieファイルの形式が不正です")
        domain, include_subdomains, path, secure, expires, name, _value = columns
        if not domain or include_subdomains not in {"TRUE", "FALSE"}:
            raise CookieConfigurationError("Cookieファイルの形式が不正です")
        if not path.startswith("/") or secure not in {"TRUE", "FALSE"}:
            raise CookieConfigurationError("Cookieファイルの形式が不正です")
        try:
            int(expires)
        except ValueError as exc:
            raise CookieConfigurationError("Cookieファイルの形式が不正です") from exc
        if not name:
            raise CookieConfigurationError("Cookieファイルの形式が不正です")
        valid_rows += 1
    if valid_rows == 0:
        raise CookieConfigurationError("Cookieが1件も含まれていません")
    return normalized.rstrip("\n") + "\n"


def save_playlist_cookie(
    session: Session,
    playlist: Playlist,
    raw: bytes,
    *,
    enable_confirmed: bool,
) -> None:
    if not enable_confirmed:
        raise CookieConfigurationError("アカウント停止リスクの確認が必要です")
    content = validate_cookie_bytes(raw)
    playlist.cookies_encrypted = {"format": "netscape", "content": content}
    playlist.cookie_enabled = True
    session.commit()


def set_playlist_cookie_enabled(
    session: Session,
    playlist: Playlist,
    enabled: bool,
    *,
    enable_confirmed: bool = False,
) -> None:
    if enabled:
        if not enable_confirmed:
            raise CookieConfigurationError("アカウント停止リスクの確認が必要です")
        if not _stored_content(playlist.cookies_encrypted):
            raise CookieConfigurationError("Cookieが設定されていません")
    playlist.cookie_enabled = enabled
    session.commit()


def clear_playlist_cookie(session: Session, playlist: Playlist) -> None:
    playlist.cookie_enabled = False
    playlist.cookies_encrypted = None
    session.commit()


def playlist_cookie_configured(playlist: Playlist) -> bool:
    return _stored_content(playlist.cookies_encrypted) is not None


def _stored_content(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    content = value.get("content")
    return content if isinstance(content, str) and content else None


def _sensitive_values(content: str, path: Path) -> tuple[str, ...]:
    values = {str(path), content}
    for line in content.splitlines():
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        values.add(line)
        columns = line.split("\t", 6)
        if len(columns) == 7 and len(columns[-1]) >= 4:
            values.add(columns[-1])
    return tuple(sorted(values, key=len, reverse=True))


@contextmanager
def materialize_cookie(
    enabled: bool,
    stored: object,
    *,
    runtime_dir: Path = COOKIE_RUNTIME_DIR,
) -> Iterator[MaterializedCookie | None]:
    """Cookieを一意名の600ファイルへ展開し、例外時も削除する。"""
    if not enabled:
        yield None
        return
    content = _stored_content(stored)
    if content is None:
        raise CookieConfigurationError("Cookie設定を読み込めません")

    try:
        runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        raise CookieConfigurationError("Cookie一時領域を準備できません") from None
    path = runtime_dir / f"cookies-{uuid4().hex}.txt"
    fd: int | None = None
    try:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
                fd = None
                file.write(content)
                file.flush()
            os.chmod(path, 0o600)
        except OSError:
            raise CookieConfigurationError("Cookie一時ファイルを作成できません") from None
        yield MaterializedCookie(path=path, sensitive_values=_sensitive_values(content, path))
    finally:
        if fd is not None:
            os.close(fd)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            raise CookieConfigurationError("Cookie一時ファイルを削除できません") from None


def add_cookie_argument(args: list[str], cookie: MaterializedCookie | None) -> list[str]:
    if cookie is None:
        return args
    result = list(args)
    try:
        delimiter = result.index("--")
    except ValueError:
        delimiter = len(result)
    result[delimiter:delimiter] = ["--cookies", str(cookie.path)]
    return result


__all__ = [
    "COOKIE_RUNTIME_DIR",
    "MAX_COOKIE_BYTES",
    "CookieConfigurationError",
    "MaterializedCookie",
    "add_cookie_argument",
    "clear_playlist_cookie",
    "materialize_cookie",
    "playlist_cookie_configured",
    "save_playlist_cookie",
    "set_playlist_cookie_enabled",
    "validate_cookie_bytes",
]
