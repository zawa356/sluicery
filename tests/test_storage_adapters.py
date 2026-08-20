from __future__ import annotations

import errno
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from sluicery.db.models import Storage, StorageKind
from sluicery.runner.base import TimeoutPolicy
from sluicery.storage import create_storage_adapter
from sluicery.storage import local as local_module
from sluicery.storage import remote_rclone as remote_module
from sluicery.storage.base import (
    CapacityState,
    ConnectionStage,
    MountStorageNotImplementedError,
    RemoteFile,
    StageStatus,
    StorageOperationError,
    StoragePathError,
    evaluate_capacity,
    validate_relative_path,
)
from sluicery.storage.errors import StorageClassification
from sluicery.storage.local import LocalStorageAdapter
from sluicery.storage.rclone import RcloneRunResult
from sluicery.storage.remote_rclone import RcloneStorageAdapter, remote_name_for_storage


def _result(
    *,
    returncode: int = 0,
    classification: StorageClassification = StorageClassification.OK,
    reason_code: str = "ok",
    stdout: list[str] | None = None,
    stderr_tail: str = "",
) -> RcloneRunResult:
    return RcloneRunResult(
        returncode=returncode,
        classification=classification,
        reason_code=reason_code,
        stdout_lines=stdout or [],
        stderr_tail=stderr_tail,
    )


class ScriptedRunner:
    def __init__(self, results: Sequence[RcloneRunResult]) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.obscured_inputs: list[str] = []
        self.obscure_timeouts: list[float] = []
        self.timeouts: list[TimeoutPolicy] = []

    def obscure_password(self, password: str, *, timeout_sec: float = 30) -> str:
        self.obscured_inputs.append(password)
        self.obscure_timeouts.append(timeout_sec)
        return "obscured-password"

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: TimeoutPolicy,
        config_env: Mapping[str, str] | None = None,
        retries: int = 1,
        stats_interval: str | None = None,
        on_progress: Any = None,
        cwd: Path | None = None,
    ) -> RcloneRunResult:
        self.calls.append((list(args), dict(config_env or {})))
        self.timeouts.append(timeout)
        return self.results.pop(0)


class ConnectionRunner(ScriptedRunner):
    def __init__(self, *, delete_fails: bool = False, corrupt_read: bool = False) -> None:
        super().__init__([])
        self.payload = ""
        self.delete_fails = delete_fails
        self.corrupt_read = corrupt_read

    def run(self, args: Sequence[str], **kwargs: Any) -> RcloneRunResult:
        self.calls.append((list(args), dict(kwargs.get("config_env") or {})))
        self.timeouts.append(kwargs["timeout"])
        if args[0] == "copyto":
            self.payload = Path(args[1]).read_text()
        if args[0] == "cat":
            return _result(stdout=["corrupted" if self.corrupt_read else self.payload])
        if args[0] == "deletefile" and self.delete_fails:
            return _result(
                returncode=1,
                classification=StorageClassification.PERMISSION_DENIED,
                reason_code="permission_denied",
            )
        return _result(stdout=["[]"] if args[0] == "lsjson" else [])


def _remote_adapter(runner: Any, *, test_timeout_sec: float = 30) -> RcloneStorageAdapter:
    return RcloneStorageAdapter(
        42,
        {
            "protocol": "smb",
            "host": "smb.example.invalid",
            "share": "test-share",
            "path": "library",
        },
        {"user": "test-user", "password": "test-password", "domain": "TEST"},
        runner=runner,
        test_timeout_sec=test_timeout_sec,
    )


@pytest.mark.parametrize("path", ["../escape", "a/../../escape", "/absolute", "..\\escape"])
def test_relative_path_rejects_traversal_and_absolute(path: str) -> None:
    with pytest.raises(StoragePathError):
        validate_relative_path(path)


