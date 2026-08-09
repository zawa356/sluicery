"""StorageAdapter factory。"""

from __future__ import annotations

from sluicery.core.settings import OperationalSettings
from sluicery.db.models import Storage, StorageKind
from sluicery.storage.base import (
    MountStorageNotImplementedError,
    StorageAdapter,
)
from sluicery.storage.local import LocalStorageAdapter
from sluicery.storage.rclone import RcloneRunner
from sluicery.storage.remote_rclone import RcloneStorageAdapter


def create_storage_adapter(
    storage: Storage,
    settings: OperationalSettings,
    *,
    rclone_runner: RcloneRunner | None = None,
) -> StorageAdapter:
    config = storage.config_json or {}
    if storage.kind == StorageKind.LOCAL:
        configured_path = config.get("path")
        if not isinstance(configured_path, str):
            raise ValueError("local Storage の path が未設定です")
        return LocalStorageAdapter(configured_path)
    if storage.kind == StorageKind.REMOTE:
        return RcloneStorageAdapter(
            storage.id,
            config,
            storage.credentials_encrypted,
            runner=rclone_runner,
            idle_timeout_sec=settings.storage_idle_timeout_sec,
            absolute_timeout_sec=settings.storage_absolute_timeout_sec,
            test_timeout_sec=settings.storage_test_timeout_sec,
            retries=settings.storage_rclone_retries,
        )
    if storage.kind == StorageKind.MOUNT:
        raise MountStorageNotImplementedError(
            "mount Storage は未実装です（Phase 19 で実装予定）"
        )
    raise ValueError(f"未対応の Storage kind です: {storage.kind}")


__all__ = ["create_storage_adapter"]
