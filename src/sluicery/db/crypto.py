"""暗号化カラム型と `SECRET_KEY` の指紋チェック（要件定義 §6.5、docs/phase2_指示書.md §3.4）。

ログ・UI 出力のマスクはこの層の責務ではない（別層に置く）。ここは暗号化のみを担う。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Text
from sqlalchemy.orm import Session
from sqlalchemy.types import TypeDecorator

# プロセス内で共有する暗号化キー。EncryptedJSON はモデル定義時（インポート時）に
# 生成されるため、SECRET_KEY が確定してから set_encryption_key() で注入する。
_key_holder: dict[str, str] = {}


def set_encryption_key(key: str) -> None:
    _key_holder["key"] = key


def get_encryption_key() -> str:
    try:
        return _key_holder["key"]
    except KeyError as exc:
        raise RuntimeError(
            "暗号化キーが未設定です。set_encryption_key() を先に呼び出してください。"
        ) from exc


class EncryptedJSON(TypeDecorator):
    """JSON を Fernet で暗号化して TEXT カラムに保存する。"""

    impl = Text
    cache_ok = True

    def __init__(
        self,
        key_provider: Callable[[], str] = get_encryption_key,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._key_provider = key_provider

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        fernet = Fernet(self._key_provider().encode("utf-8"))
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        return fernet.encrypt(raw).decode("utf-8")

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        fernet = Fernet(self._key_provider().encode("utf-8"))
        raw = fernet.decrypt(value.encode("utf-8"))
        return json.loads(raw.decode("utf-8"))


FINGERPRINT_SETTING_KEY = "_internal.secret_key_fingerprint"

SECRET_KEY_MISMATCH_WARNING = """WARNING: SECRET_KEY が前回起動時と異なります。
  保存済みの認証情報は復号できません。各 Storage の認証情報を再入力してください。
  意図しない変更の場合、以前の SECRET_KEY に戻してください。"""


def secret_key_fingerprint(secret_key: str) -> str:
    """`SECRET_KEY` のローテーション非対応方針のもと、鍵の変化検知にのみ使う指紋。"""
    return hashlib.sha256(secret_key.encode("utf-8")).hexdigest()


def check_secret_key_fingerprint(session: Session, secret_key: str) -> str | None:
    """初回起動時は指紋を保存し、以降は照合する。

    不一致の場合、警告メッセージを返す（呼び出し側で出力する）。起動は継続してよい。
    一致、または初回保存の場合は None を返す。
    """
    from sluicery.db.models import Setting

    fingerprint = secret_key_fingerprint(secret_key)
    row = session.get(Setting, FINGERPRINT_SETTING_KEY)

    if row is None:
        from datetime import UTC, datetime

        session.add(
            Setting(
                key=FINGERPRINT_SETTING_KEY,
                value_json=fingerprint,
                updated_at=datetime.now(UTC),
            )
        )
        session.commit()
        return None

    if row.value_json != fingerprint:
        return SECRET_KEY_MISMATCH_WARNING

    return None


__all__ = [
    "EncryptedJSON",
    "InvalidToken",
    "check_secret_key_fingerprint",
    "get_encryption_key",
    "secret_key_fingerprint",
    "set_encryption_key",
]
