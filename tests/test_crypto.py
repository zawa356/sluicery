from __future__ import annotations

import sqlite3

from cryptography.fernet import Fernet

from sluicery.db import crypto
from sluicery.db.models import Storage, StorageKind


def test_encrypted_column_round_trips(db_session) -> None:
    storage = Storage(
        name="s1",
        kind=StorageKind.LOCAL,
        credentials_encrypted={"token": "super-secret-value"},
    )
    db_session.add(storage)
    db_session.commit()
    db_session.refresh(storage)

    assert storage.credentials_encrypted == {"token": "super-secret-value"}


def test_encrypted_column_is_not_plaintext_on_disk(db_session, db_path) -> None:
    storage = Storage(
        name="s1",
        kind=StorageKind.LOCAL,
        credentials_encrypted={"token": "super-secret-value"},
    )
    db_session.add(storage)
    db_session.commit()
    storage_id = storage.id
    db_session.close()

    raw = sqlite3.connect(str(db_path))
    row = raw.execute(
        "SELECT credentials_encrypted FROM storage WHERE id = ?", (storage_id,)
    ).fetchone()
    raw.close()

    assert row is not None
    assert "super-secret-value" not in row[0]


def test_secret_key_fingerprint_first_boot_is_silent(db_session, secret_key: str) -> None:
    message = crypto.check_secret_key_fingerprint(db_session, secret_key)
    assert message is None


def test_secret_key_fingerprint_matches_second_boot(db_session, secret_key: str) -> None:
    crypto.check_secret_key_fingerprint(db_session, secret_key)
    message = crypto.check_secret_key_fingerprint(db_session, secret_key)
    assert message is None


def test_secret_key_fingerprint_warns_on_mismatch(db_session, secret_key: str) -> None:
    crypto.check_secret_key_fingerprint(db_session, secret_key)
    different_key = Fernet.generate_key().decode()
    message = crypto.check_secret_key_fingerprint(db_session, different_key)
    assert message is not None
    assert "SECRET_KEY" in message
