#!/usr/bin/env python3
"""downloader/ytdlp.py のテスト用に yt-dlp を模した疑似スクリプト。

実際の yt-dlp・ネットワークを使わずに、進捗出力・プロセスグループ・
タイムアウト周りの挙動を検証するために使う（docs/phase3_指示書.md §10.2）。
最初の引数でモードを切り替える。
"""

from __future__ import annotations

import os
import sys
import time


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "noop"

    if mode == "progress_then_exit":
        for i in range(3):
            print(f'SLUICERY_PROGRESS {{"status": "downloading", "downloaded_bytes": {i}}}')
            sys.stdout.flush()
        print("SLUICERY_PRINT done")
        print('SLUICERY_RESULT {"file_path": "done", "format_id": "137+140"}')
        return 0

    if mode == "fail_429":
        sys.stderr.write("HTTP Error 429: Too Many Requests\n")
        return 1

    if mode == "fail_unknown":
        sys.stderr.write("something completely unexpected happened\n")
        return 1

    if mode == "sleep_forever":
        time.sleep(3600)
        return 0

    if mode == "periodic_progress":
        while True:
            print('SLUICERY_PROGRESS {"status": "downloading"}')
            sys.stdout.flush()
            time.sleep(0.2)

    if mode == "spawn_child_and_sleep":
        pid_file = sys.argv[2]
        pid = os.fork()
        if pid == 0:
            time.sleep(3600)
            os._exit(0)
        with open(pid_file, "w") as f:
            f.write(str(pid))
        time.sleep(3600)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
