"""yt-dlp 固有の出力解釈とエラー分類を持つ CLI Runner。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sluicery.downloader.errors import Classification, classify
from sluicery.downloader.progress import ProgressEvent, parse_progress_line
from sluicery.downloader.protocol import PRINT_PREFIX, PROGRESS_PREFIX
from sluicery.runner.base import BaseRunner, TimeoutPolicy, mask_command_line


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


class YtdlpRunner(BaseRunner):
    """共通 Runner 上で yt-dlp のフレーミングだけを解釈する。"""

    def __init__(
        self,
        ytdlp_bin: Path,
        *,
        stderr_tail_kb: int = 64,
        log_dir: Path | None = None,
    ) -> None:
        super().__init__(
            ytdlp_bin,
            runner_name="YtdlpRunner",
            log_prefix="ytdlp",
            stderr_tail_kb=stderr_tail_kb,
            log_dir=log_dir,
        )

    def run(
        self,
        args: list[str],
        *,
        timeout: TimeoutPolicy,
        on_progress: Callable[[ProgressEvent], None] | None = None,
        cwd: Path | None = None,
    ) -> RunResult:
        stdout_lines: list[str] = []
        progress_events: list[ProgressEvent] = []

        def read_stdout(line: str) -> None:
            if line.startswith(PROGRESS_PREFIX):
                event = parse_progress_line(line[len(PROGRESS_PREFIX) :])
                if event is not None:
                    progress_events.append(event)
                    if on_progress is not None:
                        on_progress(event)
            elif line.startswith(PRINT_PREFIX):
                stdout_lines.append(line[len(PRINT_PREFIX) :])

        process_result = self._run_process(
            args,
            timeout=timeout,
            on_stdout_line=read_stdout,
            cwd=cwd,
            env_overrides={"LC_ALL": "C"},
        )
        classification = classify(
            process_result.returncode, process_result.stderr_text
        ).classification
        return RunResult(
            returncode=process_result.returncode,
            classification=classification,
            stdout_lines=stdout_lines,
            progress_events=progress_events,
            stderr_tail=process_result.stderr_tail,
            log_path=process_result.log_path,
            duration_sec=process_result.duration_sec,
            terminated_by=process_result.terminated_by,
        )


__all__ = ["RunResult", "TimeoutPolicy", "YtdlpRunner", "mask_command_line"]
