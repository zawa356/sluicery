from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from sluicery.downloader.errors import Classification
from sluicery.downloader.ytdlp import TimeoutPolicy, YtdlpRunner, mask_command_line

FAKE_YTDLP = Path(__file__).parent / "fixtures" / "fake_ytdlp.py"


def _runner(tmp_path: Path) -> YtdlpRunner:
    return YtdlpRunner(FAKE_YTDLP, stderr_tail_kb=1, log_dir=tmp_path)


def test_run_captures_progress_and_print_lines(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    seen = []
    result = runner.run(
        ["progress_then_exit"],
        timeout=TimeoutPolicy(idle_sec=5, absolute_sec=10, term_grace_sec=2),
        on_progress=seen.append,
    )
    assert result.returncode == 0
    assert result.classification == Classification.OK
    assert result.terminated_by is None
    assert len(result.progress_events) == 3
    assert [e.downloaded_bytes for e in result.progress_events] == [0, 1, 2]
    assert result.stdout_lines == ["done"]
    assert result.result_metadata == [{"file_path": "done", "format_id": "137+140"}]
    assert seen == result.progress_events
    assert result.log_path is not None and result.log_path.exists()


def test_ytdlp_runner_preserves_inherited_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_popen = subprocess.Popen
    seen_stdin: list[object] = []

    def capture_popen(*args: object, **kwargs: object) -> subprocess.Popen[str]:
        seen_stdin.append(kwargs.get("stdin"))
        return original_popen(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "Popen", capture_popen)
    result = _runner(tmp_path).run(
        ["noop"], timeout=TimeoutPolicy(idle_sec=5, absolute_sec=10, term_grace_sec=2)
    )

    assert result.returncode == 0
    assert seen_stdin == [None]


def test_run_classifies_via_stderr_pattern(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    result = runner.run(
        ["fail_429"], timeout=TimeoutPolicy(idle_sec=5, absolute_sec=10, term_grace_sec=2)
    )
    assert result.returncode != 0
    assert result.classification == Classification.BLOCKED
    assert "429" in result.stderr_tail


def test_run_classifies_unknown_error_as_failed(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    result = runner.run(
        ["fail_unknown"], timeout=TimeoutPolicy(idle_sec=5, absolute_sec=10, term_grace_sec=2)
    )
    assert result.classification == Classification.FAILED


def test_idle_timeout_terminates_process(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    result = runner.run(
        ["sleep_forever"], timeout=TimeoutPolicy(idle_sec=1, absolute_sec=30, term_grace_sec=1)
    )
    assert result.terminated_by == "idle"
    assert result.returncode != 0


def test_absolute_timeout_terminates_process_despite_activity(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    result = runner.run(
        ["periodic_progress"],
        timeout=TimeoutPolicy(idle_sec=30, absolute_sec=1, term_grace_sec=1),
    )
    assert result.terminated_by == "absolute"
    assert len(result.progress_events) > 0


def _process_still_running(pid: int) -> bool:
    """PID が存在し、かつ zombie でない（=実際に生きて動いている）かを見る。

    テストコンテナには init プロセスが無く（`docker run --init` 等を付けない
    運用のため）、孫プロセス（このテストでの fork 子）を SIGTERM で殺しても
    元の親から reap されるまで zombie として PID テーブルに残る。そのため
    `os.kill(pid, 0)` が例外を投げるかどうかでは判定できない（zombie にも
    成功してしまう）。`/proc/<pid>/stat` の状態文字で判定する。
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    state = stat.rsplit(")", 1)[-1].split()[0]
    return state not in ("Z", "X")


def test_terminates_entire_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    runner = _runner(tmp_path)
    result = runner.run(
        ["spawn_child_and_sleep", str(pid_file)],
        timeout=TimeoutPolicy(idle_sec=1, absolute_sec=30, term_grace_sec=1),
    )
    assert result.terminated_by == "idle"

    deadline = time.monotonic() + 5
    child_pid = None
    while time.monotonic() < deadline:
        if pid_file.exists() and pid_file.read_text().strip():
            child_pid = int(pid_file.read_text().strip())
            break
        time.sleep(0.05)
    assert child_pid is not None, "子プロセスが pid ファイルを書く前にタイムアウトした"

    # SIGTERM/SIGKILL が届いて実際に停止していることを確認する（プロセス
    # テーブルからの完全消滅は、テスト実行コンテナに init が無く zombie が
    # reap されないため保証できない。§3.5 の要求は「孤児として動き続けない」
    # ことであり、ここではそれを検証する）。
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and _process_still_running(child_pid):
        time.sleep(0.05)
    assert not _process_still_running(child_pid)


def test_cancel_terminates_running_process(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    holder: dict[str, object] = {}

    def _run() -> None:
        holder["result"] = runner.run(
            ["sleep_forever"], timeout=TimeoutPolicy(idle_sec=30, absolute_sec=30, term_grace_sec=1)
        )

    thread = threading.Thread(target=_run)
    thread.start()
    time.sleep(0.3)
    runner.cancel()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert holder["result"].terminated_by == "cancel"  # type: ignore[union-attr]


def test_mask_command_line_redacts_sensitive_values() -> None:
    args = ["URL", "--password", "hunter2", "--cookies", "/tmp/cookies.txt", "--username=foo"]
    masked = mask_command_line(args)
    assert "hunter2" not in masked
    assert "/tmp/cookies.txt" not in masked
    assert masked[0] == "URL"
    assert masked[1] == "--password"
    assert masked[2] == "********"


def test_mask_command_line_redacts_inline_flag_value() -> None:
    masked = mask_command_line(["--password=hunter2"])
    assert masked == ["--password=********"]


@pytest.mark.parametrize(
    ("args", "secret"),
    [
        (["-p", "short-secret"], "short-secret"),
        (["-pjoined-secret"], "joined-secret"),
        (["--cookies-from-browser=firefox:profile"], "firefox:profile"),
        (["--add-headers", "Authorization: Bearer header-secret"], "header-secret"),
        (["--proxy", "https://proxy-user:proxy-secret@example.com"], "proxy-secret"),
        (["https://url-user:url-secret@example.com/watch?v=1"], "url-secret"),
        (["https://example.com/watch?v=1&token=query-secret"], "query-secret"),
        (["https://example.com/#access_token=fragment-secret"], "fragment-secret"),
    ],
)
def test_mask_command_line_covers_auth_cookie_proxy_and_urls(
    args: list[str], secret: str
) -> None:
    rendered = " ".join(mask_command_line(args))
    assert secret not in rendered
    assert "********" in rendered
