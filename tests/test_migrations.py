from __future__ import annotations

import argparse
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

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

    assert {"user", "auth_session", "storage", "item", "target", "task", "setting"} <= tables
    engine = create_engine(f"sqlite:///{db_path}")
    playlist_columns = {column["name"] for column in inspect(engine).get_columns("playlist")}
    engine.dispose()
    assert {"cookie_enabled", "cookies_encrypted", "missing_policy"} <= playlist_columns


def test_run_skipped_status_round_trips(tmp_path: Path) -> None:
    db_path = tmp_path / "run-skipped.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO run (trigger, kind, status, started_at, finished_at)
                   VALUES ('schedule', 'discover', 'skipped', CURRENT_TIMESTAMP,
                           CURRENT_TIMESTAMP)"""
            )
        )

    command.downgrade(cfg, "b8c9d0e1f2a3")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT status FROM run")).scalar_one() == "cancelled"
    command.upgrade(cfg, "head")
    engine.dispose()


def test_profile_tristate_migration_preserves_values_and_round_trips(tmp_path: Path) -> None:
    db_path = tmp_path / "profile-tristate.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "01f4e2ff8faf")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO profile (
                    name, kind, layout_strategy, audio_extract, embed_metadata,
                    embed_thumbnail, embed_chapters, subtitle_auto, subtitle_embed,
                    expert_mode, allow_exec, created_at, updated_at
                ) VALUES (
                    'legacy', 'video', 'flat', 0, 1, 1, 0, 0, 0,
                    0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )"""
            )
        )

    command.upgrade(cfg, "head")
    columns = {column["name"]: column for column in inspect(engine).get_columns("profile")}
    tristate_names = (
        "audio_extract",
        "embed_metadata",
        "embed_thumbnail",
        "embed_chapters",
        "subtitle_auto",
        "subtitle_embed",
    )
    for name in tristate_names:
        assert columns[name]["nullable"] is True

    with engine.begin() as conn:
        values = conn.execute(
            text(
                """SELECT audio_extract, embed_metadata, embed_thumbnail,
                          embed_chapters, subtitle_auto, subtitle_embed
                   FROM profile WHERE name = 'legacy'"""
            )
        ).one()
        assert tuple(values) == (0, 1, 1, 0, 0, 0)
        conn.execute(
            text(
                """UPDATE profile SET audio_extract = NULL, embed_metadata = NULL,
                          embed_thumbnail = NULL, embed_chapters = NULL,
                          subtitle_auto = NULL, subtitle_embed = NULL
                   WHERE name = 'legacy'"""
            )
        )

    command.downgrade(cfg, "01f4e2ff8faf")
    downgraded = {column["name"]: column for column in inspect(engine).get_columns("profile")}
    for name in tristate_names:
        assert downgraded[name]["nullable"] is False
    with engine.connect() as conn:
        downgraded_values = conn.execute(
            text(
                """SELECT audio_extract, embed_metadata, embed_thumbnail,
                          embed_chapters, subtitle_auto, subtitle_embed
                   FROM profile WHERE name = 'legacy'"""
            )
        ).one()
    assert tuple(downgraded_values) == (0, 1, 1, 0, 0, 0)

    command.upgrade(cfg, "head")
    engine.dispose()


def test_phase6_task_migration_preserves_existing_queue_and_round_trips(tmp_path: Path) -> None:
    db_path = tmp_path / "phase6-task.db"
    cfg = _alembic_config(db_path)
    command.upgrade(cfg, "5b8c9d1e2f30")

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO task (
                    type, target_ref_type, target_ref_id, worker_class, priority,
                    status, attempts, max_attempts, created_at
                ) VALUES (
                    'download', 'target', 1, 'network', 0,
                    'queued', 0, 5, CURRENT_TIMESTAMP
                )"""
            )
        )

    command.upgrade(cfg, "head")
    columns = {column["name"] for column in inspect(engine).get_columns("task")}
    assert {
        "available_at",
        "blocked_until",
        "blocked_reason",
        "heartbeat_at",
        "worker_id",
        "cancel_requested",
    } <= columns
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT type, status, cancel_requested FROM task WHERE target_ref_id = 1")
        ).one()
        assert tuple(row) == ("download", "queued", 0)

    command.downgrade(cfg, "5b8c9d1e2f30")
    downgraded_columns = {column["name"] for column in inspect(engine).get_columns("task")}
    assert "heartbeat_at" not in downgraded_columns
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM task")).scalar_one() == 1

    command.upgrade(cfg, "head")
    engine.dispose()
