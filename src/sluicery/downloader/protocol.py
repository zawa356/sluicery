"""yt-dlp 標準出力のフレーミング規約（docs/phase3_指示書.md §3.3）。

stdout には進捗 JSON（`--progress-template`）と `--print` 出力が混在しうるため、
行頭のプレフィックスで区別する。実際に yt-dlp へどう予約引数として注入するかは
Phase 4（`core/options.py`）の責務。ここでは規約のみを定義する。
"""

from __future__ import annotations

PROGRESS_PREFIX = "SLUICERY_PROGRESS "
PRINT_PREFIX = "SLUICERY_PRINT "
RESULT_PREFIX = "SLUICERY_RESULT "

__all__ = ["PRINT_PREFIX", "PROGRESS_PREFIX", "RESULT_PREFIX"]
