"""固定 bind mount `/mnt/media` 配下へ publish する local adapter。"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

from sluicery.storage.base import (
    ConnectionStage,
    ConnectionStageResult,
    ConnectionTestResult,
    PublishResult,
    RemoteFile,
    StageStatus,
    StorageOperationError,
    StoragePathError,
    validate_relative_path,
)
from sluicery.storage.errors import StorageClassification

MEDIA_MOUNT_ROOT = Path("/mnt/media")
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_HARDLINK_FALLBACK_ERRNOS = {
    errno.EACCES,
    errno.EPERM,
    errno.EXDEV,
    errno.ENOSYS,
    errno.EOPNOTSUPP,
}


def _classification_for_os_error(exc: OSError) -> StorageClassification:
    if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
        return StorageClassification.NO_SPACE
    if exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
        return StorageClassification.PERMISSION_DENIED
    return StorageClassification.FAILED


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage_without_removing_source(src: Path, temp_path: Path) -> None:
    """元を保持したまま一時名を作り、hardlink 非対応時だけ copy する。"""
    try:
        os.link(src, temp_path)
        return
    except OSError as exc:
        if exc.errno not in _HARDLINK_FALLBACK_ERRNOS:
            raise
    with src.open("rb") as source, temp_path.open("xb") as copied:
        shutil.copyfileobj(source, copied)
        copied.flush()
        os.fsync(copied.fileno())


def _rename_noreplace(src: Path, dest: Path) -> None:
    """Linux renameat2(RENAME_NOREPLACE) で競合時の上書きを原子的に拒否する。"""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable", str(dest))
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            _AT_FDCWD,
            os.fsencode(src),
            _AT_FDCWD,
            os.fsencode(dest),
            _RENAME_NOREPLACE,
        )
        == 0
    ):
        return
    error_number = ctypes.get_errno()
    raise OSError(error_number, os.strerror(error_number), str(dest))


class LocalStorageAdapter:
    def __init__(self, configured_path: str, *, media_root: Path = MEDIA_MOUNT_ROOT) -> None:
        # media_root の差替えはユニットテスト専用。factory は常に固定値を使用する。
        boundary = media_root.resolve(strict=False)
        path = Path(configured_path)
        if path.is_absolute():
            root = path.resolve(strict=False)
        else:
            relative = validate_relative_path(configured_path, allow_empty=True)
            root = (boundary / relative).resolve(strict=False)
        if not root.is_relative_to(boundary):
            raise StoragePathError("local Storage は /mnt/media の外を参照できません")
        self._media_root = boundary
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, rel: str, *, allow_empty: bool = False) -> Path:
        normalized = validate_relative_path(rel, allow_empty=allow_empty)
        path = (self._root / normalized).resolve(strict=False)
        if not path.is_relative_to(self._root):
            raise StoragePathError("local Storage root の外を参照できません")
        return path

    def test_connection(self) -> ConnectionTestResult:
        stages: list[ConnectionStageResult] = []
        if not self._root.exists() or not self._root.is_dir():
            stages.append(
                ConnectionStageResult(
                    ConnectionStage.CONNECTIVITY,
                    StageStatus.FAILED,
                    "保存先ディレクトリが存在しません",
                    StorageClassification.UNREACHABLE,
                    "path_not_found",
                )
            )
            stages.append(
                ConnectionStageResult(
                    ConnectionStage.AUTHENTICATION,
                    StageStatus.NOT_APPLICABLE,
                    "local Storage では認証は該当しません",
                )
            )
            for stage in (ConnectionStage.LISTING, ConnectionStage.WRITE):
                stages.append(
                    ConnectionStageResult(
                        stage,
                        StageStatus.SKIPPED,
                        "前段階が失敗したため実行していません",
                        StorageClassification.UNREACHABLE,
                        "prerequisite_failed",
                    )
                )
            return ConnectionTestResult(tuple(stages))

        stages.append(
            ConnectionStageResult(
                ConnectionStage.CONNECTIVITY, StageStatus.SUCCESS, "保存先が存在します"
            )
        )
        stages.append(
            ConnectionStageResult(
                ConnectionStage.AUTHENTICATION,
                StageStatus.NOT_APPLICABLE,
                "local Storage では認証は該当しません",
            )
        )
        try:
            with os.scandir(self._root) as entries:
                next(entries, None)
        except OSError as exc:
            classification = _classification_for_os_error(exc)
            stages.append(
                ConnectionStageResult(
                    ConnectionStage.LISTING,
                    StageStatus.FAILED,
                    "保存先ディレクトリを読み取れません",
                    classification,
                    "listing_failed",
                )
            )
            stages.append(
                ConnectionStageResult(
                    ConnectionStage.WRITE,
                    StageStatus.SKIPPED,
                    "一覧取得が失敗したため実行していません",
                    classification,
                    "prerequisite_failed",
                )
            )
            return ConnectionTestResult(tuple(stages))

        stages.append(
            ConnectionStageResult(
                ConnectionStage.LISTING, StageStatus.SUCCESS, "ディレクトリを読み取れます"
            )
        )
        test_file = self._root / f".sluicery-connection-test-{uuid4().hex}"
        created = False
        cleanup_warning: str | None = None
        try:
            payload = uuid4().bytes
            with test_file.open("xb") as output:
                created = True
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            if test_file.read_bytes() != payload:
                raise OSError("テストファイルの読み出し内容が一致しません")
            test_file.unlink()
            created = False
            stages.append(
                ConnectionStageResult(
                    ConnectionStage.WRITE,
                    StageStatus.SUCCESS,
                    "作成・読み出し・削除に成功しました",
                )
            )
        except OSError as exc:
            classification = _classification_for_os_error(exc)
            stages.append(
                ConnectionStageResult(
                    ConnectionStage.WRITE,
                    StageStatus.FAILED,
                    "テストファイルの作成・読み出し・削除に失敗しました",
                    classification,
                    "write_test_failed",
                )
            )
        finally:
            if created:
                try:
                    test_file.unlink()
                except OSError:
                    cleanup_warning = (
                        "接続テストの一時ファイルを削除できませんでした: " + test_file.name
                    )
        return ConnectionTestResult(tuple(stages), cleanup_warning)

    def publish(self, src: Path, dest_rel: str, *, overwrite: bool = False) -> PublishResult:
        normalized = validate_relative_path(dest_rel)
        final_path = self._path(normalized)
        if not src.is_file():
            return PublishResult(
                False,
                normalized,
                None,
                StorageClassification.FAILED,
                "source_not_found",
                "publish 元ファイルが存在しません",
            )
        if final_path.exists() and not overwrite:
            return PublishResult(
                False,
                normalized,
                final_path.stat().st_size,
                StorageClassification.FAILED,
                "destination_exists",
                "同名ファイルが既に存在するため上書きしません",
            )

        final_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = final_path.with_name(f"{final_path.name}.sluicery-tmp-{uuid4().hex}")
        temp_rel = temp_path.relative_to(self._root).as_posix()
        source_size = src.stat().st_size
        source_hash = _sha256(src)
        try:
            _stage_without_removing_source(src, temp_path)
            if temp_path.stat().st_size != source_size or _sha256(temp_path) != source_hash:
                return PublishResult(
                    False,
                    normalized,
                    temp_path.stat().st_size,
                    StorageClassification.FAILED,
                    "verification_failed",
                    "一時ファイルのサイズまたはチェックサムが一致しません",
                    temporary_rel=temp_rel,
                )
            if final_path.exists() and not overwrite:
                return PublishResult(
                    False,
                    normalized,
                    source_size,
                    StorageClassification.FAILED,
                    "destination_exists",
                    "最終化前に同名ファイルが作成されたため上書きしません",
                    checksum_sha256=source_hash,
                    temporary_rel=temp_rel,
                )
            try:
                if overwrite:
                    os.replace(temp_path, final_path)
                else:
                    _rename_noreplace(temp_path, final_path)
            except FileExistsError:
                return PublishResult(
                    False,
                    normalized,
                    source_size,
                    StorageClassification.FAILED,
                    "destination_exists",
                    "最終化時に同名ファイルが作成されたため上書きしません",
                    checksum_sha256=source_hash,
                    temporary_rel=temp_rel,
                )
            try:
                src.unlink()
            except OSError:
                return PublishResult(
                    True,
                    normalized,
                    source_size,
                    StorageClassification.OK,
                    "ok_source_retained",
                    "最終名への配置は成功しましたが、publish 元を削除できませんでした",
                    checksum_sha256=source_hash,
                )
            return PublishResult(
                True,
                normalized,
                source_size,
                StorageClassification.OK,
                "ok",
                "一時名で検証後、最終名へ配置しました",
                checksum_sha256=source_hash,
            )
        except OSError as exc:
            return PublishResult(
                False,
                normalized,
                temp_path.stat().st_size if temp_path.exists() else None,
                _classification_for_os_error(exc),
                "publish_failed",
                "publish に失敗しました。一時ファイルがあれば自動削除していません",
                temporary_rel=temp_rel if temp_path.exists() else None,
            )

    def exists(self, rel: str) -> bool:
        return self._path(rel).exists()

    def list_recursive(self, rel: str) -> Iterator[RemoteFile]:
        base = self._path(rel, allow_empty=True)
        if not base.exists():
            return
        try:
            for path in base.rglob("*"):
                resolved = path.resolve(strict=False)
                if not resolved.is_relative_to(self._root):
                    raise StoragePathError("Storage root 外を指す symlink は一覧できません")
                if path.is_file():
                    stat = path.stat()
                    yield RemoteFile(
                        path.relative_to(self._root).as_posix(),
                        stat.st_size,
                        str(stat.st_mtime),
                    )
        except OSError as exc:
            raise StorageOperationError(
                "local Storage の一覧取得に失敗しました",
                classification=_classification_for_os_error(exc),
                reason_code="listing_failed",
            ) from exc

    def move(self, src_rel: str, dest_rel: str) -> None:
        src = self._path(src_rel)
        dest = self._path(dest_rel)
        if not src.exists():
            raise StorageOperationError("移動元が存在しません", reason_code="source_not_found")
        if dest.exists():
            raise StorageOperationError("移動先が既に存在します", reason_code="destination_exists")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.rename(src, dest)
        except OSError as exc:
            raise StorageOperationError(
                "local Storage 内の移動に失敗しました",
                classification=_classification_for_os_error(exc),
                reason_code="move_failed",
            ) from exc

    def free_space(self) -> int | None:
        try:
            return shutil.disk_usage(self._root).free
        except OSError:
            return None


__all__ = ["LocalStorageAdapter", "MEDIA_MOUNT_ROOT"]
