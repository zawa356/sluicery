"""FastAPI アプリケーションのファクトリ。

現バージョンではヘルスチェック用の骨格のみ。認証・ルーティングは
実装順序 #9（認証、Web UI 骨格）で追加する。
"""

from __future__ import annotations

from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="sluicery")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
