"""StorageAdapter の共通型・パス境界・容量判定。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol

from sluicery.storage.errors import StorageClassification


class StoragePathError(ValueError):
    pass


class StorageOperationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        classification: StorageClassification = StorageClassification.FAILED,
        reason_code: str = "operation_failed",
    ) -> None:
        super().__init__(message)
        self.classification = classification
        self.reason_code = reason_code


class MountStorageNotImplementedError(NotImplementedError):
    pass


class ConnectionStage(StrEnum):
    CONNECTIVITY = "connectivity"
    AUTHENTICATION = "authentication"
    LISTING = "listing"
    WRITE = "write"


class StageStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ConnectionStageResult:
    stage: ConnectionStage
    status: StageStatus
    message: str
    classification: StorageClassification = StorageClassification.OK
    reason_code: str = "ok"


@dataclass(frozen=True)
class ConnectionTestResult:
    stages: tuple[ConnectionStageResult, ...]
    cleanup_warning: str | None = None

    @property
    def ok(self) -> bool:
        return all(
            stage.status in {StageStatus.SUCCESS, StageStatus.NOT_APPLICABLE}
            for stage in self.stages
        ) and self.cleanup_warning is None


@dataclass(frozen=True)
class PublishResult:
    success: bool
    dest_rel: str
    size: int | None
    classification: StorageClassification
    reason_code: str
    message: str
    checksum_sha256: str | None = None
    temporary_rel: str | None = None


@dataclass(frozen=True)
class RemoteFile:
    relative_path: str
    size: int | None
    modified_at: str | None = None
    hashes: dict[str, str] = field(default_factory=dict)


class CapacityState(StrEnum):
    AVAILABLE = "available"
    WARNING = "warning"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CapacityResult:
    state: CapacityState
    free_bytes: int | None

    @property
    def should_block(self) -> bool:
        return self.state == CapacityState.BLOCKED


def evaluate_capacity(
    free_bytes: int | None, *, warn_bytes: int, stop_bytes: int
) -> CapacityResult:
    if warn_bytes < stop_bytes or stop_bytes < 0:
        raise ValueError("容量閾値は 0 <= stop <= warn となるよう指定してください")
    if free_bytes is None:
        return CapacityResult(CapacityState.UNKNOWN, None)
    if free_bytes < stop_bytes:
        return CapacityResult(CapacityState.BLOCKED, free_bytes)
    if free_bytes < warn_bytes:
        return CapacityResult(CapacityState.WARNING, free_bytes)
    return CapacityResult(CapacityState.AVAILABLE, free_bytes)


def validate_relative_path(rel: str, *, allow_empty: bool = False) -> str:
    """Storage root 基準の POSIX 相対パスへ正規化し traversal を拒否する。"""
    if "\x00" in rel:
        raise StoragePathError("Storage 相対パスに NUL は使用できません")
    normalized = rel.replace("\\", "/")
    if normalized in {"", "."}:
        if allow_empty:
            return ""
        raise StoragePathError("Storage 相対パスを空にできません")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise StoragePathError("Storage 相対パスに絶対パスまたは traversal は使用できません")
    parts = [part for part in path.parts if part not in {"", "."}]
    if not parts:
        if allow_empty:
            return ""
        raise StoragePathError("Storage 相対パスを空にできません")
    return PurePosixPath(*parts).as_posix()


class StorageAdapter(Protocol):
    def test_connection(self) -> ConnectionTestResult: ...

    def publish(self, src: Path, dest_rel: str, *, overwrite: bool = False) -> PublishResult: ...

    def exists(self, rel: str) -> bool: ...

    def list_recursive(
        self, rel: str, *, timeout_sec: float | None = None
    ) -> Iterator[RemoteFile]: ...

    def move(self, src_rel: str, dest_rel: str) -> None: ...

    def delete_file(self, rel: str) -> None: ...

    def free_space(self) -> int | None: ...


__all__ = [
    "CapacityResult",
    "CapacityState",
    "ConnectionStage",
    "ConnectionStageResult",
    "ConnectionTestResult",
    "MountStorageNotImplementedError",
    "PublishResult",
    "RemoteFile",
    "StageStatus",
    "StorageAdapter",
    "StorageOperationError",
    "StoragePathError",
    "evaluate_capacity",
    "validate_relative_path",
]
