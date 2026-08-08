from __future__ import annotations

from datetime import UTC

from sluicery.db.models import User


def test_created_at_is_utc_aware(db_session) -> None:
    user = User(username="admin", password_hash="x")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    assert user.created_at.tzinfo is not None
    assert user.created_at.utcoffset() == UTC.utcoffset(None)


def test_updated_at_changes_on_update(db_session) -> None:
    user = User(username="admin", password_hash="x")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    first_updated_at = user.updated_at

    user.password_hash = "y"
    db_session.commit()
    db_session.refresh(user)

    assert user.updated_at >= first_updated_at
    assert user.updated_at.tzinfo is not None
