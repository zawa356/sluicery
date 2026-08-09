#!/usr/bin/env python3
"""RcloneRunner のプロセス・JSON・環境注入試験用疑似 CLI。"""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "noop"

    if mode == "json_then_exit":
        sys.stderr.write("broken json\n")
        sys.stderr.write(
            json.dumps(
                {
                    "level": "info",
                    "msg": "stats",
                    "stats": {
                        "bytes": 12,
                        "totalBytes": 20,
                        "speed": 3.5,
                        "eta": 2,
                        "transfers": 1,
                        "totalTransfers": 2,
                        "errors": 0,
                        "elapsedTime": 1.25,
                    },
                }
            )
            + "\n"
        )
        sys.stdout.write('[{"Path":"file.bin","Size":12}]\n')
        return 0

    if mode == "fail_auth":
        sys.stderr.write("NT_STATUS_LOGON_FAILURE: authentication failed\n")
        return 1

    if mode == "inspect_env":
        report_path = sys.argv[2]
        config_names = sorted(name for name in os.environ if name.startswith("RCLONE_CONFIG_"))
        secret = next(
            (os.environ[name] for name in config_names if name.endswith("_PASS")),
            "",
        )
        with open(report_path, "w", encoding="utf-8") as report:
            json.dump(
                {
                    "argv": sys.argv[1:],
                    "config_count": len(config_names),
                    "secret_in_argv": secret in sys.argv,
                },
                report,
            )
        sys.stderr.write(f"config name {config_names[-1]} value {secret}\n")
        return 0

    if mode == "obscure":
        if len(sys.argv) < 3 or sys.argv[2] != "-":
            return 2
        password = sys.stdin.readline().rstrip("\r\n")
        if password in sys.argv:
            return 3
        sys.stdout.write("obscured-from-stdin\n")
        return 0

    if mode == "spawn_child_and_sleep":
        pid_file = sys.argv[2]
        pid = os.fork()
        if pid == 0:
            time.sleep(3600)
            os._exit(0)
        with open(pid_file, "w", encoding="utf-8") as file:
            file.write(str(pid))
        time.sleep(3600)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