def test_local_root_must_stay_under_media_boundary(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    media_root.mkdir()
    adapter = LocalStorageAdapter("library", media_root=media_root)
    assert adapter.root == media_root / "library"
    with pytest.raises(StoragePathError):
        LocalStorageAdapter(str(tmp_path / "outside"), media_root=media_root)


def test_local_connection_has_four_distinct_stages(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    storage_root = media_root / "library"
    storage_root.mkdir(parents=True)
    result = LocalStorageAdapter("library", media_root=media_root).test_connection()
    assert result.ok
    assert [stage.stage for stage in result.stages] == list(ConnectionStage)
    assert result.stages[1].status == StageStatus.NOT_APPLICABLE
    assert not list(storage_root.glob(".sluicery-connection-test-*"))


def test_local_connection_reports_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_root = tmp_path / "media"
    (media_root / "library").mkdir(parents=True)
    original_unlink = Path.unlink

    def fail_test_file_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        if path.name.startswith(".sluicery-connection-test-"):
            raise PermissionError("synthetic cleanup failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_test_file_unlink)
    result = LocalStorageAdapter("library", media_root=media_root).test_connection()
    assert not result.ok
    assert result.cleanup_warning is not None
    assert result.stages[-1].status == StageStatus.FAILED


def test_local_publish_uses_temporary_name_then_final(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    (media_root / "library").mkdir(parents=True)
    source = tmp_path / "source.bin"
    source.write_bytes(b"complete-content")
    adapter = LocalStorageAdapter("library", media_root=media_root)
    result = adapter.publish(source, "folder/final.bin")
    assert result.success
    assert source.exists()
    assert (adapter.root / "folder/final.bin").read_bytes() == b"complete-content"
    assert not list(adapter.root.rglob("*.sluicery-tmp-*"))


def test_local_delete_file_removes_only_exact_file(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    target = media_root / "library" / "folder" / "target.bin"
    neighbor = media_root / "library" / "folder" / "neighbor.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"target")
    neighbor.write_bytes(b"neighbor")
    adapter = LocalStorageAdapter("library", media_root=media_root)

    adapter.delete_file("folder/target.bin")

    assert not target.exists()
    assert neighbor.read_bytes() == b"neighbor"


def test_local_delete_file_rejects_symlink_without_deleting_target(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    root = media_root / "library"
    root.mkdir(parents=True)
    target = root / "target.bin"
    target.write_bytes(b"keep")
    link = root / "link.bin"
    link.symlink_to(target.name)
    adapter = LocalStorageAdapter("library", media_root=media_root)

    with pytest.raises(StorageOperationError, match="安全に確認"):
        adapter.delete_file("link.bin")

    assert link.is_symlink()
    assert target.read_bytes() == b"keep"


def test_remote_delete_file_uses_exact_deletefile_command() -> None:
    runner = ScriptedRunner([_result()])
    adapter = _remote_adapter(runner)

    adapter.delete_file("folder/target.bin")

    assert runner.calls[0][0][0] == "deletefile"
    assert runner.calls[0][0][1].endswith("/folder/target.bin")


def test_remote_inspect_file_computes_strong_hash_when_backend_has_none() -> None:
    digest = "a" * 64
    runner = ScriptedRunner(
        [
            _result(
                stdout=[
                    json.dumps(
                        {
                            "Path": "target.bin",
                            "Size": 7,
                            "ModTime": "2026-08-20T00:00:00Z",
                            "Hashes": {},
                        }
                    )
                ]
            ),
            _result(stdout=[f"{digest}  target.bin"]),
        ]
    )

    identity = _remote_adapter(runner).inspect_file("folder/target.bin")

    assert identity.hashes == {"sha256": digest}
    assert [call[0][0] for call in runner.calls] == ["lsjson", "hashsum"]


def test_remote_inspect_file_refuses_when_strong_hash_is_unavailable() -> None:
    runner = ScriptedRunner(
        [
            _result(stdout=[json.dumps({"Path": "target.bin", "Size": 7, "Hashes": {}})]),
            _result(returncode=1),
        ]
    )

    with pytest.raises(StorageOperationError, match="強いhash"):
        _remote_adapter(runner).inspect_file("folder/target.bin")


def test_remote_conditional_delete_quarantines_then_verifies() -> None:
    digest = "a" * 64
    runner = ScriptedRunner(
        [
            _result(returncode=1, stderr_tail="object not found"),
            _result(),
            _result(returncode=1, stderr_tail="object not found"),
            _result(
                stdout=[
                    json.dumps(
                        {
                            "Path": ".sluicery-retention-test",
                            "Size": 7,
                            "ModTime": "2026-08-20T00:00:00Z",
                            "Hashes": {"sha256": digest},
                        }
                    )
                ]
            ),
            _result(),
            _result(returncode=1, stderr_tail="object not found"),
        ]
    )
    expected = RemoteFile(
        "folder/target.bin",
        7,
        "2026-08-20T00:00:00Z",
        {"sha256": digest},
    )

    _remote_adapter(runner).delete_file(
        "folder/target.bin",
        expected=expected,
        quarantine_rel="folder/.sluicery-retention-test",
    )

    assert [call[0][0] for call in runner.calls] == [
        "lsjson",
        "moveto",
        "lsjson",
        "lsjson",
        "deletefile",
        "lsjson",
    ]
    assert runner.calls[-2][0][1].endswith("/folder/.sluicery-retention-test")


def test_local_mismatch_never_overwrites_concurrently_created_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_root = tmp_path / "media"
    root = media_root / "library"
    root.mkdir(parents=True)
    path = root / "target.bin"
    path.write_bytes(b"original")
    adapter = LocalStorageAdapter("library", media_root=media_root)
    expected = adapter.inspect_file("target.bin")
    path.unlink()
    path.write_bytes(b"replacement")
    original_inspect = adapter._inspect_path

    def inspect_with_race(candidate: Path, rel: str) -> RemoteFile:
        identity = original_inspect(candidate, rel)
        path.write_bytes(b"concurrent")
        return identity

    monkeypatch.setattr(adapter, "_inspect_path", inspect_with_race)

    with pytest.raises(StorageOperationError, match="quarantineに保持"):
        adapter.delete_file(
            "target.bin",
            expected=expected,
            quarantine_rel=".sluicery-retention-test",
        )

    assert path.read_bytes() == b"concurrent"
    assert (root / ".sluicery-retention-test").read_bytes() == b"replacement"


def test_local_delete_never_overwrites_concurrently_created_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_root = tmp_path / "media"
    root = media_root / "library"
    root.mkdir(parents=True)
    path = root / "target.bin"
    quarantine = root / ".sluicery-retention-test"
    path.write_bytes(b"original")
    adapter = LocalStorageAdapter("library", media_root=media_root)
    expected = adapter.inspect_file("target.bin")
    original_rename = local_module._rename_noreplace

    def rename_with_race(src: Path, dest: Path) -> None:
        quarantine.write_bytes(b"concurrent")
        original_rename(src, dest)

    monkeypatch.setattr(local_module, "_rename_noreplace", rename_with_race)

    with pytest.raises(StorageOperationError, match="削除に失敗"):
        adapter.delete_file(
            "target.bin",
            expected=expected,
            quarantine_rel=".sluicery-retention-test",
        )

    assert path.read_bytes() == b"original"
    assert quarantine.read_bytes() == b"concurrent"


def test_local_publish_falls_back_to_copy_when_hardlink_is_cross_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_root = tmp_path / "media"
    (media_root / "library").mkdir(parents=True)
    source = tmp_path / "source.bin"
    source.write_bytes(b"cross-device-content")
    adapter = LocalStorageAdapter("library", media_root=media_root)
    original_link = os.link

    def cross_device_for_source(src: Any, dest: Any) -> None:
        if Path(src) == source:
            raise OSError(errno.EXDEV, "synthetic cross-device hardlink")
        original_link(src, dest)

    monkeypatch.setattr(os, "link", cross_device_for_source)
    result = adapter.publish(source, "folder/final.bin")

    assert result.success
    assert source.exists()
    assert (adapter.root / "folder/final.bin").read_bytes() == b"cross-device-content"


def test_local_publish_interruption_never_creates_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_root = tmp_path / "media"
    (media_root / "library").mkdir(parents=True)
    source = tmp_path / "source.bin"
    source.write_bytes(b"complete-content")
    adapter = LocalStorageAdapter("library", media_root=media_root)

    def interrupt_finalization(src: Path, dest: Path) -> None:
        raise OSError("synthetic interruption")

    monkeypatch.setattr(local_module, "_rename_noreplace", interrupt_finalization)
    result = adapter.publish(source, "folder/final.bin")
    assert not result.success
    assert source.exists()
    assert not (adapter.root / "folder/final.bin").exists()
    assert result.temporary_rel is not None
    assert (adapter.root / result.temporary_rel).exists()


def test_local_publish_base_exception_keeps_source_and_final_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_root = tmp_path / "media"
    (media_root / "library").mkdir(parents=True)
    source = tmp_path / "source.bin"
    source.write_bytes(b"complete-content")
    adapter = LocalStorageAdapter("library", media_root=media_root)

    def interrupt_finalization(src: Path, dest: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(local_module, "_rename_noreplace", interrupt_finalization)
    with pytest.raises(KeyboardInterrupt):
        adapter.publish(source, "folder/final.bin")

    assert source.exists()
    assert not (adapter.root / "folder/final.bin").exists()
    assert list(adapter.root.rglob("*.sluicery-tmp-*"))


def test_local_publish_race_does_not_replace_competing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_root = tmp_path / "media"
    destination = media_root / "library/final.bin"
    destination.parent.mkdir(parents=True)
    source = tmp_path / "source.bin"
    source.write_bytes(b"new-content")
    adapter = LocalStorageAdapter("library", media_root=media_root)
    original_rename = local_module._rename_noreplace

    def create_competitor_then_rename(src: Path, dest: Path) -> None:
        dest.write_bytes(b"keep-competitor")
        original_rename(src, dest)

    monkeypatch.setattr(local_module, "_rename_noreplace", create_competitor_then_rename)
    result = adapter.publish(source, "final.bin")

    assert not result.success
    assert result.reason_code == "destination_exists"
    assert destination.read_bytes() == b"keep-competitor"
    assert source.exists()
    assert result.temporary_rel is not None
    assert (adapter.root / result.temporary_rel).exists()


def test_local_publish_does_not_overwrite_existing_file(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    destination = media_root / "library/final.bin"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"keep-me")
    source = tmp_path / "source.bin"
    source.write_bytes(b"new-content")
    result = LocalStorageAdapter("library", media_root=media_root).publish(source, "final.bin")
    assert not result.success
    assert result.reason_code == "destination_exists"
    assert destination.read_bytes() == b"keep-me"
    assert source.exists()


def test_local_exists_list_move_and_free_space(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    original = media_root / "library/folder/original.bin"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"abc")
    adapter = LocalStorageAdapter("library", media_root=media_root)
    assert adapter.exists("folder/original.bin")
    assert [item.relative_path for item in adapter.list_recursive("")] == ["folder/original.bin"]
    adapter.move("folder/original.bin", "moved/final.bin")
    assert not adapter.exists("folder/original.bin")
    assert adapter.exists("moved/final.bin")
    assert adapter.free_space() is not None


def test_remote_name_is_derived_only_from_storage_id() -> None:
    assert remote_name_for_storage(42) == "st42"


def test_remote_publish_orders_temp_verify_and_moveto(tmp_path: Path) -> None:
    not_found = _result(
        returncode=1,
        classification=StorageClassification.FAILED,
        reason_code="unknown_error",
        stderr_tail="object not found",
    )
    runner = ScriptedRunner(
        [
            not_found,
            _result(),
            _result(),
            _result(stdout=[json.dumps({"Size": 3})]),
            not_found,
            _result(),
            not_found,
        ]
    )
    source = tmp_path / "source.bin"
    source.write_bytes(b"abc")
    result = _remote_adapter(runner).publish(source, "folder/final.bin")
    assert result.success
    assert source.exists()
    commands = [call[0][0] for call in runner.calls]
    assert commands == [
        "lsjson",
        "mkdir",
        "copyto",
        "lsjson",
        "lsjson",
        "moveto",
        "lsjson",
    ]
    copy_destination = runner.calls[2][0][2]
    assert ".sluicery-tmp-" in copy_destination
    assert runner.calls[5][0][2].endswith("folder/final.bin")
    assert "--ignore-existing" in runner.calls[5][0]
    assert all("test-password" not in argument for call, _env in runner.calls for argument in call)
    assert runner.obscured_inputs == ["test-password"]
    assert "RCLONE_CONFIG_ST42_PASS" in runner.calls[0][1]


def test_remote_publish_failure_leaves_only_reported_temp(tmp_path: Path) -> None:
    not_found = _result(returncode=1, stderr_tail="object not found")
    transfer_failed = _result(
        returncode=1,
        classification=StorageClassification.UNREACHABLE,
        reason_code="unreachable",
    )
    runner = ScriptedRunner([not_found, _result(), transfer_failed])
    source = tmp_path / "source.bin"
    source.write_bytes(b"abc")
    result = _remote_adapter(runner).publish(source, "folder/final.bin")
    assert not result.success
    assert result.temporary_rel is not None
    assert result.classification == StorageClassification.UNREACHABLE
    assert "moveto" not in [call[0][0] for call in runner.calls]
    assert source.exists()


def test_remote_publish_race_does_not_replace_competing_destination(tmp_path: Path) -> None:
    not_found = _result(returncode=1, stderr_tail="object not found")
    runner = ScriptedRunner(
        [
            not_found,
            _result(),
            _result(),
            _result(stdout=[json.dumps({"Size": 3})]),
            not_found,
            _result(),
            _result(),
        ]
    )
    source = tmp_path / "source.bin"
    source.write_bytes(b"abc")

    result = _remote_adapter(runner).publish(source, "folder/final.bin")

    assert not result.success
    assert result.reason_code == "destination_exists"
    assert result.temporary_rel is not None
    assert source.exists()
    assert "--ignore-existing" in runner.calls[5][0]


def test_remote_connection_has_four_successful_stages_and_cleans_up() -> None:
    runner = ConnectionRunner()
    result = _remote_adapter(runner).test_connection()
    assert result.ok
    assert [stage.stage for stage in result.stages] == list(ConnectionStage)
    assert all(stage.status == StageStatus.SUCCESS for stage in result.stages)
    assert [call[0][0] for call in runner.calls] == ["lsjson", "copyto", "cat", "deletefile"]


def test_remote_connection_reports_delete_failure() -> None:
    result = _remote_adapter(ConnectionRunner(delete_fails=True)).test_connection()
    assert not result.ok
    assert result.cleanup_warning is not None
    assert result.stages[-1].classification == StorageClassification.PERMISSION_DENIED


def test_remote_connection_reports_content_mismatch_as_failure() -> None:
    result = _remote_adapter(ConnectionRunner(corrupt_read=True)).test_connection()

    assert not result.ok
    assert result.stages[-1].classification == StorageClassification.FAILED
    assert result.stages[-1].reason_code == "content_mismatch"


def test_remote_connection_uses_one_deadline_for_all_subprocesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = iter([100.0, 101.0, 102.0, 110.0, 120.0, 129.0])
    monkeypatch.setattr(remote_module.time, "monotonic", values.__next__)
    runner = ConnectionRunner()

    result = _remote_adapter(runner, test_timeout_sec=30).test_connection()

    assert result.ok
    assert runner.obscure_timeouts == [29.0]
    assert [timeout.absolute_sec for timeout in runner.timeouts] == [28.0, 20.0, 10.0, 1.0]


def test_remote_exists_list_move_and_free_space() -> None:
    not_found = _result(returncode=1, stderr_tail="object not found")
    runner = ScriptedRunner(
        [
            _result(),
            _result(
                stdout=[
                    json.dumps(
                        [
                            {
                                "Path": "file.bin",
                                "Size": 7,
                                "ModTime": "2026-08-09T00:00:00Z",
                                "Hashes": {"md5": "abc"},
                            }
                        ]
                    )
                ]
            ),
            not_found,
            _result(),
            _result(stdout=[json.dumps({"free": 12345})]),
        ]
    )
    adapter = _remote_adapter(runner)
    assert adapter.exists("existing.bin")
    files = list(adapter.list_recursive("folder"))
    assert files[0].relative_path == "folder/file.bin"
    assert files[0].size == 7
    adapter.move("source.bin", "destination.bin")
    assert adapter.free_space() == 12345


def test_remote_recursive_list_uses_integrity_deadline() -> None:
    runner = ScriptedRunner([_result(stdout=["[]"])])
    adapter = _remote_adapter(runner)

    assert list(adapter.list_recursive("", timeout_sec=7)) == []

    timeout = runner.timeouts[-1]
    assert timeout.idle_sec == 7
    assert timeout.absolute_sec == 7
    assert timeout.term_grace_sec == 7


def test_remote_connection_distinguishes_auth_failure() -> None:
    runner = ScriptedRunner(
        [
            _result(
                returncode=1,
                classification=StorageClassification.AUTH_FAILED,
                reason_code="authentication_failed",
            )
        ]
    )
    result = _remote_adapter(runner).test_connection()
    assert result.stages[0].status == StageStatus.SUCCESS
    assert result.stages[1].status == StageStatus.FAILED
    assert result.stages[1].classification == StorageClassification.AUTH_FAILED
    assert result.stages[2].status == StageStatus.SKIPPED
    assert result.stages[3].status == StageStatus.SKIPPED


def test_capacity_none_skips_blocking() -> None:
    result = evaluate_capacity(None, warn_bytes=100, stop_bytes=20)
    assert result.state == CapacityState.UNKNOWN
    assert not result.should_block


def test_mount_factory_is_explicitly_unimplemented() -> None:
    storage = Storage(
        id=1,
        name="future-mount",
        kind=StorageKind.MOUNT,
        enabled=True,
        config_json={},
    )
    with pytest.raises(MountStorageNotImplementedError, match="Phase 19"):
        create_storage_adapter(storage, None)  # type: ignore[arg-type]
