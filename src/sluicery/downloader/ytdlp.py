"""yt-dlp CLI ラッパ（要件定義 §5.2, docs/phase3_指示書.md §3）。

- Python API は使わない。CLI を subprocess で実行するだけ
- `shell=True` は使わない。引数は常にリストで渡す（要件 N-10）
- 子プロセスは新しいプロセスグループで起動し、群単位で終了させる（§3.5）。
  yt-dlp は ffmpeg を子プロセスとして起動するため、親だけ kill すると
  ffmpeg が孤児として残り Staging に不完全ファイルを書き続ける
- ロケールは `LC_ALL=C` に固定する（§5 のエラー分類が英語メッセージの
  パターンに依存しているため）
- 予約引数の注入・レイヤー合成は Phase 4（`core/options.py`）の責務。
  ここでは呼び出し側が渡した引数リストをそのまま実行するだけ（§1.3）
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit, urlunsplit
from uuid import uuid4

from sluicery.downloader.errors import Classification, classify
from sluicery.downloader.progress import ProgressEvent, parse_progress_line
from sluicery.downloader.protocol import PRINT_PREFIX, PROGRESS_PREFIX

# コマンドラインをログに出す前に必ず通すマスク層（§3.6）。
# Cookie ファイルパス・パスワード等、値そのものが機密になりうる引数の
# 直後の値を伏せる。呼び出し側での付け忘れが起きないよう、この層に集約する。
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
    """URL の userinfo と認証用途の query 値を伏せる。"""
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


@dataclass
class TimeoutPolicy:
    idle_sec: int | None
    absolute_sec: int | None
    term_grace_sec: int


@dataclass
class RunResult:
    returncode: int
    classification: Classification
    stdout_lines: list[str] = field(default_factory=list)
    progress_events: list[ProgressEvent] = field(default_factory=list)
    stderr_tail: str = ""
    log_path: Path | None = None
    duration_sec: float = 0.0
    terminated_by: str | None = None


def _build_env() -> dict[str, str]:
    """環境変数は明示的に構築する。親プロセスの環境をそのまま継承しない（§3.2）。"""
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LC_ALL": "C",
    }


def _terminate_process_group(proc: subprocess.Popen[str], grace_sec: float) -> None:
    """プロセスグループ単位で終了させる（§3.5）。"""
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
    """アイドルタイムアウト判定用。最後に出力を受け取った時刻を保持する。"""

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


class YtdlpRunner:
    """1インスタンスにつき同時に1プロセスのみ実行する。"""

    def __init__(
        self,
        ytdlp_bin: Path,
        *,
        stderr_tail_kb: int = 64,
        log_dir: Path | None = None,
    ) -> None:
        self._ytdlp_bin = ytdlp_bin
        self._stderr_tail_kb = stderr_tail_kb
        self._log_dir = log_dir or Path(tempfile.gettempdir())
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._cancel_requested = False

    def cancel(self) -> None:
        """実行中のプロセスにキャンセル要求を出す。実際の終了は `run()` のループが行う。"""
        with self._lock:
            self._cancel_requested = True

    def run(
        self,
        args: list[str],
        *,
        timeout: TimeoutPolicy,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        cwd: Path | None = None,
    ) -> RunResult:
        with self._lock:
            if self._proc is not None:
                raise RuntimeError("YtdlpRunner は同時に1プロセスしか実行できません")
            self._cancel_requested = False

        full_args = [str(self._ytdlp_bin), *args]
        self._log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._log_dir / f"ytdlp-{uuid4().hex}.log"

        start = time.monotonic()
        proc = subprocess.Popen(  # noqa: S603 - shell=True は使わない。引数はリスト
            full_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=_build_env(),
            cwd=str(cwd) if cwd is not None else None,
        )
        with self._lock:
            self._proc = proc

        activity = _Activity()
        stdout_lines: list[str] = []
        progress_events: list[ProgressEvent] = []
        stderr_chunks: list[str] = []
        stderr_lock = threading.Lock()

        def read_stdout() -> None:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                activity.touch()
                line = raw_line.rstrip("\n")
                if line.startswith(PROGRESS_PREFIX):
                    event = parse_progress_line(line[len(PROGRESS_PREFIX) :])
                    if event is not None:
                        progress_events.append(event)
                        if on_progress is not None:
                            on_progress(event)
                    # 解釈できない行は破棄する（§4.3）。
                elif line.startswith(PRINT_PREFIX):
                    stdout_lines.append(line[len(PRINT_PREFIX) :])
                # プレフィックスの無い行は無視する（§3.3、プロトコル境界を守る）。

        def read_stderr() -> None:
            assert proc.stderr is not None
            with log_path.open("w", encoding="utf-8") as log_file:
                for raw_line in proc.stderr:
                    activity.touch()
                    with stderr_lock:
                        stderr_chunks.append(raw_line)
                    log_file.write(raw_line)
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
        finally:
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
            with self._lock:
                self._proc = None

        with stderr_lock:
            stderr_full = "".join(stderr_chunks)
        stderr_tail = _tail(stderr_full, self._stderr_tail_kb * 1024)
        classification = classify(proc.returncode, stderr_full).classification
        duration = time.monotonic() - start

        return RunResult(
            returncode=proc.returncode,
            classification=classification,
            stdout_lines=stdout_lines,
            progress_events=progress_events,
            stderr_tail=stderr_tail,
            log_path=log_path,
            duration_sec=duration,
            terminated_by=terminated_by,
        )


__all__ = ["RunResult", "TimeoutPolicy", "YtdlpRunner", "mask_command_line"]
