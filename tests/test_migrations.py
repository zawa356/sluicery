from __future__ import annotations

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config

REPO_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config(db_path: Path) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.cmd_opts = argparse.Namespace(x=[f"db_path={db_path}"])
    return cfg


def test_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    db_path = tmp_path / "migration-test.db"
    cfg = _alembic_config(db_path)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    import sqlite3

    con = sqlite3.connect(str(db_path))
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()

    assert {"user", "storage", "item", "target", "task", "setting"} <= tables
