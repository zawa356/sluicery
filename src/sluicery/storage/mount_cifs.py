"""明示的なprivileged overlay専用のCIFS / NFSカーネルmount Adapter。"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from sluicery.runner.base import mask_output_text
from sluicery.storage.base import (
    ConnectionStage,
    ConnectionStageResult,
    ConnectionTestResult,
    PublishResult,
    RemoteFile,
    StageStatus,
    StorageAdapter,
    StorageOperationError,
    StoragePathError,
    validate_relative_path,
)
from sluicery.storage.errors import StorageClassification, classify_stderr
from sluicery.storage.local import LocalStorageAdapter

MOUNT_ROOT = Path("/mnt/sluicery-mounts")
MOUNT_RUN_DIR = Path("/run/sluicery")
_OVERLAY_SENTINEL = "enabled-by-compose-overlay"
_CAP_DAC_READ_SEARCH = 2
_CAP_SYS_ADMIN = 21
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\Z")


@dataclass(frozen=True)
class MountStorageConfig:
    protocol: str
    host: str
    share: str
    path: str
    port: int

    @classmethod
    def parse(cls, value: object) -> MountStorageConfig:
        if not isinstance(value, Mapping):
            raise ValueError("mount Storage configが不正です")
        if set(value) != {"protocol", "host", "share", "path", "port"}:
            raise ValueError("mount Storage configの項目が不正です")
        protocol = value.get("protocol")
        host = value.get("host")
        share = value.get("share")
        path = value.get("path")
        port = value.get("port")
        if protocol not in {"cifs", "nfs"}:
            raise ValueError("mount protocolはcifsまたはnfsで指定してください")
        if not isinstance(host, str) or _HOST.fullmatch(host) is None:
            raise ValueError("mount hostが不正です")
        if (
            not isinstance(share, str)
            or not share
            or any(char.isspace() or ord(char) < 32 for char in share)
        ):
            raise ValueError("mount shareが不正です")
        if protocol == "cifs":
            if any(separator in share for separator in "/\\") or share in {".", ".."}:
                raise ValueError("CIFS shareは単一の共有名で指定してください")
        else:
            parsed_share = PurePosixPath(share)
            if not parsed_share.is_absolute() or ".." in parsed_share.parts or "," in share:
                raise ValueError("NFS exportは安全な絶対pathで指定してください")
        if not isinstance(path, str):
            raise ValueError("mount pathが不正です")
        try:
            normalized_path = validate_relative_path(path, allow_empty=True)
        except StoragePathError as exc:
            raise ValueError("mount pathが不正です") from exc
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("mount portが不正です")
        return cls(protocol, host, share, normalized_path, port)

    @property
    def source(self) -> str:
        if self.protocol == "cifs":
            return f"//{self.host}/{self.share}"
        return f"{self.host}:{self.share}"


@dataclass(frozen=True)
class MountCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class MountCommandRunner(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        timeout_sec: float,
        sensitive_values: Sequence[str] = (),
    ) -> MountCommandResult: ...


class SubprocessMountCommandRunner:
    def run(
        self,
        args: Sequence[str],
        *,
        timeout_sec: float,
        sensitive_values: Sequence[str] = (),
    ) -> MountCommandResult:
        default_path = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        env = {
            "PATH": os.environ.get("PATH", default_path),
            "HOME": "/tmp",
            "LC_ALL": "C",
        }
        try:
            result = subprocess.run(
                list(args),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = mask_output_text(
                str(exc.stderr or ""), sensitive_values=sensitive_values
            )
            return MountCommandResult(124, "", stderr or "mount command timed out")
        return MountCommandResult(
            result.returncode,
            mask_output_text(result.stdout, sensitive_values=sensitive_values),
            mask_output_text(result.stderr, sensitive_values=sensitive_values),
        )


def _effective_capabilities(status_path: Path = Path("/proc/self/status")) -> int:
    try:
        for line in status_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("CapEff:"):
                return int(line.split(":", 1)[1].strip(), 16)
    except (OSError, ValueError):
        return 0
    return 0


def mount_storage_available(
    *, env: Mapping[str, str] | None = None, status_path: Path = Path("/proc/self/status")
) -> bool:
    environment = os.environ if env is None else env
    if environment.get("SLUICERY_PRIVILEGED_MOUNT") != _OVERLAY_SENTINEL:
        return False
    capabilities = _effective_capabilities(status_path)
    required = (1 << _CAP_SYS_ADMIN) | (1 << _CAP_DAC_READ_SEARCH)
    return capabilities & required == required


def _safe_credentials(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("CIFS mountには資格情報が必要です")
    credentials = {
        "user": value.get("user"),
        "password": value.get("password"),
        "domain": value.get("domain", ""),
    }
    if not isinstance(credentials["user"], str) or not credentials["user"]:
        raise ValueError("CIFS mountのuserが不正です")
    if not isinstance(credentials["password"], str) or not credentials["password"]:
        raise ValueError("CIFS mountのpasswordが不正です")
    if not isinstance(credentials["domain"], str):
        raise ValueError("CIFS mountのdomainが不正です")
    if any(
        any(character in item for character in "\x00\r\n")
        for item in credentials.values()
    ):
        raise ValueError("CIFS mount資格情報に制御文字は使用できません")
    return credentials


class MountStorageAdapter:
    def __init__(
        self,
        storage_id: int,
        config: object,
        credentials: object,
        *,
        available: bool | None = None,
        runner: MountCommandRunner | None = None,
        mount_root: Path = MOUNT_ROOT,
        run_dir: Path = MOUNT_RUN_DIR,
        timeout_sec: float = 30.0,
    ) -> None:
        if available is None:
            available = mount_storage_available()
        if not available:
            raise StorageOperationError(
                "mount Storageはcompose.privileged.yaml明示指定時だけ利用できます",
                classification=StorageClassification.PERMISSION_DENIED,
                reason_code="privileged_overlay_required",
            )
        if storage_id <= 0:
            raise ValueError("mount Storage IDが不正です")
        self._config = MountStorageConfig.parse(config)
        self._credentials = (
            _safe_credentials(credentials) if self._config.protocol == "cifs" else None
        )
        self._runner = runner or SubprocessMountCommandRunner()
        self._mount_root = mount_root.resolve(strict=False)
        self._mountpoint = self._mount_root / f"storage-{storage_id}"
        self._run_dir = run_dir
        self._timeout_sec = timeout_sec
        configured_root = self._mountpoint / self._config.path
        self._delegate = LocalStorageAdapter(
            str(configured_root), media_root=self._mount_root
        )

    @property
    def mountpoint(self) -> Path:
        return self._mountpoint

    def _run(
        self, args: Sequence[str], *, sensitive_values: Sequence[str] = ()
    ) -> MountCommandResult:
        return self._runner.run(
            args, timeout_sec=self._timeout_sec, sensitive_values=sensitive_values
        )

    def _mounted_state(self) -> tuple[str, str] | None:
        result = self._run(
            [
                "findmnt",
                "--noheadings",
                "--output",
                "SOURCE,FSTYPE",
                "--mountpoint",
                str(self._mountpoint),
            ]
        )
        if result.returncode == 1:
            return None
        if result.returncode != 0:
            raise StorageOperationError(
                "mount状態を確認できません",
                classification=StorageClassification.FAILED,
                reason_code="mount_state_failed",
            )
        fields = result.stdout.strip().split()
        if len(fields) != 2:
            raise StorageOperationError(
                "mount状態の出力が不正です", reason_code="mount_state_invalid"
            )
        return fields[0], fields[1]

    def _state_matches(self, state: tuple[str, str]) -> bool:
        source, filesystem = state
        expected_filesystems = {"cifs"} if self._config.protocol == "cifs" else {"nfs", "nfs4"}
        return source == self._config.source and filesystem in expected_filesystems

    def _mount_args(self, credentials_path: Path | None) -> list[str]:
        if self._config.protocol == "cifs":
            assert credentials_path is not None
            uid = int(os.environ.get("PUID", str(os.getuid())))
            gid = int(os.environ.get("PGID", str(os.getgid())))
            options = (
                f"credentials={credentials_path},uid={uid},gid={gid},"
                f"file_mode=0664,dir_mode=0775,nosuid,nodev,port={self._config.port}"
            )
        else:
            options = f"nosuid,nodev,port={self._config.port}"
        return [
            "mount",
            "-t",
            self._config.protocol,
            self._config.source,
            str(self._mountpoint),
            "-o",
            options,
        ]

    def _perform_mount(self) -> None:
        credentials_path: Path | None = None
        sensitive_values: tuple[str, ...] = ()
        try:
            if self._credentials is not None:
                self._run_dir.mkdir(parents=True, exist_ok=True)
                descriptor, name = tempfile.mkstemp(
                    prefix="mount-credentials-", dir=self._run_dir
                )
                credentials_path = Path(name)
                os.fchmod(descriptor, 0o600)
                content = (
                    f"username={self._credentials['user']}\n"
                    f"password={self._credentials['password']}\n"
                    f"domain={self._credentials['domain']}\n"
                ).encode()
                with os.fdopen(descriptor, "wb") as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                sensitive_values = tuple(self._credentials.values())
            result = self._run(
                self._mount_args(credentials_path), sensitive_values=sensitive_values
            )
        except OSError as exc:
            raise StorageOperationError(
                "mount実行の準備に失敗しました",
                classification=StorageClassification.PERMISSION_DENIED,
                reason_code="mount_setup_failed",
            ) from exc
        finally:
            if credentials_path is not None:
                try:
                    credentials_path.unlink()
                except FileNotFoundError:
                    pass
        if result.returncode != 0:
            classified = classify_stderr(result.stderr)
            if result.returncode == 124:
                classified = type(classified)(
                    StorageClassification.UNREACHABLE, "timeout"
                )
            raise StorageOperationError(
                "kernel mountに失敗しました",
                classification=classified.classification,
                reason_code=classified.reason_code,
            )

    def _ensure_mounted(self) -> None:
        try:
            self._mount_root.mkdir(parents=True, exist_ok=True)
            self._run_dir.mkdir(parents=True, exist_ok=True)
            lock_path = self._run_dir / f"mount-storage-{self._mountpoint.name}.lock"
            with lock_path.open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                state = self._mounted_state()
                if state is not None:
                    if not self._state_matches(state):
                        raise StorageOperationError(
                            "mountpointは別の接続先で使用中です",
                            reason_code="mountpoint_conflict",
                        )
                    return
                if self._mountpoint.is_symlink():
                    raise StoragePathError("mountpointにsymlinkは使用できません")
                self._mountpoint.mkdir(mode=0o700, exist_ok=True)
                if any(self._mountpoint.iterdir()):
                    raise StorageOperationError(
                        "未mountのmountpointが空ではありません",
                        reason_code="mountpoint_not_empty",
                    )
                self._perform_mount()
                mounted = self._mounted_state()
                if mounted is None or not self._state_matches(mounted):
                    raise StorageOperationError(
                        "mount完了後の接続先を確認できません",
                        reason_code="mount_verification_failed",
                    )
        except OSError as exc:
            raise StorageOperationError(
                "mountpointを準備できません",
                classification=StorageClassification.PERMISSION_DENIED,
                reason_code="mountpoint_setup_failed",
            ) from exc

    def test_connection(self) -> ConnectionTestResult:
        try:
            self._ensure_mounted()
        except (StorageOperationError, StoragePathError) as exc:
            classification = getattr(
                exc, "classification", StorageClassification.PERMISSION_DENIED
            )
            reason_code = getattr(exc, "reason_code", "unsafe_mountpoint")
            if classification == StorageClassification.AUTH_FAILED:
                first = ConnectionStageResult(
                    ConnectionStage.CONNECTIVITY,
                    StageStatus.SUCCESS,
                    "接続先には到達しました",
                )
                second = ConnectionStageResult(
                    ConnectionStage.AUTHENTICATION,
                    StageStatus.FAILED,
                    "mount認証に失敗しました",
                    classification,
                    reason_code,
                )
            else:
                first = ConnectionStageResult(
                    ConnectionStage.CONNECTIVITY,
                    StageStatus.FAILED,
                    "kernel mountを確立できません",
                    classification,
                    reason_code,
                )
                second = ConnectionStageResult(
                    ConnectionStage.AUTHENTICATION,
                    StageStatus.SKIPPED,
                    "前段階が失敗したため実行していません",
                    classification,
                    "prerequisite_failed",
                )
            skipped = tuple(
                ConnectionStageResult(
                    stage,
                    StageStatus.SKIPPED,
                    "前段階が失敗したため実行していません",
                    classification,
                    "prerequisite_failed",
                )
                for stage in (ConnectionStage.LISTING, ConnectionStage.WRITE)
            )
            return ConnectionTestResult((first, second, *skipped))
        local_result = self._delegate.test_connection()
        stages = list(local_result.stages)
        stages[0] = ConnectionStageResult(
            ConnectionStage.CONNECTIVITY,
            StageStatus.SUCCESS,
            "kernel mountを確認しました",
        )
        stages[1] = ConnectionStageResult(
            ConnectionStage.AUTHENTICATION,
            StageStatus.SUCCESS,
            "mount認証を確認しました",
        )
        return ConnectionTestResult(tuple(stages), local_result.cleanup_warning)

    def publish(self, src: Path, dest_rel: str, *, overwrite: bool = False) -> PublishResult:
        self._ensure_mounted()
        return self._delegate.publish(src, dest_rel, overwrite=overwrite)

    def exists(self, rel: str) -> bool:
        self._ensure_mounted()
        return self._delegate.exists(rel)

    def list_recursive(
        self, rel: str, *, timeout_sec: float | None = None
    ) -> Iterator[RemoteFile]:
        self._ensure_mounted()
        yield from self._delegate.list_recursive(rel, timeout_sec=timeout_sec)

    def inspect_file(self, rel: str) -> RemoteFile:
        self._ensure_mounted()
        return self._delegate.inspect_file(rel)

    def hardlink_from(
        self,
        source_adapter: StorageAdapter,
        src_rel: str,
        dest_rel: str,
        *,
        expected: RemoteFile,
    ) -> bool:
        self._ensure_mounted()
        if not isinstance(source_adapter, MountStorageAdapter):
            return False
        source_adapter._ensure_mounted()
        return self._delegate.hardlink_from(
            source_adapter._delegate, src_rel, dest_rel, expected=expected
        )

    def move(self, src_rel: str, dest_rel: str) -> None:
        self._ensure_mounted()
        self._delegate.move(src_rel, dest_rel)

    def delete_file(
        self,
        rel: str,
        *,
        expected: RemoteFile | None = None,
        quarantine_rel: str | None = None,
    ) -> None:
        self._ensure_mounted()
        self._delegate.delete_file(
            rel, expected=expected, quarantine_rel=quarantine_rel
        )

    def free_space(self) -> int | None:
        self._ensure_mounted()
        return self._delegate.free_space()


__all__ = [
    "MOUNT_ROOT",
    "MountCommandResult",
    "MountStorageAdapter",
    "MountStorageConfig",
    "SubprocessMountCommandRunner",
    "mount_storage_available",
]
