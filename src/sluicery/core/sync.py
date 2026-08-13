"""Playlist の discover / download 二相同期に関するドメイン処理。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from sluicery.core.target_state import transition_item
from sluicery.db.models import ItemMembership
from sluicery.db.repositories.item import ItemRepository
from sluicery.db.repositories.playlist import PlaylistRepository
from sluicery.db.repositories.playlist_profile import PlaylistProfileRepository
from sluicery.db.repositories.target import TargetRepository


@dataclass(frozen=True)
class SyncStats:
    new_items: int = 0
    delisted_items: int = 0
    targets_queued: int = 0
    targets_remaining: int = 0
    downloaded: int = 0
    failed: int = 0
    blocked: int = 0
    empty_result: bool = False

    def to_dict(self) -> dict[str, int | bool]:
        return asdict(self)


def parse_discover_entries(lines: list[str]) -> list[dict[str, Any]]:
    """yt-dlp の flat-playlist JSON 行を Item 用の値へ正規化する。

    壊れた行や、再取得に必要な ID / HTTP(S) URL を欠く行は採用しない。
    同じ source_id が複数回現れた場合は最後のメタデータを採用する。
    """
    entries: dict[str, dict[str, Any]] = {}
    for line in lines:
        try:
            raw = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(raw, dict):
            continue
        source_id = _text(raw.get("id"))
        source_url = _source_url(raw)
        if source_id is None or source_url is None:
            continue
        entries[source_id] = {
            "source_id": source_id,
            "source_url": source_url,
            "title": _text(raw.get("title")),
            "uploader": _text(raw.get("uploader")),
            "duration": _integer(raw.get("duration")),
            "upload_date": _text(raw.get("upload_date")),
            "playlist_index": _integer(raw.get("playlist_index")),
            "metadata_json": raw,
        }
    return list(entries.values())


def apply_discovery(
    session: Session,
    playlist_id: int,
    entries: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> SyncStats:
    """discover 差分を適用する。空結果と dry-run はドメインDBを変更しない。"""
    observed_at = now or datetime.now(UTC)
    # 呼び出し側が直接組み立てたテスト入力にも重複が入り得るため、ここでも畳む。
    discovered = {entry["source_id"]: entry for entry in entries if entry.get("source_id")}
    if not discovered:
        return SyncStats(empty_result=True)

    item_repo = ItemRepository(session)
    existing_items = item_repo.list_for_playlist(playlist_id)
    existing = {item.source_id: item for item in existing_items}
    discovered_ids = set(discovered)
    new_source_ids = discovered_ids - set(existing)
    delisted = [
        item
        for item in existing_items
        if item.membership == ItemMembership.ACTIVE and item.source_id not in discovered_ids
    ]
    reappeared = [
        item
        for item in existing_items
        if item.membership == ItemMembership.DELISTED and item.source_id in discovered_ids
    ]
    enabled_profiles = PlaylistProfileRepository(session).list_enabled_for_playlist(playlist_id)
    stats = SyncStats(
        new_items=len(new_source_ids),
        delisted_items=len(delisted),
    )
    if dry_run:
        return stats

    rows: list[dict[str, Any]] = []
    for entry in discovered.values():
        row = dict(entry)
        row["last_seen_at"] = observed_at
        rows.append(row)
    upserted = item_repo.upsert_many(playlist_id, rows, commit=False)
    for item in delisted:
        transition_item(session, item.id, ItemMembership.DELISTED, now=observed_at, commit=False)
    for item in reappeared:
        transition_item(session, item.id, ItemMembership.ACTIVE, now=observed_at, commit=False)

    new_item_ids = [item.id for item in upserted if item.source_id in new_source_ids]
    TargetRepository(session).create_missing_for_items(
        new_item_ids,
        [profile.id for profile in enabled_profiles],
        commit=False,
    )
    if not PlaylistRepository(session).set_last_discover_at(playlist_id, observed_at, commit=False):
        session.rollback()
        raise LookupError(f"Playlist {playlist_id} が見つかりません")
    session.commit()
    return stats


def _source_url(raw: dict[str, Any]) -> str | None:
    for key in ("webpage_url", "original_url", "url"):
        value = _text(raw.get(key))
        if value is None:
            continue
        try:
            parsed = urlsplit(value)
        except ValueError:
            continue
        if parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
            return value
    return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value and value != "NA" else None


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return round(value)
    return None


__all__ = ["SyncStats", "apply_discovery", "parse_discover_entries"]
