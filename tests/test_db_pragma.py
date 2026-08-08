from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from sluicery.db.models import PlaylistProfile


def test_foreign_keys_pragma_is_on(engine) -> None:
    with engine.connect() as conn:
        value = conn.execute(text("PRAGMA foreign_keys")).scalar()
    assert value == 1


def test_foreign_key_violation_is_rejected(db_session) -> None:
    bad = PlaylistProfile(playlist_id=9999, profile_id=9999, storage_id=9999)
    db_session.add(bad)
    try:
        db_session.commit()
        raise AssertionError("外部キー違反が拒否されなかった")
    except IntegrityError:
        db_session.rollback()
