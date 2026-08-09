from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from sluicery.runner.base import TimeoutPolicy
from sluicery.storage.errors import StorageClassification
from sluicery.storage.progress import TransferProgressEvent
from sluicery.storage.rclone import RcloneConfigurationError, RcloneRunner, RcloneRunResult

FAKE_RCLONE = Path(__file__).parent / "fixtures" / "fake_rclone.py"


def _runner(tmp_path: Path) -> RcloneRunner:
    return RcloneRunner(FAKE_RCLONE, stderr_tail_kb=1, log_dir=tmp_path)


def _timeout(idle: int = 5, absolute: int = 10) -> TimeoutPolicy:
    return TimeoutPolicy(idle_sec=idle, absolute_sec=absolute, term_grace_sec=1)


def _process_still_running(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[-1].split()[0] not in ("Z", "X")


def test_rclone_runner_parses_json_progress_and_stdout(tmp_path: Path) -> None:
    seen: list[TransferProgressEvent] = []
    result = _runner(tmp_path).run(
        ["json_then_exit"],
        timeout=_timeout(),
        stats_interval="1s",
        on_progress=seen.append,
    )
    assert result.returncode == 0
    assert result.classification == StorageClassification.OK
    assert len(result.progress_events) == 1
    assert result.progress_events[0].bytes_transferred == 12
    assert seen == result.progress_events
    assert json.loads("".join(result.stdout_lines))[0]["Path"] == "file.bin"


def test_rclone_runner_classifies_auth_failure(tmp_path: Path) -> None:
    result = _runner(tmp_path).run(["fail_auth"], timeout=_timeout())
    assert result.classification == StorageClassification.AUTH_FAILED


def test_config_is_only_in_child_env_and_output_is_masked(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    secret = "synthetic-rclone-secret"
    config_env = {
        "RCLONE_CONFIG_ST42_TYPE": "smb",
        "RCLONE_CONFIG_ST42_USER": "synthetic-user",
        "RCLONE_CONFIG_ST42_PASS": secret,
    }
    result = _runner(tmp_path).run(
        ["inspect_env", str(report)], timeout=_timeout(), config_env=config_env
    )
    payload = json.loads(report.read_text())
    assert payload["config_count"] == 3
    assert payload["secret_in_argv"] is False
    assert secret not in payload["argv"]
    assert secret not in result.stderr_tail
    assert "RCLONE_CONFIG_ST42" not in result.stderr_tail
    assert result.log_path is not None
    log_text = result.log_path.read_text()
    assert secret not in log_text
    assert "RCLONE_CONFIG_ST42" not in log_text


def test_config_value_on_command_line_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RcloneConfigurationError):
        _runner(tmp_path).run(
            ["noop", "synthetic-rclone-secret"],
            timeout=_timeout(),
            config_env={"RCLONE_CONFIG_ST42_PASS": "synthetic-rclone-secret"},
        )


def test_obscure_password_uses_stdin(tmp_path: Path) -> None:
    assert (
        _runner(tmp_path).obscure_password("synthetic-plain-password")
        == "obscured-from-stdin"
    )


def test_rclone_runner_terminates_entire_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    result = _runner(tmp_path).run(
        ["spawn_child_and_sleep", str(pid_file)], timeout=_timeout(idle=1, absolute=30)
    )
    assert result.terminated_by == "idle"
    assert result.classification == StorageClassification.UNREACHABLE
    assert result.reason_code == "timeout"
    child_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and _process_still_running(child_pid):
        time.sleep(0.05)
    assert not _process_still_running(child_pid)


def test_rclone_runner_classifies_cancellation_separately(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    result_holder: dict[str, RcloneRunResult] = {}

    def run() -> None:
        result_holder["result"] = runner.run(
            ["spawn_child_and_sleep", str(tmp_path / "cancelled-child.pid")],
            timeout=_timeout(idle=30, absolute=30),
        )

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 3
    while not (tmp_path / "cancelled-child.pid").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    runner.cancel()
    thread.join(timeout=5)

    result = result_holder["result"]
    assert result.terminated_by == "cancel"
    assert result.classification == StorageClassification.FAILED
    assert result.reason_code == "cancelled"


def test_caller_interruption_terminates_process_group(tmp_path: Path) -> None:
    class InterruptingTimeout:
        idle_sec = None
        term_grace_sec = 1

        @property
        def absolute_sec(self) -> int:
            raise KeyboardInterrupt

    pid_file = tmp_path / "interrupted-child.pid"
    with pytest.raises(KeyboardInterrupt):
        _runner(tmp_path).run(
            ["spawn_child_and_sleep", str(pid_file)],
            timeout=InterruptingTimeout(),  # type: ignore[arg-type]
        )
    child_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and _process_still_running(child_pid):
        time.sleep(0.05)
    assert not _process_still_running(child_pid)
