"""rclone 固有の JSON ログ解釈・分類・クレデンシャル注入。"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sluicery.runner.base import BaseRunner, TimeoutPolicy
from sluicery.storage.errors import StorageClassification, classify
from sluicery.storage.progress import TransferProgressEvent, parse_rclone_log_line

_RCLONE_CONFIG_NAME = re.compile(r"RCLONE_CONFIG_[A-Z0-9_]+\Z")
_SENSITIVE_CONFIG_SUFFIXES = (
    "_PASS",
    "_PASSWORD",
    "_USER",
    "_USERNAME",
    "_DOMAIN",
    "_TOKEN",
    "_SECRET",
    "_KEY",
)


class RcloneConfigurationError(ValueError):
    pass


class RcloneObscureError(RuntimeError):
    pass


@dataclass
class RcloneRunResult:
    returncode: int
    classification: StorageClassification
    reason_code: str
    stdout_lines: list[str] = field(default_factory=list)
    progress_events: list[TransferProgressEvent] = field(default_factory=list)
    stderr_tail: str = ""
    log_path: Path | None = None
    duration_sec: float = 0.0
    terminated_by: str | None = None


def _validate_config_env(config_env: Mapping[str, str]) -> None:
    invalid = [name for name in config_env if _RCLONE_CONFIG_NAME.fullmatch(name) is None]
    if invalid:
        raise RcloneConfigurationError("rclone 設定には RCLONE_CONFIG_* 形式だけを指定できます")


def _sensitive_config_values(config_env: Mapping[str, str]) -> list[str]:
    return [
        value
        for name, value in config_env.items()
        if value and name.endswith(_SENSITIVE_CONFIG_SUFFIXES)
    ]


class RcloneRunner(BaseRunner):
    def __init__(
        self,
        rclone_bin: Path = Path("/usr/local/bin/rclone"),
        *,
        stderr_tail_kb: int = 64,
        log_dir: Path | None = None,
    ) -> None:
        super().__init__(
            rclone_bin,
            runner_name="RcloneRunner",
            log_prefix="rclone",
            stderr_tail_kb=stderr_tail_kb,
            log_dir=log_dir,
        )

    def obscure_password(self, password: str, *, timeout_sec: int = 30) -> str:
        """平文は stdin だけに流し、rclone の obscure 値を返す。"""
        if "\n" in password or "\r" in password:
            raise RcloneConfigurationError("パスワードに改行は使用できません")
        output: list[str] = []
        result = self._run_process(
            ["obscure", "-", "--config", os.devnull, "--ask-password=false"],
            timeout=TimeoutPolicy(idle_sec=timeout_sec, absolute_sec=timeout_sec, term_grace_sec=2),
            on_stdout_line=output.append,
            stdin_text=f"{password}\n",
            sensitive_values=[password],
            mask_rclone_env_names=True,
        )
        obscured = [line for line in output if line]
        if result.returncode != 0 or len(obscured) != 1:
            raise RcloneObscureError("rclone obscure に失敗しました")
        return obscured[0]

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: TimeoutPolicy,
        config_env: Mapping[str, str] | None = None,
        retries: int = 1,
        stats_interval: str | None = None,
        on_progress: Callable[[TransferProgressEvent], None] | None = None,
        cwd: Path | None = None,
    ) -> RcloneRunResult:
        env = dict(config_env or {})
        _validate_config_env(env)
        for name, value in env.items():
            if name in args or value in args:
                raise RcloneConfigurationError(
                    "rclone の設定名・クレデンシャルをコマンドラインへ指定できません"
                )

        stdout_lines: list[str] = []
        progress_events: list[TransferProgressEvent] = []

        def read_stderr(line: str) -> None:
            event = parse_rclone_log_line(line)
            if event is not None:
                progress_events.append(event)
                if on_progress is not None:
                    on_progress(event)

        managed_args = [
            *args,
            "--use-json-log",
            "--log-level",
            "INFO",
            "--retries",
            str(retries),
            "--low-level-retries",
            "1",
            "--config",
            os.devnull,
            "--ask-password=false",
        ]
        if stats_interval is not None:
            managed_args.extend(["--stats", stats_interval])

        result = self._run_process(
            managed_args,
            timeout=timeout,
            on_stdout_line=stdout_lines.append,
            on_stderr_line=read_stderr,
            cwd=cwd,
            env_overrides=env,
            sensitive_values=_sensitive_config_values(env),
            mask_rclone_env_names=True,
        )
        if result.terminated_by in {"idle", "absolute"}:
            classification_name = StorageClassification.UNREACHABLE
            reason_code = "timeout"
        elif result.terminated_by == "cancel":
            classification_name = StorageClassification.FAILED
            reason_code = "cancelled"
        else:
            classification = classify(result.returncode, result.stderr_text)
            classification_name = classification.classification
            reason_code = classification.reason_code
        return RcloneRunResult(
            returncode=result.returncode,
            classification=classification_name,
            reason_code=reason_code,
            stdout_lines=stdout_lines,
            progress_events=progress_events,
            stderr_tail=result.stderr_tail,
            log_path=result.log_path,
            duration_sec=result.duration_sec,
            terminated_by=result.terminated_by,
        )


__all__ = [
    "RcloneConfigurationError",
    "RcloneObscureError",
    "RcloneRunResult",
    "RcloneRunner",
]
