"""DB エンジンとセッション管理（PRAGMA 設定、§3 全般）。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from sluicery.db.models import metadata_obj


def _set_sqlite_pragma(dbapi_connection: object, connection_record: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def create_engine_for(db_path: Path) -> Engine:
    """SQLite 用のエンジンを作成する。PRAGMA は接続イベントで毎接続時に設定する。"""
    from sqlalchemy import create_engine

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", _set_sqlite_pragma)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """呼び出し側でセッションのライフサイクルを制御するための補助コンテキスト。

    リポジトリ層はセッションを自前で作らない（§7.1）。この関数は CLI や
    テストなど、セッションの「外側」の呼び出し元が使う想定。
    """
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = [
    "create_engine_for",
    "create_session_factory",
    "metadata_obj",
    "session_scope",
]
