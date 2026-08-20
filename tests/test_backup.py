from __future__ import annotations

import io
import json
import sqlite3
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from sluicery.backup import (
    BackupError,
    SecretKeyMismatchError,
    create_backup,
    restore_backup,
)
from sluicery.db.crypto import FINGERPRINT_SETTING_KEY, secret_key_fingerprint


def _database(path: Path, secret_key: str, marker: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE setting (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO setting VALUES (?, ?, ?)",
            (
                FINGERPRINT_SETTING_KEY,
                secret_key_fingerprint(secret_key),
                "2026-08-21T00:00:00Z",
            ),
        )
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES (?)", (marker,))


def _marker(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT value FROM marker").fetchone()
    assert row is not None
    return row[0]


def test_backup_and_restore_roundtrip_excludes_runtime_data(tmp_path: Path) -> None:
    secret_key = Fernet.generate_key().decode()
    source_db = tmp_path / "source.db"
    _database(source_db, secret_key, "backup-state")
    config = tmp_path / "config"
    config.mkdir()
    (config / "hooks.yaml").write_text("subscriptions: []\n", encoding="utf-8")
    nested = config / "nested"
    nested.mkdir()
    (nested / "portable.txt").write_text("設定\n", encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "worker.log").write_text("safe log\n", encoding="utf-8")
    (tmp_path / "staging").mkdir()
    (tmp_path / "staging" / "media.part").write_bytes(b"not included")
    (tmp_path / "ytdlp").mkdir()
    (tmp_path / "ytdlp" / "binary").write_bytes(b"not included")

    result = create_backup(
        db_path=source_db,
        config_dir=config,
        log_dir=logs,
        output_dir=tmp_path / "backups",
        secret_key=secret_key,
        include_logs=True,
        label="unit",
        now=datetime(2026, 8, 21, 1, 2, 3, tzinfo=UTC),
    )

    assert result.archive_path.name.startswith("sluicery-20260821T010203Z-unit-")
    assert result.archive_path.stat().st_mode & 0o777 == 0o600
    with tarfile.open(result.archive_path, "r:gz") as archive:
        names = set(archive.getnames())
        assert names == {
            "manifest.json",
            "database/sluicery.db",
            "config/hooks.yaml",
            "config/nested/portable.txt",
            "logs/worker.log",
        }
        for member in archive.getmembers():
            source = archive.extractfile(member)
            assert source is not None
            assert secret_key.encode() not in source.read()
    assert not any("staging" in name or "ytdlp" in name for name in names)

    target_db = tmp_path / "target.db"
    _database(target_db, secret_key, "old-state")
    target_config = tmp_path / "target-config"
    target_config.mkdir()
    (target_config / "stale.yaml").write_text("remove me\n", encoding="utf-8")
    target_logs = tmp_path / "target-logs"
    restored = restore_backup(
        archive_path=result.archive_path,
        db_path=target_db,
        config_dir=target_config,
        log_dir=target_logs,
        secret_key=secret_key,
    )

    assert restored.secret_key_matched
    assert restored.config_files == 2
    assert restored.log_files == 1
    assert _marker(target_db) == "backup-state"
    assert (target_config / "hooks.yaml").read_text(encoding="utf-8") == "subscriptions: []\n"
    assert not (target_config / "stale.yaml").exists()
    assert (target_logs / "worker.log").read_text(encoding="utf-8") == "safe log\n"


def test_secret_key_mismatch_stops_before_overwrite(tmp_path: Path) -> None:
    backup_key = Fernet.generate_key().decode()
    current_key = Fernet.generate_key().decode()
    source_db = tmp_path / "source.db"
    _database(source_db, backup_key, "backup-state")
    config = tmp_path / "config"
    config.mkdir()
    (config / "hooks.yaml").write_text("backup\n", encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    result = create_backup(
        db_path=source_db,
        config_dir=config,
        log_dir=logs,
        output_dir=tmp_path / "backups",
        secret_key=backup_key,
    )
    target_db = tmp_path / "target.db"
    _database(target_db, current_key, "current-state")
    target_config = tmp_path / "target-config"
    target_config.mkdir()
    (target_config / "current.yaml").write_text("current\n", encoding="utf-8")

    with pytest.raises(SecretKeyMismatchError, match="指紋"):
        restore_backup(
            archive_path=result.archive_path,
            db_path=target_db,
            config_dir=target_config,
            log_dir=tmp_path / "target-logs",
            secret_key=current_key,
        )

    assert _marker(target_db) == "current-state"
    assert (target_config / "current.yaml").read_text(encoding="utf-8") == "current\n"


def test_backup_refuses_database_key_fingerprint_mismatch(tmp_path: Path) -> None:
    database_key = Fernet.generate_key().decode()
    current_key = Fernet.generate_key().decode()
    database = tmp_path / "database.db"
    _database(database, database_key, "state")
    config = tmp_path / "config"
    config.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()

    with pytest.raises(SecretKeyMismatchError, match="DB記録"):
        create_backup(
            db_path=database,
            config_dir=config,
            log_dir=logs,
            output_dir=tmp_path / "backups",
            secret_key=current_key,
        )


def test_backup_refuses_config_symlink(tmp_path: Path) -> None:
    secret_key = Fernet.generate_key().decode()
    database = tmp_path / "database.db"
    _database(database, secret_key, "state")
    config = tmp_path / "config"
    config.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("outside\n", encoding="utf-8")
    (config / "link").symlink_to(outside)
    logs = tmp_path / "logs"
    logs.mkdir()

    with pytest.raises(BackupError, match="symlink|特殊"):
        create_backup(
            db_path=database,
            config_dir=config,
            log_dir=logs,
            output_dir=tmp_path / "backups",
            secret_key=secret_key,
        )


def test_restore_rejects_path_traversal_archive(tmp_path: Path) -> None:
    archive_path = tmp_path / "malicious.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        payload = b"escape"
        member = tarfile.TarInfo("../escape")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))

    with pytest.raises(BackupError, match="path"):
        restore_backup(
            archive_path=archive_path,
            db_path=tmp_path / "target.db",
            config_dir=tmp_path / "config",
            log_dir=tmp_path / "logs",
            secret_key=Fernet.generate_key().decode(),
        )

    assert not (tmp_path.parent / "escape").exists()


def test_restore_rejects_symlink_in_target_before_any_overwrite(tmp_path: Path) -> None:
    secret_key = Fernet.generate_key().decode()
    source_db = tmp_path / "source.db"
    _database(source_db, secret_key, "backup-state")
    config = tmp_path / "config"
    config.mkdir()
    (config / "nested").mkdir()
    (config / "nested" / "hooks.yaml").write_text("backup\n", encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    result = create_backup(
        db_path=source_db,
        config_dir=config,
        log_dir=logs,
        output_dir=tmp_path / "backups",
        secret_key=secret_key,
    )
    target_db = tmp_path / "target.db"
    _database(target_db, secret_key, "current-state")
    target_config = tmp_path / "target-config"
    target_config.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (target_config / "nested").symlink_to(outside, target_is_directory=True)

    with pytest.raises(BackupError, match="symlink"):
        restore_backup(
            archive_path=result.archive_path,
            db_path=target_db,
            config_dir=target_config,
            log_dir=tmp_path / "target-logs",
            secret_key=secret_key,
        )

    assert _marker(target_db) == "current-state"
    assert not (outside / "hooks.yaml").exists()


def test_restore_rejects_manifest_tampering(tmp_path: Path) -> None:
    secret_key = Fernet.generate_key().decode()
    database = tmp_path / "source.db"
    _database(database, secret_key, "backup-state")
    config = tmp_path / "config"
    config.mkdir()
    (config / "hooks.yaml").write_text("backup\n", encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    result = create_backup(
        db_path=database,
        config_dir=config,
        log_dir=logs,
        output_dir=tmp_path / "backups",
        secret_key=secret_key,
    )
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(result.archive_path, "r:gz") as source:
        payloads = {}
        for member in source.getmembers():
            stream = source.extractfile(member)
            assert stream is not None
            payloads[member.name] = stream.read()
    manifest = json.loads(payloads["manifest.json"])
    manifest["created_at"] = "2099-01-01T00:00:00Z"
    payloads["manifest.json"] = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode()
    with tarfile.open(tampered, "w:gz") as archive:
        for name, payload in payloads.items():
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))

    with pytest.raises(BackupError, match="HMAC"):
        restore_backup(
            archive_path=tampered,
            db_path=tmp_path / "target.db",
            config_dir=tmp_path / "target-config",
            log_dir=tmp_path / "target-logs",
            secret_key=secret_key,
        )


def test_restore_checkpoint_failure_leaves_database_config_and_logs_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    secret_key = Fernet.generate_key().decode()
    source_db = tmp_path / "source.db"
    _database(source_db, secret_key, "backup-state")
    source_config = tmp_path / "source-config"
    source_config.mkdir()
    (source_config / "hooks.yaml").write_text("backup\n", encoding="utf-8")
    source_logs = tmp_path / "source-logs"
    source_logs.mkdir()
    (source_logs / "run-1.log").write_text("backup-log\n", encoding="utf-8")
    result = create_backup(
        db_path=source_db,
        config_dir=source_config,
        log_dir=source_logs,
        output_dir=tmp_path / "backups",
        secret_key=secret_key,
        include_logs=True,
    )
    target_db = tmp_path / "target.db"
    _database(target_db, secret_key, "current-state")
    target_config = tmp_path / "target-config"
    target_config.mkdir()
    (target_config / "current.yaml").write_text("current\n", encoding="utf-8")
    target_logs = tmp_path / "target-logs"
    target_logs.mkdir()
    (target_logs / "current.log").write_text("current-log\n", encoding="utf-8")

    def fail_checkpoint(_path: Path) -> None:
        raise BackupError("synthetic checkpoint failure")

    monkeypatch.setattr("sluicery.backup._checkpoint_existing_database", fail_checkpoint)
    with pytest.raises(BackupError, match="checkpoint"):
        restore_backup(
            archive_path=result.archive_path,
            db_path=target_db,
            config_dir=target_config,
            log_dir=target_logs,
            secret_key=secret_key,
        )

    assert _marker(target_db) == "current-state"
    assert (target_config / "current.yaml").read_text(encoding="utf-8") == "current\n"
    assert not (target_config / "hooks.yaml").exists()
    assert (target_logs / "current.log").read_text(encoding="utf-8") == "current-log\n"


def test_makefile_requires_confirmation_backup_and_migration_check() -> None:
    makefile = (Path(__file__).resolve().parents[1] / "Makefile").read_text(
        encoding="utf-8"
    )
    assert "BACKUP_LABEL=pre-restore" in makefile
    assert 'read -p "restoreを続行しますか？ [y/N] "' in makefile
    assert "sluicery.cli db upgrade" in makefile
    assert "sluicery.cli db current" in makefile
    assert "--allow-secret-key-mismatch" not in makefile
    assert "ALLOW_SECRET_KEY_MISMATCH" not in makefile
    assert '-v "$(CONFIG_DIR):/restore/config"' in makefile
    assert 'docker rmi "$(TEST_IMAGE)"' in makefile
    assert "MEDIA_ROOT" not in "\n".join(
        line for line in makefile.splitlines() if "sluicery.backup" in line
    )
