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

`setting` テーブルにはユーザーが操作可能な運用パラメータ以外に、内部状態
（`SECRET_KEY` の指紋など）も同居する。内部状態のキーは `_internal.*` の
名前空間に置き、`CODE_DEFAULTS` に登録しない（`db/crypto.py` の
`FINGERPRINT_SETTING_KEY` を参照）。この関数群（`get` / `set_override` /
`unset_override` / `list_all`）は `CODE_DEFAULTS` に登録されたキーしか
扱わないため、`_internal.*` は `sluicery settings list/get/set/unset` の
対象や、将来実装する設定エクスポート（Phase 17）の対象に自動的に含まれない。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from sluicery.db.repositories.setting import SettingRepository


@dataclass(frozen=True)
class SettingSpec:
    key: str
    type_: type
    default: Any


# 要件定義から拾える運用パラメータ。`defaults.video.*` / `defaults.music.*` は
# Phase 4 のオプション合成で L3 として参照する。
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
    "defaults.video.format_selector": SettingSpec(
        "defaults.video.format_selector", str, "bv*[height<=1080]+ba/b[height<=1080]/b"
    ),
    "defaults.video.container": SettingSpec("defaults.video.container", str, "mkv"),
    "defaults.video.audio_extract": SettingSpec("defaults.video.audio_extract", bool, False),
    "defaults.video.embed_metadata": SettingSpec("defaults.video.embed_metadata", bool, True),
    "defaults.video.embed_thumbnail": SettingSpec("defaults.video.embed_thumbnail", bool, True),
    "defaults.video.embed_chapters": SettingSpec("defaults.video.embed_chapters", bool, True),
    "defaults.video.subtitle_langs": SettingSpec("defaults.video.subtitle_langs", str, "ja,en"),
    "defaults.video.subtitle_auto": SettingSpec("defaults.video.subtitle_auto", bool, False),
    "defaults.video.subtitle_embed": SettingSpec("defaults.video.subtitle_embed", bool, True),
    "defaults.music.format_selector": SettingSpec(
        "defaults.music.format_selector", str, "bestaudio/best"
    ),
    "defaults.music.audio_extract": SettingSpec("defaults.music.audio_extract", bool, True),
    "defaults.music.audio_format": SettingSpec("defaults.music.audio_format", str, "opus"),
    "defaults.music.audio_quality": SettingSpec("defaults.music.audio_quality", str, "0"),
    "defaults.music.embed_metadata": SettingSpec("defaults.music.embed_metadata", bool, True),
    "defaults.music.embed_thumbnail": SettingSpec("defaults.music.embed_thumbnail", bool, True),
    "defaults.music.parse_metadata": SettingSpec(
        "defaults.music.parse_metadata",
        list,
        [
            "playlist_index:%(track_number)s",
            "playlist_title:%(album)s",
            "uploader:%(artist)s",
            "upload_date:%(release_date)s",
        ],
    ),
    "retry.max_attempts": SettingSpec("retry.max_attempts", int, 5),
    "ytdlp.auto_install": SettingSpec("ytdlp.auto_install", bool, True),
    "ytdlp.keep_versions": SettingSpec("ytdlp.keep_versions", int, 3),
    "ytdlp.idle_timeout_sec": SettingSpec("ytdlp.idle_timeout_sec", int, 300),
    "ytdlp.absolute_timeout_sec": SettingSpec("ytdlp.absolute_timeout_sec", int, 21600),
    "ytdlp.discover_timeout_sec": SettingSpec("ytdlp.discover_timeout_sec", int, 300),
    "ytdlp.term_grace_sec": SettingSpec("ytdlp.term_grace_sec", int, 10),
    "ytdlp.stderr_tail_kb": SettingSpec("ytdlp.stderr_tail_kb", int, 64),
}


class UnknownSettingKeyError(KeyError):
    pass


def _cast(type_: type, raw: Any) -> Any:
    if raw is None or type_ is type(None):
        return raw
    if type_ is bool and isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if type_ is list:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise ValueError("JSON文字列の配列を指定してください")
        return parsed
    return type_(raw)


def get(session: Session, key: str) -> Any:
    """上書きがあればその値を、なければコード側既定値を返す。"""
    spec = CODE_DEFAULTS.get(key)
    if spec is None:
        raise UnknownSettingKeyError(key)

    row = SettingRepository(session).get(key)
    if row is None:
        return spec.default
    return _cast(spec.type_, json.loads(row.value_json))


def is_overridden(session: Session, key: str) -> bool:
    if key not in CODE_DEFAULTS:
        raise UnknownSettingKeyError(key)
    return SettingRepository(session).get(key) is not None


def set_override(session: Session, key: str, value: Any) -> None:
    spec = CODE_DEFAULTS.get(key)
    if spec is None:
        raise UnknownSettingKeyError(key)

    casted = _cast(spec.type_, value)
    encoded = json.dumps(casted, ensure_ascii=False)
    SettingRepository(session).set_override(key, encoded)


def unset_override(session: Session, key: str) -> None:
    if key not in CODE_DEFAULTS:
        raise UnknownSettingKeyError(key)
    SettingRepository(session).delete_override(key)


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

    @property
    def ytdlp_auto_install(self) -> bool:
        return get(self._session, "ytdlp.auto_install")

    @property
    def ytdlp_keep_versions(self) -> int:
        return get(self._session, "ytdlp.keep_versions")

    @property
    def ytdlp_idle_timeout_sec(self) -> int:
        return get(self._session, "ytdlp.idle_timeout_sec")

    @property
    def ytdlp_absolute_timeout_sec(self) -> int:
        return get(self._session, "ytdlp.absolute_timeout_sec")

    @property
    def ytdlp_discover_timeout_sec(self) -> int:
        return get(self._session, "ytdlp.discover_timeout_sec")

    @property
    def ytdlp_term_grace_sec(self) -> int:
        return get(self._session, "ytdlp.term_grace_sec")

    @property
    def ytdlp_stderr_tail_kb(self) -> int:
        return get(self._session, "ytdlp.stderr_tail_kb")


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
