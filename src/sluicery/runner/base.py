"""外部プロセス実行の共通基盤（要件 N-8/N-10、Phase 5 §2）。"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit, urlunsplit
from uuid import uuid4

_SENSITIVE_VALUE_FLAGS = frozenset(
    {
        "-u",
        "--username",
        "-p",
        "--password",
        "-2",
        "--twofactor",
        "--video-password",
        "--ap-username",
        "--ap-password",
        "--cookies",
        "--cookies-from-browser",
        "--proxy",
        "--add-header",
        "--add-headers",
        "--client-certificate",
        "--client-certificate-key",
        "--client-certificate-password",
        "--netrc-location",
    }
)
_SENSITIVE_INLINE_PREFIXES = tuple(
    f"{flag}=" for flag in _SENSITIVE_VALUE_FLAGS if flag.startswith("--")
)
_SENSITIVE_SHORT_PREFIXES = ("-u", "-p", "-2")
_SENSITIVE_QUERY_FRAGMENTS = (
    "api_key",
    "apikey",
    "auth",
    "credential",
    "key",
    "pass",
    "policy",
    "secret",
    "sig",
    "token",
    "user",
)
_RCLONE_ENV_NAME = re.compile(r"RCLONE_CONFIG_[A-Z0-9_]+")


def _mask_url_parameters(value: str) -> str:
    parts: list[str] = []
    for component in value.split("&") if value else []:
        key, separator, _parameter_value = component.partition("=")
        normalized = unquote_plus(key).lower().replace("-", "_")
        if separator and any(fragment in normalized for fragment in _SENSITIVE_QUERY_FRAGMENTS):
            parts.append(f"{key}=********")
        else:
            parts.append(component)
    return "&".join(parts)


def _mask_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return value
        netloc = parsed.netloc
        if parsed.username is not None:
            netloc = f"********@{netloc.rsplit('@', 1)[1]}"
    except ValueError:
        return value

    return urlunsplit(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            _mask_url_parameters(parsed.query),
            _mask_url_parameters(parsed.fragment),
        )
    )


def mask_command_line(args: Sequence[str]) -> list[str]:
    """CLI 引数を表示する前に、認証値と URL 内の秘密を伏せる。"""
    masked: list[str] = []
    redact_next = False
    for arg in args:
        if redact_next:
            masked.append("********")
            redact_next = False
            continue
        if arg in _SENSITIVE_VALUE_FLAGS:
            masked.append(arg)
            redact_next = True
            continue
        inline_prefix = next((p for p in _SENSITIVE_INLINE_PREFIXES if arg.startswith(p)), None)
        if inline_prefix is not None:
            masked.append(f"{inline_prefix}********")
            continue
        short_prefix = next(
            (
                prefix
                for prefix in _SENSITIVE_SHORT_PREFIXES
                if arg.startswith(prefix) and len(arg) > len(prefix) and not arg.startswith("--")
            ),
            None,
        )
        if short_prefix is not None:
            masked.append(f"{short_prefix}********")
            continue
        masked.append(_mask_url(arg))
    return masked


def mask_output_text(
    text: str,
    *,
    sensitive_values: Sequence[str] = (),
    mask_rclone_env_names: bool = False,
) -> str:
    """子プロセス出力に含まれた秘密を、保持・ファイル出力より前に伏せる。"""
    masked = text
    for value in sorted({value for value in sensitive_values if value}, key=len, reverse=True):
        masked = masked.replace(value, "********")
    if mask_rclone_env_names:
        masked = _RCLONE_ENV_NAME.sub("********", masked)
    return masked


@dataclass(frozen=True)
class TimeoutPolicy:
    idle_sec: int | None
    absolute_sec: int | None
    term_grace_sec: int


@dataclass(frozen=True)
class ProcessRunResult:
    returncode: int
    stderr_text: str
    stderr_tail: str
    log_path: Path
    duration_sec: float
    terminated_by: str | None


def _build_env(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """必要な値だけ選び、親プロセスの環境を一括継承しない。"""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
    }
    if overrides:
        env.update(overrides)
    return env


def _terminate_process_group(proc: subprocess.Popen[str], grace_sec: float) -> None:
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace_sec)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def _tail(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[-max_bytes:].decode("utf-8", errors="ignore")


class _Activity:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = time.monotonic()

    def touch(self) -> None:
        with self._lock:
            self._last = time.monotonic()

    @property
    def last(self) -> float:
        with self._lock:
            return self._last


class BaseRunner:
    """stdout/stderr、タイムアウト、キャンセルを共通管理する Runner 基底。"""

    def __init__(
        self,
        executable: Path,
        *,
        runner_name: str,
        log_prefix: str,
        stderr_tail_kb: int = 64,
        log_dir: Path | None = None,
    ) -> None:
        self._executable = executable
        self._runner_name = runner_name
        self._log_prefix = log_prefix
        self._stderr_tail_kb = stderr_tail_kb
        self._log_dir = log_dir or Path(tempfile.gettempdir())
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._cancel_requested = False

    def cancel(self) -> None:
        with self._lock:
            self._cancel_requested = True

    def _run_process(
        self,
        args: Sequence[str],
        *,
        timeout: TimeoutPolicy,
        on_stdout_line: Callable[[str], None] | None = None,
        on_stderr_line: Callable[[str], None] | None = None,
        cwd: Path | None = None,
        env_overrides: Mapping[str, str] | None = None,
        stdin_text: str | None = None,
        sensitive_values: Sequence[str] = (),
        mask_rclone_env_names: bool = False,
    ) -> ProcessRunResult:
        with self._lock:
            if self._proc is not None:
                raise RuntimeError(f"{self._runner_name} は同時に1プロセスしか実行できません")
            self._cancel_requested = False

        full_args = [str(self._executable), *args]
        self._log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._log_dir / f"{self._log_prefix}-{uuid4().hex}.log"
        start = time.monotonic()
        proc = subprocess.Popen(  # noqa: S603 - shell=False、引数はリスト固定
            full_args,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=_build_env(env_overrides),
            cwd=str(cwd) if cwd is not None else None,
        )
        with self._lock:
            self._proc = proc

        if stdin_text is not None:
            assert proc.stdin is not None
            try:
                proc.stdin.write(stdin_text)
                proc.stdin.flush()
            except BrokenPipeError:
                pass
            finally:
                proc.stdin.close()

        activity = _Activity()
        stderr_chunks: list[str] = []
        stderr_lock = threading.Lock()

        def sanitize(line: str) -> str:
            return mask_output_text(
                line,
                sensitive_values=sensitive_values,
                mask_rclone_env_names=mask_rclone_env_names,
            )

        def read_stdout() -> None:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                activity.touch()
                if on_stdout_line is not None:
                    on_stdout_line(sanitize(raw_line.rstrip("\n")))

        def read_stderr() -> None:
            assert proc.stderr is not None
            with log_path.open("w", encoding="utf-8") as log_file:
                for raw_line in proc.stderr:
                    activity.touch()
                    line = sanitize(raw_line)
                    with stderr_lock:
                        stderr_chunks.append(line)
                    if on_stderr_line is not None:
                        on_stderr_line(line.rstrip("\n"))
                    log_file.write(line)
                    log_file.flush()

        stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        terminated_by: str | None = None
        try:
            while True:
                try:
                    proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    pass

                with self._lock:
                    cancelled = self._cancel_requested
                if cancelled:
                    terminated_by = "cancel"
                    _terminate_process_group(proc, timeout.term_grace_sec)
                    break

                now = time.monotonic()
                if timeout.absolute_sec is not None and now - start >= timeout.absolute_sec:
                    terminated_by = "absolute"
                    _terminate_process_group(proc, timeout.term_grace_sec)
                    break
                if timeout.idle_sec is not None and now - activity.last >= timeout.idle_sec:
                    terminated_by = "idle"
                    _terminate_process_group(proc, timeout.term_grace_sec)
                    break
        except BaseException:
            # CLI 自体への SIGINT 等でも、別 session の子だけを残して終了しない。
            if proc.poll() is None:
                _terminate_process_group(proc, timeout.term_grace_sec)
            raise
        finally:
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
            with self._lock:
                self._proc = None

        with stderr_lock:
            stderr_text = "".join(stderr_chunks)
        return ProcessRunResult(
            returncode=proc.returncode,
            stderr_text=stderr_text,
            stderr_tail=_tail(stderr_text, self._stderr_tail_kb * 1024),
            log_path=log_path,
            duration_sec=time.monotonic() - start,
            terminated_by=terminated_by,
        )


__all__ = [
    "BaseRunner",
    "ProcessRunResult",
    "TimeoutPolicy",
    "mask_command_line",
    "mask_output_text",
]
