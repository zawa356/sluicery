"""rclone remote StorageAdapter（Phase 5 で検証する backend は SMB のみ）。"""

from __future__ import annotations

import json
import re
import tempfile
import time
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from sluicery.runner.base import TimeoutPolicy
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
from sluicery.storage.rclone import RcloneRunner, RcloneRunResult

_OPTION_KEY = re.compile(r"[a-z][a-z0-9_]*\Z")
_MANAGED_OPTIONS = {"type", "host", "user", "pass", "domain", "port"}
_NOT_FOUND = re.compile(r"not found|does not exist|directory not found|object not found", re.I)
_DESTINATION_EXISTS = re.compile(r"already exists|destination exists|immutable file", re.I)


class UnsupportedRemoteProtocolError(ValueError):
    pass


def remote_name_for_storage(storage_id: int) -> str:
    if storage_id < 1:
        raise ValueError("storage.id は1以上である必要があります")
    return f"st{storage_id}"


def _share_name(value: Any) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise StoragePathError("SMB share を指定してください")
    if "/" in value or "\\" in value or "\x00" in value:
        raise StoragePathError("SMB share にパス区切りまたは NUL は使用できません")
    return value


class RcloneStorageAdapter:
    def __init__(
        self,
        storage_id: int,
        config: Mapping[str, Any],
        credentials: Mapping[str, Any] | None,
        *,
        runner: RcloneRunner | None = None,
        idle_timeout_sec: int = 300,
        absolute_timeout_sec: int = 21600,
        test_timeout_sec: float = 30,
        retries: int = 1,
    ) -> None:
        protocol = config.get("protocol")
        if protocol != "smb":
            raise UnsupportedRemoteProtocolError(
                "Phase 5 で実装・検証済みの remote protocol は smb だけです"
            )
        host = config.get("host")
        if not isinstance(host, str) or not host:
            raise ValueError("SMB host を指定してください")
        self._storage_id = storage_id
        self._remote_name = remote_name_for_storage(storage_id)
        self._host = host
        self._share = _share_name(config.get("share"))
        self._base_path = validate_relative_path(str(config.get("path") or ""), allow_empty=True)
        self._port = int(config.get("port") or 445)
        self._options = dict(config.get("options") or {})
        self._credentials = dict(credentials or {})
        self._runner = runner or RcloneRunner()
        self._normal_timeout = TimeoutPolicy(idle_timeout_sec, absolute_timeout_sec, 10)
        self._test_timeout_sec = test_timeout_sec
        self._retries = retries
        self._config_env_cache: dict[str, str] | None = None
        for key in self._options:
            if _OPTION_KEY.fullmatch(key) is None or key in _MANAGED_OPTIONS:
                raise ValueError(f"rclone 追加オプション名が不正です: {key}")

    @property
    def remote_name(self) -> str:
        return self._remote_name

    def _config_env(self, *, timeout_sec: float | None = None) -> dict[str, str]:
        if self._config_env_cache is not None:
            return dict(self._config_env_cache)
        prefix = f"RCLONE_CONFIG_{self._remote_name.upper()}_"
        env = {
            f"{prefix}TYPE": "smb",
            f"{prefix}HOST": self._host,
            f"{prefix}PORT": str(self._port),
        }
        user = self._credentials.get("user")
        password = self._credentials.get("password")
        domain = self._credentials.get("domain")
        if isinstance(user, str) and user:
            env[f"{prefix}USER"] = user
        if isinstance(password, str) and password:
            env[f"{prefix}PASS"] = self._runner.obscure_password(
                password,
                timeout_sec=(self._test_timeout_sec if timeout_sec is None else timeout_sec),
            )
        if isinstance(domain, str) and domain:
            env[f"{prefix}DOMAIN"] = domain
        for key, value in self._options.items():
            env[f"{prefix}{key.upper()}"] = str(value)
        self._config_env_cache = env
        return dict(env)

    def _remote_path(self, rel: str = "") -> str:
        normalized = validate_relative_path(rel, allow_empty=True)
        parts = [self._share]
        if self._base_path:
            parts.append(self._base_path)
        if normalized:
            parts.append(normalized)
        return f"{self._remote_name}:{PurePosixPath(*parts).as_posix()}"

    def _run(
        self,
        args: list[str],
        *,
        stats_interval: str | None = None,
    ) -> RcloneRunResult:
        return self._runner.run(
            args,
            timeout=self._normal_timeout,
            config_env=self._config_env(),
            retries=self._retries,
            stats_interval=stats_interval,
        )

    @staticmethod
    def _remaining_timeout(deadline: float) -> float:
        return max(0.01, deadline - time.monotonic())

    def _run_test(self, args: list[str], *, deadline: float) -> RcloneRunResult:
        remaining = self._remaining_timeout(deadline)
        return self._runner.run(
            args,
            timeout=TimeoutPolicy(remaining, remaining, min(2.0, remaining)),
            config_env=self._config_env(),
            retries=self._retries,
        )

    @staticmethod
    def _raise_result(result: RcloneRunResult, message: str) -> None:
        raise StorageOperationError(
            message,
            classification=result.classification,
            reason_code=result.reason_code,
        )

    @staticmethod
    def _json(result: RcloneRunResult) -> Any:
        try:
            return json.loads("\n".join(result.stdout_lines))
        except (json.JSONDecodeError, ValueError) as exc:
            raise StorageOperationError(
                "rclone の JSON 出力を解釈できません", reason_code="invalid_json"
            ) from exc

    def test_connection(self) -> ConnectionTestResult:
        stages: list[ConnectionStageResult] = []
        deadline = time.monotonic() + self._test_timeout_sec
        # 初回の password obscure も接続テスト全体の deadline に含める。
        self._config_env(timeout_sec=self._remaining_timeout(deadline))
        listing = self._run_test(
            ["lsjson", self._remote_path(), "--max-depth", "1", "--dirs-only"],
            deadline=deadline,
        )
        if listing.returncode != 0:
            classification = listing.classification
            if classification == StorageClassification.UNREACHABLE:
                stages.append(
                    ConnectionStageResult(
                        ConnectionStage.CONNECTIVITY,
                        StageStatus.FAILED,
                        "SMB ホストへ到達できません",
                        classification,
                        listing.reason_code,
                    )
                )
                auth_status = StageStatus.SKIPPED
            else:
                stages.append(
                    ConnectionStageResult(
                        ConnectionStage.CONNECTIVITY,
                        StageStatus.SUCCESS,
                        "SMB ホストから応答がありました",
                    )
                )
                auth_status = (
                    StageStatus.FAILED
                    if classification == StorageClassification.AUTH_FAILED
                    else StageStatus.SUCCESS
                )
            stages.append(
                ConnectionStageResult(
                    ConnectionStage.AUTHENTICATION,
                    auth_status,
                    (
                        "認証情報が拒否されました"
                        if auth_status == StageStatus.FAILED
                        else "前段階の失敗により認証を判定できません"
                        if auth_status == StageStatus.SKIPPED
                        else "認証情報は受け入れられました"
                    ),
                    (
                        classification
                        if auth_status != StageStatus.SUCCESS
                        else StorageClassification.OK
                    ),
                    listing.reason_code if auth_status != StageStatus.SUCCESS else "ok",
                )
            )
            stages.append(
                ConnectionStageResult(
                    ConnectionStage.LISTING,
                    (
                        StageStatus.SKIPPED
                        if auth_status != StageStatus.SUCCESS
                        else StageStatus.FAILED
                    ),
                    (
                        "前段階の失敗により一覧取得を実行できません"
                        if auth_status != StageStatus.SUCCESS
                        else "対象パスの一覧を取得できません"
                    ),
                    classification,
                    listing.reason_code,
                )
            )
            stages.append(
                ConnectionStageResult(
                    ConnectionStage.WRITE,
                    StageStatus.SKIPPED,
                    "一覧取得までに失敗したため実行していません",
                    classification,
                    "prerequisite_failed",
                )
            )
            return ConnectionTestResult(tuple(stages))

        stages.extend(
            (
                ConnectionStageResult(
                    ConnectionStage.CONNECTIVITY,
                    StageStatus.SUCCESS,
                    "SMB ホストから応答がありました",
                ),
                ConnectionStageResult(
                    ConnectionStage.AUTHENTICATION,
                    StageStatus.SUCCESS,
                    "認証情報は受け入れられました",
                ),
                ConnectionStageResult(
                    ConnectionStage.LISTING,
                    StageStatus.SUCCESS,
                    "対象パスの一覧を取得できました",
                ),
            )
        )

        test_name = f".sluicery-connection-test-{uuid4().hex}"
        remote_test = self._remote_path(test_name)
        payload = uuid4().hex.encode("ascii")
        uploaded = False
        cleanup_warning: str | None = None
        with tempfile.TemporaryDirectory(prefix="sluicery-storage-test-") as temp_dir:
            source = Path(temp_dir) / "payload"
            source.write_bytes(payload)
            copy_result = self._run_test(
                ["copyto", str(source), remote_test], deadline=deadline
            )
            if copy_result.returncode == 0:
                uploaded = True
                read_result = self._run_test(["cat", remote_test], deadline=deadline)
            else:
                read_result = copy_result
            if copy_result.returncode == 0 and read_result.returncode == 0:
                read_back = "\n".join(read_result.stdout_lines).encode()
                write_ok = read_back == payload
                if write_ok:
                    result_for_error = read_result
                else:
                    result_for_error = RcloneRunResult(
                        returncode=1,
                        classification=StorageClassification.FAILED,
                        reason_code="content_mismatch",
                    )
            else:
                write_ok = False
                result_for_error = copy_result if copy_result.returncode != 0 else read_result

        if uploaded:
            delete_result = self._run_test(["deletefile", remote_test], deadline=deadline)
            if delete_result.returncode != 0:
                cleanup_warning = (
                    "接続テストの一時ファイルを削除できませんでした: " + test_name
                )
                write_ok = False
                result_for_error = delete_result

        if write_ok:
            stages.append(
                ConnectionStageResult(
                    ConnectionStage.WRITE,
                    StageStatus.SUCCESS,
                    "作成・読み出し・削除に成功しました",
                )
            )
        else:
            stages.append(
                ConnectionStageResult(
                    ConnectionStage.WRITE,
                    StageStatus.FAILED,
                    "テストファイルの作成・読み出し・削除に失敗しました",
                    result_for_error.classification,
                    result_for_error.reason_code,
                )
            )
        return ConnectionTestResult(tuple(stages), cleanup_warning)

    def publish(self, src: Path, dest_rel: str, *, overwrite: bool = False) -> PublishResult:
        normalized = validate_relative_path(dest_rel)
        if not src.is_file():
            return PublishResult(
                False,
                normalized,
                None,
                StorageClassification.FAILED,
                "source_not_found",
                "publish 元ファイルが存在しません",
            )
        if self.exists(normalized) and not overwrite:
            return PublishResult(
                False,
                normalized,
                None,
                StorageClassification.FAILED,
                "destination_exists",
                "同名ファイルが既に存在するため上書きしません",
            )

        temp_rel = f"{normalized}.sluicery-tmp-{uuid4().hex}"
        parent = PurePosixPath(normalized).parent.as_posix()
        if parent != ".":
            mkdir_result = self._run(["mkdir", self._remote_path(parent)])
            if mkdir_result.returncode != 0:
                return PublishResult(
                    False,
                    normalized,
                    None,
                    mkdir_result.classification,
                    mkdir_result.reason_code,
                    "転送先ディレクトリを作成できません",
                )
        copy_result = self._run(
            ["copyto", str(src), self._remote_path(temp_rel)], stats_interval="1s"
        )
        if copy_result.returncode != 0:
            return PublishResult(
                False,
                normalized,
                None,
                copy_result.classification,
                copy_result.reason_code,
                "一時名への転送に失敗しました。残った一時ファイルは自動削除しません",
                temporary_rel=temp_rel,
            )

        stat_result = self._run(["lsjson", self._remote_path(temp_rel), "--stat"])
        if stat_result.returncode != 0:
            return PublishResult(
                False,
                normalized,
                None,
                stat_result.classification,
                stat_result.reason_code,
                "転送後のサイズ確認に失敗しました",
                temporary_rel=temp_rel,
            )
        payload = self._json(stat_result)
        remote_size = payload.get("Size") if isinstance(payload, dict) else None
        if remote_size != src.stat().st_size:
            return PublishResult(
                False,
                normalized,
                int(remote_size) if isinstance(remote_size, int | float) else None,
                StorageClassification.FAILED,
                "verification_failed",
                "転送元と一時ファイルのサイズが一致しません",
                temporary_rel=temp_rel,
            )
        if self.exists(normalized) and not overwrite:
            return PublishResult(
                False,
                normalized,
                int(remote_size),
                StorageClassification.FAILED,
                "destination_exists",
                "最終化前に同名ファイルが作成されたため上書きしません",
                temporary_rel=temp_rel,
            )
        move_args = ["moveto", self._remote_path(temp_rel), self._remote_path(normalized)]
        if not overwrite:
            move_args.append("--ignore-existing")
        move_result = self._run(move_args)
        if move_result.returncode != 0:
            if not overwrite and _DESTINATION_EXISTS.search(move_result.stderr_tail):
                return PublishResult(
                    False,
                    normalized,
                    int(remote_size),
                    StorageClassification.FAILED,
                    "destination_exists",
                    "最終化時に同名ファイルが作成されたため上書きしません",
                    temporary_rel=temp_rel,
                )
            return PublishResult(
                False,
                normalized,
                int(remote_size),
                move_result.classification,
                move_result.reason_code,
                "一時ファイルを最終名へ移動できません",
                temporary_rel=temp_rel,
            )
        if not overwrite and self.exists(temp_rel):
            return PublishResult(
                False,
                normalized,
                int(remote_size),
                StorageClassification.FAILED,
                "destination_exists",
                "最終化時に同名ファイルが作成されたため上書きしません",
                temporary_rel=temp_rel,
            )
        try:
            src.unlink()
        except OSError:
            return PublishResult(
                True,
                normalized,
                int(remote_size),
                StorageClassification.OK,
                "ok_source_retained",
                "最終名への配置は成功しましたが、publish 元を削除できませんでした",
            )
        return PublishResult(
            True,
            normalized,
            int(remote_size),
            StorageClassification.OK,
            "ok",
            "一時名で転送・検証後、最終名へ配置しました",
        )

    def exists(self, rel: str) -> bool:
        result = self._run(["lsjson", self._remote_path(rel), "--stat"])
        if result.returncode == 0:
            return True
        if _NOT_FOUND.search(result.stderr_tail):
            return False
        self._raise_result(result, "remote Storage の存在確認に失敗しました")
        return False

    def list_recursive(self, rel: str) -> Iterator[RemoteFile]:
        normalized = validate_relative_path(rel, allow_empty=True)
        result = self._run(
            ["lsjson", self._remote_path(normalized), "--recursive", "--files-only", "--hash"]
        )
        if result.returncode != 0:
            self._raise_result(result, "remote Storage の一覧取得に失敗しました")
        payload = self._json(result)
        if not isinstance(payload, list):
            raise StorageOperationError(
                "rclone の一覧出力が配列ではありません", reason_code="invalid_json"
            )
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("Path"), str):
                continue
            item_path = validate_relative_path(item["Path"])
            prefix = f"{normalized}/" if normalized else ""
            relative = f"{prefix}{item_path}"
            size = item.get("Size")
            hashes = item.get("Hashes")
            yield RemoteFile(
                relative,
                int(size) if isinstance(size, int | float) else None,
                item.get("ModTime") if isinstance(item.get("ModTime"), str) else None,
                hashes if isinstance(hashes, dict) else {},
            )

    def move(self, src_rel: str, dest_rel: str) -> None:
        source = validate_relative_path(src_rel)
        destination = validate_relative_path(dest_rel)
        if self.exists(destination):
            raise StorageOperationError("移動先が既に存在します", reason_code="destination_exists")
        result = self._run(
            ["moveto", self._remote_path(source), self._remote_path(destination)]
        )
        if result.returncode != 0:
            self._raise_result(result, "remote Storage 内の移動に失敗しました")

    def free_space(self) -> int | None:
        result = self._run(["about", self._remote_path(), "--json"])
        if result.returncode != 0:
            return None
        try:
            payload = self._json(result)
        except StorageOperationError:
            return None
        free = payload.get("free") if isinstance(payload, dict) else None
        return int(free) if isinstance(free, int | float) else None


__all__ = [
    "RcloneStorageAdapter",
    "UnsupportedRemoteProtocolError",
    "remote_name_for_storage",
]
