"""SQLite・config・任意logの安全なバックアップ／リストア。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO
from uuid import uuid4

from cryptography.fernet import Fernet

from sluicery.db.crypto import FINGERPRINT_SETTING_KEY, secret_key_fingerprint

ARCHIVE_SCHEMA_VERSION = 1
MAX_ARCHIVE_MEMBERS = 100_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 20 * 1024**3
MAX_MANIFEST_BYTES = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_LABEL = re.compile(r"[a-z0-9][a-z0-9-]{0,31}\Z")


class BackupError(RuntimeError):
    pass


class SecretKeyMismatchError(BackupError):
    pass


@dataclass(frozen=True)
class BackupResult:
    archive_path: Path
    file_count: int
    total_bytes: int
    included_logs: bool


@dataclass(frozen=True)
class RestoreResult:
    database_path: Path
    config_files: int
    log_files: int
    secret_key_matched: bool


@dataclass(frozen=True)
class _ArchiveFile:
    path: str
    source: Path
    size: int
    sha256: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_hmac(manifest: dict[str, object], secret_key: str) -> str:
    canonical = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    key_bytes = base64.urlsafe_b64decode(secret_key.encode("ascii"))
    return hmac.new(
        key_bytes,
        b"sluicery-backup-manifest-v1\0" + canonical,
        hashlib.sha256,
    ).hexdigest()


def _safe_tree_files(root: Path, prefix: str) -> Iterator[_ArchiveFile]:
    if not root.is_dir() or root.is_symlink():
        raise BackupError(f"{prefix} directoryが存在しないか安全ではありません")
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for directory in directories:
            if (current_path / directory).is_symlink():
                raise BackupError(f"{prefix} directory内のsymlinkは保存できません")
        for filename in files:
            source = current_path / filename
            if source.is_symlink() or not source.is_file():
                raise BackupError(f"{prefix} directory内の特殊fileは保存できません")
            relative = source.relative_to(root).as_posix()
            archive_path = f"{prefix}/{relative}"
            yield _ArchiveFile(
                archive_path,
                source,
                source.stat().st_size,
                _sha256_file(source),
            )


def _database_fingerprint(db_path: Path) -> str | None:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='setting'"
            ).fetchone()
            if table is None:
                return None
            row = connection.execute(
                "SELECT value_json FROM setting WHERE key = ?",
                (FINGERPRINT_SETTING_KEY,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise BackupError("SQLite DBを検査できません") from exc
    if row is None:
        return None
    value = row[0]
    return value if isinstance(value, str) else None


def _validate_database(db_path: Path) -> None:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as exc:
        raise BackupError("SQLite DBの整合性検査に失敗しました") from exc
    if result != ("ok",):
        raise BackupError("SQLite DBのquick_checkが正常ではありません")


def _snapshot_database(source_path: Path, destination_path: Path) -> None:
    if not source_path.is_file() or source_path.is_symlink():
        raise BackupError("SQLite DBが存在しないか安全ではありません")
    source_uri = f"{source_path.resolve().as_uri()}?mode=ro"
    try:
        with sqlite3.connect(source_uri, uri=True) as source:
            with sqlite3.connect(destination_path) as destination:
                source.backup(destination)
                destination.execute("PRAGMA journal_mode=DELETE")
    except sqlite3.Error as exc:
        raise BackupError("SQLite backup APIによるsnapshot作成に失敗しました") from exc
    _validate_database(destination_path)


def _tar_info(name: str, size: int, timestamp: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = timestamp
    info.mode = 0o600
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def create_backup(
    *,
    db_path: Path,
    config_dir: Path,
    log_dir: Path,
    output_dir: Path,
    secret_key: str,
    include_logs: bool = False,
    label: str | None = None,
    now: datetime | None = None,
) -> BackupResult:
    """稼働中SQLiteから一貫したsnapshotを作り、atomicにarchiveを確定する。"""
    Fernet(secret_key.encode("utf-8"))
    if label is not None and _LABEL.fullmatch(label) is None:
        raise BackupError("backup labelが不正です")
    output_dir.mkdir(parents=True, exist_ok=True)
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise BackupError("backup出力先が安全なdirectoryではありません")
    os.chmod(output_dir, 0o700)

    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    fingerprint = secret_key_fingerprint(secret_key)
    database_fingerprint = _database_fingerprint(db_path)
    if database_fingerprint is not None and database_fingerprint != fingerprint:
        raise SecretKeyMismatchError(
            "現在のSECRET_KEYがDB記録の指紋と一致しないためbackupを中止しました"
        )

    with tempfile.TemporaryDirectory(prefix="sluicery-backup-") as temp_name:
        temp_dir = Path(temp_name)
        database_snapshot = temp_dir / "sluicery.db"
        _snapshot_database(db_path, database_snapshot)
        files = [
            _ArchiveFile(
                "database/sluicery.db",
                database_snapshot,
                database_snapshot.stat().st_size,
                _sha256_file(database_snapshot),
            ),
            *_safe_tree_files(config_dir, "config"),
        ]
        if include_logs and log_dir.exists():
            files.extend(_safe_tree_files(log_dir, "logs"))
        files.sort(key=lambda item: item.path)
        total_bytes = sum(item.size for item in files)
        if total_bytes > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise BackupError("backup対象がuncompressed size上限を超えています")

        manifest: dict[str, object] = {
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "created_at": timestamp.isoformat().replace("+00:00", "Z"),
            "secret_key_fingerprint": fingerprint,
            "includes_logs": include_logs,
            "files": [
                {"path": item.path, "size": item.size, "sha256": item.sha256}
                for item in files
            ],
        }
        manifest["manifest_hmac_sha256"] = _manifest_hmac(manifest, secret_key)
        manifest_bytes = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise BackupError("backup manifestが上限を超えています")

        stem = timestamp.strftime("%Y%m%dT%H%M%SZ")
        label_part = f"-{label}" if label else ""
        output_path = output_dir / (
            f"sluicery-{stem}{label_part}-{uuid4().hex[:8]}.tar.gz"
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".sluicery-backup-", suffix=".tmp", dir=output_dir
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            with tarfile.open(temporary_path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
                archive.addfile(
                    _tar_info("manifest.json", len(manifest_bytes), int(timestamp.timestamp())),
                    io.BytesIO(manifest_bytes),
                )
                for item in files:
                    with item.source.open("rb") as source:
                        archive.addfile(
                            _tar_info(item.path, item.size, int(timestamp.timestamp())),
                            source,
                        )
            os.chmod(temporary_path, 0o600)
            with temporary_path.open("rb") as archive_file:
                os.fsync(archive_file.fileno())
            os.replace(temporary_path, output_path)
            directory_fd = os.open(output_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary_path.unlink(missing_ok=True)
    return BackupResult(output_path, len(files), total_bytes, include_logs)


def _validate_member_name(name: str) -> str:
    if "\\" in name:
        raise BackupError("archive member pathが不正です")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != name:
        raise BackupError("archive member pathが不正です")
    if name == "manifest.json" or name == "database/sluicery.db":
        return name
    if len(path.parts) >= 2 and path.parts[0] in {"config", "logs"}:
        return name
    raise BackupError("archiveに許可されていないmemberがあります")


def _copy_member(source: IO[bytes], target: Path, expected_size: int) -> str:
    digest = hashlib.sha256()
    written = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as output:
        while chunk := source.read(1024 * 1024):
            written += len(chunk)
            if written > expected_size:
                raise BackupError("archive member sizeが宣言値を超えました")
            digest.update(chunk)
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    if written != expected_size:
        raise BackupError("archive member sizeが宣言値と一致しません")
    return digest.hexdigest()


def _read_archive(
    archive_path: Path,
    staging_dir: Path,
    *,
    secret_key: str,
) -> dict[str, object]:
    if not archive_path.is_file() or archive_path.is_symlink():
        raise BackupError("backup archiveが存在しないか安全ではありません")
    try:
        archive = tarfile.open(archive_path, "r:*")
    except (tarfile.TarError, OSError) as exc:
        raise BackupError("backup archiveを開けません") from exc
    with archive:
        members = archive.getmembers()
        if not members or len(members) > MAX_ARCHIVE_MEMBERS:
            raise BackupError("archive member数が不正です")
        names: set[str] = set()
        total_size = 0
        by_name: dict[str, tarfile.TarInfo] = {}
        for member in members:
            name = _validate_member_name(member.name)
            if name in names or not member.isfile() or member.size < 0:
                raise BackupError("archive memberの型・重複が不正です")
            names.add(name)
            by_name[name] = member
            total_size += member.size
            if total_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise BackupError("archiveのuncompressed sizeが上限を超えています")

        manifest_member = by_name.get("manifest.json")
        if manifest_member is None or manifest_member.size > MAX_MANIFEST_BYTES:
            raise BackupError("manifestが存在しないか上限を超えています")
        manifest_source = archive.extractfile(manifest_member)
        if manifest_source is None:
            raise BackupError("manifestを読み取れません")
        try:
            manifest = json.loads(manifest_source.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupError("manifest JSONが不正です") from exc
        if not isinstance(manifest, dict) or set(manifest) != {
            "schema_version",
            "created_at",
            "secret_key_fingerprint",
            "includes_logs",
            "files",
            "manifest_hmac_sha256",
        }:
            raise BackupError("manifest schemaが不正です")
        if manifest["schema_version"] != ARCHIVE_SCHEMA_VERSION:
            raise BackupError("未対応のbackup schema versionです")
        if not isinstance(manifest["created_at"], str):
            raise BackupError("manifest created_atが不正です")
        fingerprint = manifest["secret_key_fingerprint"]
        if not isinstance(fingerprint, str) or _SHA256.fullmatch(fingerprint) is None:
            raise BackupError("manifestのSECRET_KEY指紋が不正です")
        if not isinstance(manifest["includes_logs"], bool):
            raise BackupError("manifest includes_logsが不正です")
        supplied_hmac = manifest["manifest_hmac_sha256"]
        if not isinstance(supplied_hmac, str) or _SHA256.fullmatch(supplied_hmac) is None:
            raise BackupError("manifest HMACが不正です")
        current_fingerprint = secret_key_fingerprint(secret_key)
        if fingerprint != current_fingerprint:
            raise SecretKeyMismatchError(
                "backupのSECRET_KEY指紋が現在の鍵と一致しません。"
                "復号不能になるためrestoreを中止しました"
            )
        unsigned_manifest = dict(manifest)
        del unsigned_manifest["manifest_hmac_sha256"]
        expected_hmac = _manifest_hmac(unsigned_manifest, secret_key)
        if not hmac.compare_digest(supplied_hmac, expected_hmac):
            raise BackupError("manifest HMACが一致しません")
        entries = manifest["files"]
        if not isinstance(entries, list):
            raise BackupError("manifest filesが不正です")

        expected: dict[str, tuple[int, str]] = {}
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
                raise BackupError("manifest file entryが不正です")
            path = _validate_member_name(entry["path"] if isinstance(entry["path"], str) else "")
            size = entry["size"]
            sha256 = entry["sha256"]
            if (
                path == "manifest.json"
                or path in expected
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
                or not isinstance(sha256, str)
                or _SHA256.fullmatch(sha256) is None
            ):
                raise BackupError("manifest file entryの値が不正です")
            expected[path] = (size, sha256)
        if "database/sluicery.db" not in expected:
            raise BackupError("archiveにSQLite DBがありません")
        if set(by_name) - {"manifest.json"} != set(expected):
            raise BackupError("manifestとarchive memberが一致しません")
        if not manifest["includes_logs"] and any(path.startswith("logs/") for path in expected):
            raise BackupError("manifestのlogs指定がmemberと一致しません")

        for path, (size, sha256) in expected.items():
            member = by_name[path]
            if member.size != size:
                raise BackupError("manifestとarchive member sizeが一致しません")
            source = archive.extractfile(member)
            if source is None:
                raise BackupError("archive memberを読み取れません")
            actual = _copy_member(source, staging_dir / path, size)
            if actual != sha256:
                raise BackupError("archive memberのSHA-256が一致しません")
    _validate_database(staging_dir / "database/sluicery.db")
    return manifest


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, target)
        _fsync_directory(target.parent)
    finally:
        temporary_path.unlink(missing_ok=True)


def _validate_target_tree(target_root: Path) -> None:
    target_root.mkdir(parents=True, exist_ok=True)
    if target_root.is_symlink() or not target_root.is_dir():
        raise BackupError("restore先directoryが安全ではありません")
    for current in target_root.rglob("*"):
        if current.is_symlink() or not (current.is_file() or current.is_dir()):
            raise BackupError("restore先directory内にsymlinkまたは特殊fileがあります")


def _restore_tree(staged_root: Path, target_root: Path, *, exact: bool) -> int:
    _validate_target_tree(target_root)
    staged_files = {
        path.relative_to(staged_root): path
        for path in staged_root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    for relative in staged_files:
        destination = target_root / relative
        if destination.exists() and destination.is_dir():
            raise BackupError("restore先のfile / directory型がbackupと一致しません")
        parent = destination.parent
        while parent != target_root:
            if parent.exists() and not parent.is_dir():
                raise BackupError("restore先のfile / directory型がbackupと一致しません")
            parent = parent.parent
    for relative, source in sorted(staged_files.items(), key=lambda item: item[0].as_posix()):
        _atomic_copy(source, target_root / relative)
    if exact:
        for current in sorted(
            target_root.rglob("*"), key=lambda path: len(path.parts), reverse=True
        ):
            relative = current.relative_to(target_root)
            if current.is_symlink() or (current.is_file() and relative not in staged_files):
                current.unlink()
            elif current.is_dir():
                try:
                    current.rmdir()
                except OSError:
                    pass
        _fsync_directory(target_root)
    return len(staged_files)


def _checkpoint_existing_database(db_path: Path) -> None:
    if not db_path.exists():
        return
    if db_path.is_symlink() or not db_path.is_file():
        raise BackupError("既存SQLite DBが安全な通常fileではありません")
    try:
        with sqlite3.connect(db_path) as connection:
            result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.Error as exc:
        raise BackupError("既存SQLite DBのWAL checkpointに失敗しました") from exc
    if result is None or result[0] != 0:
        raise BackupError("既存SQLite DBのWALが使用中です。全serviceを停止してください")


def restore_backup(
    *,
    archive_path: Path,
    db_path: Path,
    config_dir: Path,
    log_dir: Path,
    secret_key: str,
) -> RestoreResult:
    """全member検証と現DB checkpoint後に、media / Staging / yt-dlpへ触れず復元する。"""
    Fernet(secret_key.encode("utf-8"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sluicery-restore-", dir=db_path.parent) as name:
        staging_dir = Path(name)
        manifest = _read_archive(
            archive_path,
            staging_dir,
            secret_key=secret_key,
        )
        expected = manifest["secret_key_fingerprint"]
        actual = secret_key_fingerprint(secret_key)
        matched = expected == actual
        restored_db = staging_dir / "database/sluicery.db"
        database_fingerprint = _database_fingerprint(restored_db)
        if database_fingerprint is not None and database_fingerprint != expected:
            raise BackupError("backup DBとmanifestのSECRET_KEY指紋が一致しません")

        _validate_target_tree(config_dir)
        if (staging_dir / "logs").exists():
            _validate_target_tree(log_dir)
        if db_path.exists() and (db_path.is_symlink() or not db_path.is_file()):
            raise BackupError("既存SQLite DBが安全な通常fileではありません")
        # checkpoint失敗時にDBだけ旧状態、configだけ新状態となる部分適用を防ぐ。
        # 全target検証と同様、最初の書込みより前に実行する。
        _checkpoint_existing_database(db_path)
        config_files = _restore_tree(staging_dir / "config", config_dir, exact=True)
        logs_root = staging_dir / "logs"
        log_files = _restore_tree(logs_root, log_dir, exact=False) if logs_root.exists() else 0
        _atomic_copy(restored_db, db_path)
        for suffix in ("-wal", "-shm"):
            Path(f"{db_path}{suffix}").unlink(missing_ok=True)
        _fsync_directory(db_path.parent)
        _validate_database(db_path)
    return RestoreResult(db_path, config_files, log_files, matched)


def _secret_key_from_environment() -> str:
    key = os.environ.get("SECRET_KEY", "")
    if not key:
        raise BackupError("SECRET_KEYが未設定です")
    try:
        Fernet(key.encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise BackupError("SECRET_KEYの形式が不正です") from exc
    return key


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m sluicery.backup")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--db", type=Path, default=None)
    create.add_argument("--config", type=Path, default=Path("/app/config"))
    create.add_argument("--logs", type=Path, default=None)
    create.add_argument("--output-dir", type=Path, required=True)
    create.add_argument("--include-logs", action="store_true")
    create.add_argument("--label", default=None)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--archive", type=Path, required=True)
    restore.add_argument("--db", type=Path, default=None)
    restore.add_argument("--config", type=Path, required=True)
    restore.add_argument("--logs", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        secret_key = _secret_key_from_environment()
        data_dir = Path(os.environ.get("DATA_DIR", "/data"))
        db_path = args.db or Path(os.environ.get("DB_PATH", data_dir / "sluicery.db"))
        log_dir = args.logs or data_dir / "logs"
        if args.command == "create":
            backup_result = create_backup(
                db_path=db_path,
                config_dir=args.config,
                log_dir=log_dir,
                output_dir=args.output_dir,
                secret_key=secret_key,
                include_logs=args.include_logs,
                label=args.label,
            )
            print(f"backup作成完了: {backup_result.archive_path}")
            print(
                f"files={backup_result.file_count} bytes={backup_result.total_bytes}"
            )
            print(
                "WARNING: SECRET_KEYはarchiveに含まれません。"
                "archiveとは別の安全な場所へ必ず保管してください。",
                file=sys.stderr,
            )
            return 0
        restore_result = restore_backup(
            archive_path=args.archive,
            db_path=db_path,
            config_dir=args.config,
            log_dir=log_dir,
            secret_key=secret_key,
        )
        print(
            f"restore完了: config_files={restore_result.config_files} "
            f"log_files={restore_result.log_files}"
        )
        return 0
    except (BackupError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ARCHIVE_SCHEMA_VERSION",
    "BackupError",
    "BackupResult",
    "RestoreResult",
    "SecretKeyMismatchError",
    "create_backup",
    "main",
    "restore_backup",
]
