from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sluicery import cli
from sluicery.config import load_settings
from sluicery.downloader.errors import Classification
from sluicery.downloader.ytdlp import RunResult, TimeoutPolicy, YtdlpRunner


def _patch_runtime(monkeypatch, session_factory, base_env, captured: dict[str, Any]) -> None:
    settings = load_settings()
    monkeypatch.setattr(cli, "_open_session", lambda: session_factory())
    monkeypatch.setattr(
        cli,
        "_ytdlp_timeout_and_bin",
        lambda: (
            settings,
            Path("/fake/yt-dlp"),
            TimeoutPolicy(idle_sec=1, absolute_sec=2, term_grace_sec=1),
            64,
        ),
    )

    def fake_run(
        self: YtdlpRunner,
        args: list[str],
        *,
        timeout: TimeoutPolicy,
        on_progress=None,
        cwd=None,
    ) -> RunResult:
        captured["args"] = args
        captured["timeout"] = timeout
        if "--flat-playlist" in args:
            stdout = [json.dumps({"id": "entry", "title": "Entry", "formats": []})]
        else:
            stdout = ["/data/staging/work/entry.mkv"]
        return RunResult(returncode=0, classification=Classification.OK, stdout_lines=stdout)

    monkeypatch.setattr(YtdlpRunner, "run", fake_run)


def test_probe_uses_discover_builder_and_discover_timeout(
    monkeypatch,
    session_factory,
    base_env,
) -> None:
    captured: dict[str, Any] = {}
    _patch_runtime(monkeypatch, session_factory, base_env, captured)

    assert cli._cmd_ytdlp_probe("https://example.com/list", "--limit-rate 1M") == 0
    args = captured["args"]
    assert "--flat-playlist" in args
    assert "--simulate" in args
    assert "--print" in args
    assert "--output" not in args
    assert args[-2] == "--"
    assert args[-1] == "https://example.com/list"
    assert captured["timeout"].idle_sec == 300
    assert captured["timeout"].absolute_sec == 300


def test_fetch_uses_download_builder_and_keeps_progress_with_print(
    monkeypatch,
    session_factory,
    base_env,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}
    _patch_runtime(monkeypatch, session_factory, base_env, captured)

    assert (
        cli._cmd_ytdlp_fetch(
            "https://example.com/item",
            str(tmp_path),
            None,
            None,
            "video",
            None,
        )
        == 0
    )
    args = captured["args"]
    assert "--output" in args
    assert "--paths" in args
    assert "--progress-template" in args
    assert "--print" in args
    assert "--progress" in args
    assert "--download-archive" not in args
    assert args[-2] == "--"
    assert args[-1] == "https://example.com/item"
    assert captured["timeout"].idle_sec == 300
    assert captured["timeout"].absolute_sec == 21600


def test_raw_exec_rejects_exec_option_before_runtime_setup(capsys) -> None:
    assert cli._cmd_ytdlp_exec(["--exec", "echo bad"]) == 1
    assert "ytdlp exec" in capsys.readouterr().err


def test_fetch_and_raw_exec_mask_credentials(
    monkeypatch,
    session_factory,
    base_env,
    tmp_path: Path,
    capsys,
) -> None:
    captured: dict[str, Any] = {}
    _patch_runtime(monkeypatch, session_factory, base_env, captured)

    assert (
        cli._cmd_ytdlp_fetch(
            "https://user:password@example.com/item?token=url-secret",
            str(tmp_path),
            None,
            None,
            "video",
            "-poption-secret",
        )
        == 0
    )
    fetch_output = capsys.readouterr().out
    for secret in ("user", "password", "url-secret", "option-secret"):
        assert secret not in fetch_output
    assert "********" in fetch_output

    assert cli._cmd_ytdlp_exec(["-praw-secret", "https://example.com/item"]) == 0
    raw_output = capsys.readouterr().out
    assert "raw-secret" not in raw_output
    assert "-p********" in raw_output
