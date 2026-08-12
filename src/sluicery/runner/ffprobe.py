"""BaseRunner上のffprobe JSONラッパ。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sluicery.runner.base import BaseRunner, TimeoutPolicy


@dataclass(frozen=True)
class ProbeResult:
    returncode: int
    metadata: dict[str, Any] | None
    stderr_tail: str
    terminated_by: str | None


class FFprobeRunner(BaseRunner):
    def __init__(self, executable: Path = Path("/usr/local/bin/ffprobe")) -> None:
        super().__init__(executable, runner_name="FFprobeRunner", log_prefix="ffprobe")

    def probe(self, file_path: Path, *, timeout_sec: int) -> ProbeResult:
        stdout: list[str] = []
        result = self._run_process(
            [
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                "--",
                str(file_path),
            ],
            timeout=TimeoutPolicy(
                idle_sec=timeout_sec,
                absolute_sec=timeout_sec,
                term_grace_sec=5,
            ),
            on_stdout_line=stdout.append,
        )
        metadata: dict[str, Any] | None = None
        if result.returncode == 0:
            try:
                decoded = json.loads("\n".join(stdout))
                if isinstance(decoded, dict):
                    metadata = decoded
            except (json.JSONDecodeError, ValueError):
                pass
        return ProbeResult(
            result.returncode,
            metadata,
            result.stderr_tail,
            result.terminated_by,
        )


__all__ = ["FFprobeRunner", "ProbeResult"]
