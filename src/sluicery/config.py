"""sluicery の起動時設定。`.env` の読み込みと検証を行う（要件定義 §17）。

**`.env` はセキュリティ境界とインフラ設定に限定する。** 運用パラメータ
（Staging しきい値、cron 式など）はここに含めない。`core.settings` の
`setting` テーブル経由の設定を参照すること（`docs/基本設計.md` D-005）。
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet
from pydantic import ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_UMASK_RE = re.compile(r"^[0-7]{3,4}$")

# ADMIN_PASSWORD は初回起動時にしか使わないためログや config check には出さないが、
# 値そのものはクレデンシャルなので一律マスク対象に含める。
SECRET_FIELD_NAMES = frozenset({"SECRET_KEY", "ADMIN_PASSWORD"})


def validate_secret_key(value: str) -> str:
    """`SECRET_KEY` が Fernet 鍵として使用可能であることを確認する。"""
    if not value:
        raise ValueError("値が空です")
    try:
        Fernet(value.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - Fernet の例外型は多岐にわたる
        raise ValueError(
            "Fernet 鍵として不正です。生成例: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        ) from exc
    return value


def validate_tz(value: str) -> str:
    """有効な IANA タイムゾーンであることを確認する。"""
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"'{value}' は有効な IANA タイムゾーンではありません") from exc
    return value


def validate_umask(value: str) -> str:
    if not _UMASK_RE.match(value):
        raise ValueError(f"'{value}' は3〜4桁の8進数で指定してください（例: 022）")
    return value


def validate_writable_dir(path: Path) -> Path:
    if not path.exists():
        raise ValueError(f"'{path}' が存在しません")
    if not path.is_dir():
        raise ValueError(f"'{path}' はディレクトリではありません")
    if not os.access(path, os.W_OK):
        raise ValueError(f"'{path}' に書き込めません")
    return path


class Settings(BaseSettings):
    """`.env` から読み込む設定（要件定義 §17）。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    SECRET_KEY: str
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str | None = None
    TZ: str = "Asia/Tokyo"
    PUID: int = 1000
    PGID: int = 1000
    UMASK: str = "022"
    HTTP_PORT: int = 8080
    DATA_DIR: Path = Path("/data")
    STAGING_DIR: Path | None = None
    MEDIA_ROOT: Path = Path("/mnt/media")
    ALLOW_EXEC: bool = False
    AUTO_MIGRATE: bool = True
    DB_PATH: Path | None = None
    LOG_LEVEL: str = "INFO"

    @field_validator("SECRET_KEY")
    @classmethod
    def _check_secret_key(cls, v: str) -> str:
        return validate_secret_key(v)

    @field_validator("TZ")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        return validate_tz(v)

    @field_validator("UMASK")
    @classmethod
    def _check_umask(cls, v: str) -> str:
        return validate_umask(v)

    @field_validator("HTTP_PORT")
    @classmethod
    def _check_port(cls, v: int) -> int:
        if not (1 <= v <= 65535):
            raise ValueError("1〜65535 の範囲で指定してください")
        return v

    @field_validator("DATA_DIR", "MEDIA_ROOT")
    @classmethod
    def _check_writable(cls, v: Path) -> Path:
        return validate_writable_dir(v)

    @model_validator(mode="after")
    def _fill_computed_defaults(self) -> Settings:
        if self.STAGING_DIR is None:
            self.STAGING_DIR = self.DATA_DIR / "staging"
        if self.DB_PATH is None:
            self.DB_PATH = self.DATA_DIR / "sluicery.db"
        validate_writable_dir(self.STAGING_DIR)
        return self


SECRET_KEY_MISSING_MESSAGE = """ERROR: SECRET_KEY が設定されていません。
  .env に SECRET_KEY を設定してください。
  生成例: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  この鍵を紛失すると、保存済みの認証情報を復号できなくなります。バックアップを取得してください。"""


def _is_secret_key_missing(errors: list[dict]) -> bool:
    return any(
        e["loc"] == ("SECRET_KEY",) and e["type"] in ("missing", "string_type")
        for e in errors
    )


def format_validation_error(exc: ValidationError) -> str:
    """スタックトレースの代わりに表示する、原因が分かるメッセージを組み立てる。"""
    errors = exc.errors()
    if _is_secret_key_missing(errors):
        return SECRET_KEY_MISSING_MESSAGE

    lines = ["ERROR: 設定の検証に失敗しました。"]
    for e in errors:
        field = ".".join(str(p) for p in e["loc"])
        lines.append(f"  {field}: {e['msg']}")
    return "\n".join(lines)


def load_settings() -> Settings:
    """Settings を読み込む。失敗時は明確なメッセージを stderr に出して終了する。"""
    try:
        return Settings()
    except ValidationError as exc:
        print(format_validation_error(exc), file=sys.stderr)
        sys.exit(1)


@dataclass
class FieldCheckResult:
    name: str
    ok: bool
    display_value: str
    message: str | None = None


def _mask(name: str, value: object) -> str:
    if name in SECRET_FIELD_NAMES and value not in (None, ""):
        return "********"
    return "" if value is None else str(value)


def check_config() -> list[FieldCheckResult]:
    """`sluicery config check` 用に、全項目を検証結果付きで返す。

    一部の項目が不正でも、他の項目の結果を分かる範囲で返す
    （Settings 全体の構築が失敗する場合は、環境値から個別に再検証する）。
    """
    try:
        settings = Settings()
    except ValidationError as exc:
        return _check_config_from_errors(exc)

    results = []
    for name in Settings.model_fields:
        value = getattr(settings, name)
        results.append(FieldCheckResult(name=name, ok=True, display_value=_mask(name, value)))
    return results


def _check_config_from_errors(exc: ValidationError) -> list[FieldCheckResult]:
    error_by_field = {".".join(str(p) for p in e["loc"]): e["msg"] for e in exc.errors()}
    raw = _read_raw_env()

    results = []
    for name, field in Settings.model_fields.items():
        raw_value = raw.get(name)
        if name in error_by_field:
            results.append(
                FieldCheckResult(
                    name=name,
                    ok=False,
                    display_value=_mask(name, raw_value),
                    message=error_by_field[name],
                )
            )
        else:
            display = raw_value if raw_value is not None else field.get_default(call_default_factory=True)
            results.append(FieldCheckResult(name=name, ok=True, display_value=_mask(name, display)))
    return results


def _read_raw_env() -> dict[str, str]:
    values: dict[str, str] = {}
    env_path = Path(".env")
    if env_path.exists():
        try:
            from dotenv import dotenv_values

            values.update({k: v for k, v in dotenv_values(env_path).items() if v is not None})
        except ImportError:
            pass
    values.update(os.environ)
    return {k: v for k, v in values.items() if k in Settings.model_fields}
