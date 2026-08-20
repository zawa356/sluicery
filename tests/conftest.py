from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from sluicery.db import crypto, models
from sluicery.db.session import create_engine_for, create_session_factory


@pytest.fixture
def secret_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _reset_encryption_key(secret_key: str):
    crypto.set_encryption_key(secret_key)
    yield
    crypto._key_holder.clear()  # noqa: SLF001 - テスト間の分離のため


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    # §9.1: インメモリではなく一時ファイルの SQLite を使う（WAL の挙動を本番構成に揃える）。
    return tmp_path / "sluicery-test.db"


@pytest.fixture
def engine(db_path: Path):
    eng = create_engine_for(db_path)
    models.metadata_obj.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    factory = create_session_factory(engine)
    yield factory
    from sluicery.hooks import flush_pending_hooks

    assert flush_pending_hooks()


@pytest.fixture
def db_session(session_factory):
    s = session_factory()
    yield s
    s.close()


@pytest.fixture
def env_data_dirs(tmp_path: Path) -> dict[str, Path]:
    data_dir = tmp_path / "data"
    staging_dir = data_dir / "staging"
    media_dir = tmp_path / "media"
    for d in (data_dir, staging_dir, media_dir):
        d.mkdir(parents=True, exist_ok=True)
    return {"DATA_DIR": data_dir, "STAGING_DIR": staging_dir, "MEDIA_ROOT": media_dir}


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch, secret_key: str, env_data_dirs: dict[str, Path]):
    """Settings が読めるだけの最小限の環境変数を設定する。"""
    from sluicery.config import Settings

    monkeypatch.setenv("SECRET_KEY", secret_key)
    for key, value in env_data_dirs.items():
        monkeypatch.setenv(key, str(value))
    # cwd の .env を誤って拾わないようにする
    monkeypatch.chdir(env_data_dirs["DATA_DIR"].parent)
    # MEDIA_MOUNT_PATH はコンテナ内の固定マウント先（本番は /mnt/media）。
    # テスト環境にそのパスは存在しないため、tmp_path 配下に差し替える。
    monkeypatch.setattr(Settings, "MEDIA_MOUNT_PATH", env_data_dirs["MEDIA_ROOT"])
