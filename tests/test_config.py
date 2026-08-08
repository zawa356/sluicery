from __future__ import annotations

import pytest
from pydantic import ValidationError

from sluicery.config import Settings, load_settings


def test_missing_secret_key_exits(base_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        load_settings()
    assert exc_info.value.code == 1


def test_missing_secret_key_raises_validation_error(
    base_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_invalid_tz_rejected(base_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Not/ARealZone")
    with pytest.raises(ValidationError):
        Settings()


def test_allow_exec_defaults_to_false(base_env: None) -> None:
    settings = Settings()
    assert settings.ALLOW_EXEC is False


def test_valid_settings_load(base_env: None) -> None:
    settings = Settings()
    assert settings.TZ == "Asia/Tokyo"
    assert settings.STAGING_DIR is not None
    assert settings.DB_PATH == settings.DATA_DIR / "sluicery.db"
