"""sluicery のエントリポイント。compose の各サービスから呼び出される。"""

from __future__ import annotations

import argparse
import os
import sys


def _run_web() -> int:
    import uvicorn

    from sluicery.web.app import create_app

    port = int(os.environ.get("HTTP_PORT", "8080"))
    uvicorn.run(create_app(), host="0.0.0.0", port=port, log_level="info")
    return 0


def _run_worker(worker_class: str) -> int:
    # Task キュー・ワーカーの実装は実装順序 #6 で追加する。
    print(f"[sluicery] worker-{worker_class}: 実装準備中（実装順序 #6 で追加）", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sluicery")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("web", help="Web UI + REST API + スケジューラを起動する")

    worker_parser = sub.add_parser("worker", help="Task ワーカーを起動する")
    worker_parser.add_argument("--class", dest="worker_class", choices=["network", "compute"], required=True)

    args = parser.parse_args(argv)

    if args.command == "web":
        return _run_web()
    if args.command == "worker":
        return _run_worker(args.worker_class)

    parser.error(f"未知のコマンド: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
