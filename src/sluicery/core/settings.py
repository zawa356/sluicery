"""運用パラメータの設定アクセサ（docs/phase2_指示書.md §6）。

二層構造：
- インフラ・セキュリティ設定 → `.env`（`sluicery.config.Settings`）
- 運用パラメータ → `setting` テーブル（本モジュール）

**既定値はコード側（`CODE_DEFAULTS`）に定義し、`setting` テーブルには
ユーザーが上書きした項目のみを保存する。** 初期投入で全項目を DB に
書き込まない（将来コード側の既定値を変えても既存環境に反映されるように
するため）。

Phase 2 では定義と読み書きのみを行う。実際に参照する処理（スケジューラ、
ダウンロードオプション合成など）は各フェーズで実装する。

キャッシュは持たない（マルチプロセス構成のためプロセス跨ぎの無効化が
できない。§6.4 の「キャッシュなし」を選択。基本設計 §7 に記録）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from sluicery.db.models import Setting


@dataclass(frozen=True)
class SettingSpec:
    key: str
    type_: type
    default: Any


# 要件定義から拾える運用パラメータ（§6.3）。`defaults.video.*` / `defaults.music.*`
# は Phase 4 のオプション合成モデルで層3として使うキー体系のみ、ここでは定義しない
# （値の構造が Profile の合成モデルに依存するため、Phase 4 で追加する）。
CODE_DEFAULTS: dict[str, SettingSpec] = {
    "staging.warn_pct": SettingSpec("staging.warn_pct", int, 80),
    "staging.stop_pct": SettingSpec("staging.stop_pct", int, 90),
    "log.retention_days": SettingSpec("log.retention_days", int, 30),
    "schedule.discover_cron": SettingSpec("schedule.discover_cron", str, "0 */6 * * *"),
    "schedule.download_cron": SettingSpec("schedule.download_cron", str, "0 */6 * * *"),
    "schedule.integrity_cron": SettingSpec("schedule.integrity_cron", str, "0 3 * * *"),
    "schedule.jitter_minutes": SettingSpec("schedule.jitter_minutes", int, 5),
    "schedule.download_window": SettingSpec("schedule.download_window", str, None),
    "ytdlp.update_cron": SettingSpec("ytdlp.update_cron", str, "0 4 * * 0"),
    "ytdlp.smoketest_url": SettingSpec("ytdlp.smoketest_url", str, ""),
    "download.item_concurrency": SettingSpec("download.item_concurrency", int, 1),
    "download.concurrent_fragments": SettingSpec("download.concurrent_fragments", int, 3),
    "download.sleep_requests": SettingSpec("download.sleep_requests", float, 1.5),
    "download.sleep_interval": SettingSpec("download.sleep_interval", int, 3),
    "download.max_sleep_interval": SettingSpec("download.max_sleep_interval", int, 12),
    "download.limit_rate": SettingSpec("download.limit_rate", str, "8M"),
    "download.retries": SettingSpec("download.retries", int, 5),
    "download.fragment_retries": SettingSpec("download.fragment_retries", int, 10),
    "retry.max_attempts": SettingSpec("retry.max_attempts", int, 5),
}


class UnknownSettingKeyError(KeyError):
    pass


def _cast(type_: type, raw: Any) -> Any:
    if raw is None or type_ is type(None):
        return raw
    if type_ is bool and isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return type_(raw)


def get(session: Session, key: str) -> Any:
    """上書きがあればその値を、なければコード側既定値を返す。"""
    spec = CODE_DEFAULTS.get(key)
    if spec is None:
        raise UnknownSettingKeyError(key)

    row = session.get(Setting, key)
    if row is None:
        return spec.default
    return _cast(spec.type_, json.loads(row.value_json))


def is_overridden(session: Session, key: str) -> bool:
    if key not in CODE_DEFAULTS:
        raise UnknownSettingKeyError(key)
    return session.get(Setting, key) is not None


def set_override(session: Session, key: str, value: Any) -> None:
    spec = CODE_DEFAULTS.get(key)
    if spec is None:
        raise UnknownSettingKeyError(key)

    casted = _cast(spec.type_, value)
    row = session.get(Setting, key)
    encoded = json.dumps(casted, ensure_ascii=False)
    if row is None:
        session.add(Setting(key=key, value_json=encoded, updated_at=datetime.now(UTC)))
    else:
        row.value_json = encoded
        row.updated_at = datetime.now(UTC)
    session.commit()


def unset_override(session: Session, key: str) -> None:
    if key not in CODE_DEFAULTS:
        raise UnknownSettingKeyError(key)
    row = session.get(Setting, key)
    if row is not None:
        session.delete(row)
        session.commit()


@dataclass(frozen=True)
class SettingListEntry:
    key: str
    value: Any
    is_override: bool


def list_all(session: Session) -> list[SettingListEntry]:
    return [
        SettingListEntry(key=key, value=get(session, key), is_override=is_overridden(session, key))
        for key in CODE_DEFAULTS
    ]


class OperationalSettings:
    """キー文字列を直に扱わせない、型付きの属性アクセサ（§6.4）。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def staging_warn_pct(self) -> int:
        return get(self._session, "staging.warn_pct")

    @property
    def staging_stop_pct(self) -> int:
        return get(self._session, "staging.stop_pct")

    @property
    def log_retention_days(self) -> int:
        return get(self._session, "log.retention_days")

    @property
    def schedule_discover_cron(self) -> str:
        return get(self._session, "schedule.discover_cron")

    @property
    def schedule_download_cron(self) -> str:
        return get(self._session, "schedule.download_cron")

    @property
    def schedule_integrity_cron(self) -> str:
        return get(self._session, "schedule.integrity_cron")

    @property
    def schedule_jitter_minutes(self) -> int:
        return get(self._session, "schedule.jitter_minutes")

    @property
    def schedule_download_window(self) -> str | None:
        return get(self._session, "schedule.download_window")

    @property
    def ytdlp_update_cron(self) -> str:
        return get(self._session, "ytdlp.update_cron")

    @property
    def ytdlp_smoketest_url(self) -> str:
        return get(self._session, "ytdlp.smoketest_url")

    @property
    def download_item_concurrency(self) -> int:
        return get(self._session, "download.item_concurrency")

    @property
    def download_concurrent_fragments(self) -> int:
        return get(self._session, "download.concurrent_fragments")

    @property
    def download_sleep_requests(self) -> float:
        return get(self._session, "download.sleep_requests")

    @property
    def download_sleep_interval(self) -> int:
        return get(self._session, "download.sleep_interval")

    @property
    def download_max_sleep_interval(self) -> int:
        return get(self._session, "download.max_sleep_interval")

    @property
    def download_limit_rate(self) -> str:
        return get(self._session, "download.limit_rate")

    @property
    def download_retries(self) -> int:
        return get(self._session, "download.retries")

    @property
    def download_fragment_retries(self) -> int:
        return get(self._session, "download.fragment_retries")

    @property
    def retry_max_attempts(self) -> int:
        return get(self._session, "retry.max_attempts")


__all__ = [
    "CODE_DEFAULTS",
    "OperationalSettings",
    "SettingListEntry",
    "SettingSpec",
    "UnknownSettingKeyError",
    "get",
    "is_overridden",
    "list_all",
    "set_override",
    "unset_override",
]
