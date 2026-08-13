from __future__ import annotations

import stat
from pathlib import Path

import pytest
from sqlalchemy import text

from sluicery.core.cookies import (
    CookieConfigurationError,
    add_cookie_argument,
    clear_playlist_cookie,
    materialize_cookie,
    save_playlist_cookie,
    set_playlist_cookie_enabled,
    validate_cookie_bytes,
)
from sluicery.db.models import Playlist, PlaylistKindHint

COOKIE_BYTES = b"""# Netscape HTTP Cookie File
.example.com\tTRUE\t/\tTRUE\t2147483647\tSID\tcookie-secret-value
"""


def _playlist(db_session) -> Playlist:
    playlist = Playlist(
        name="cookie-list",
        folder_name="cookie-list",
        url="https://example.com/list",
        kind_hint=PlaylistKindHint.VIDEO,
    )
    db_session.add(playlist)
    db_session.commit()
    return playlist


def test_cookie_is_encrypted_at_rest_and_write_only(db_session, engine) -> None:
    playlist = _playlist(db_session)

    save_playlist_cookie(db_session, playlist, COOKIE_BYTES, enable_confirmed=True)

    with engine.connect() as connection:
        raw = connection.execute(
            text("SELECT cookies_encrypted FROM playlist WHERE id = :id"),
            {"id": playlist.id},
        ).scalar_one()
    assert "cookie-secret-value" not in raw
    assert playlist.cookie_enabled is True
    assert playlist.cookies_encrypted["content"] == COOKIE_BYTES.decode()

    set_playlist_cookie_enabled(db_session, playlist, False)
    assert playlist.cookie_enabled is False
    assert playlist.cookies_encrypted is not None
    clear_playlist_cookie(db_session, playlist)
    assert playlist.cookie_enabled is False
    assert playlist.cookies_encrypted is None


def test_enabling_cookie_requires_explicit_risk_confirmation(db_session) -> None:
    playlist = _playlist(db_session)

    with pytest.raises(CookieConfigurationError, match="停止リスク"):
        save_playlist_cookie(db_session, playlist, COOKIE_BYTES, enable_confirmed=False)

    assert playlist.cookie_enabled is False
    assert playlist.cookies_encrypted is None


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not a cookie file",
        b"# Netscape HTTP Cookie File\ninvalid-row\n",
        b"# Netscape HTTP Cookie File\n",
        b"\xff\xfe",
    ],
)
def test_invalid_cookie_content_is_rejected_without_echoing_it(raw: bytes) -> None:
    with pytest.raises(CookieConfigurationError) as caught:
        validate_cookie_bytes(raw)

    decoded = raw.decode("utf-8", errors="ignore")
    if decoded:
        assert decoded not in str(caught.value)


def test_materialized_cookie_is_mode_600_and_removed_without_writeback(tmp_path: Path) -> None:
    stored = {"format": "netscape", "content": COOKIE_BYTES.decode()}
    materialized_path: Path | None = None

    with materialize_cookie(True, stored, runtime_dir=tmp_path) as cookie:
        assert cookie is not None
        materialized_path = cookie.path
        assert stat.S_IMODE(cookie.path.stat().st_mode) == 0o600
        assert cookie.path.read_bytes() == COOKIE_BYTES
        assert "cookie-secret-value" in cookie.sensitive_values
        args = add_cookie_argument(["--simulate", "--", "https://example.com"], cookie)
        assert args[-4:-2] == ["--cookies", str(cookie.path)]
        cookie.path.write_text("yt-dlp writeback", encoding="utf-8")

    assert materialized_path is not None and not materialized_path.exists()
    assert stored["content"] == COOKIE_BYTES.decode()


def test_materialized_cookie_is_removed_when_execution_raises(tmp_path: Path) -> None:
    stored = {"format": "netscape", "content": COOKIE_BYTES.decode()}
    path: Path | None = None

    with pytest.raises(RuntimeError, match="runner failed"):
        with materialize_cookie(True, stored, runtime_dir=tmp_path) as cookie:
            assert cookie is not None
            path = cookie.path
            raise RuntimeError("runner failed")

    assert path is not None and not path.exists()
    assert list(tmp_path.iterdir()) == []


def test_cookie_filesystem_error_does_not_expose_runtime_path(tmp_path: Path) -> None:
    invalid_runtime = tmp_path / "not-a-directory"
    invalid_runtime.write_text("file", encoding="utf-8")
    stored = {"format": "netscape", "content": COOKIE_BYTES.decode()}

    with pytest.raises(CookieConfigurationError) as caught:
        with materialize_cookie(True, stored, runtime_dir=invalid_runtime):
            pass

    assert str(invalid_runtime) not in str(caught.value)
