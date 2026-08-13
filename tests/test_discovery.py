from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from sluicery.core.sync import apply_discovery, parse_discover_entries
from sluicery.db.models import (
    Artifact,
    ArtifactRole,
    Item,
    ItemMembership,
    LayoutStrategy,
    Playlist,
    PlaylistKindHint,
    PlaylistProfile,
    Profile,
    ProfileKind,
    Storage,
    StorageKind,
    Target,
    TargetStatus,
)

NOW = datetime(2026, 8, 13, tzinfo=UTC)


def _playlist(db_session, *, profiles: int = 2):
    playlist = Playlist(
        name="list",
        folder_name="list",
        url="https://example.com/list",
        kind_hint=PlaylistKindHint.MIXED,
    )
    storage = Storage(name="storage", kind=StorageKind.LOCAL, config_json={"path": "out"})
    db_session.add_all([playlist, storage])
    db_session.flush()
    assignments = []
    for index in range(profiles):
        profile = Profile(
            name=f"profile-{index}",
            kind=ProfileKind.VIDEO,
            layout_strategy=LayoutStrategy.FLAT,
        )
        db_session.add(profile)
        db_session.flush()
        assignment = PlaylistProfile(
            playlist_id=playlist.id,
            profile_id=profile.id,
            storage_id=storage.id,
            enabled=index == 0 or profiles == 1,
        )
        db_session.add(assignment)
        assignments.append(assignment)
    db_session.commit()
    return playlist, storage, assignments


def _entry(source_id: str, *, title: str = "title", index: int = 1):
    return {
        "source_id": source_id,
        "source_url": f"https://example.com/watch/{source_id}",
        "title": title,
        "uploader": None,
        "duration": 12,
        "upload_date": None,
        "playlist_index": index,
        "metadata_json": {"id": source_id, "title": title},
    }


def test_parse_discover_entries_accepts_missing_optional_metadata():
    lines = [
        json.dumps(
            {
                "id": "one",
                "url": "https://example.com/watch/one",
                "title": "One",
                "uploader": "NA",
                "duration": None,
                "playlist_index": 3,
            }
        ),
        "not-json",
        json.dumps({"id": "no-url", "url": "opaque-id"}),
    ]

    assert parse_discover_entries(lines) == [
        {
            "source_id": "one",
            "source_url": "https://example.com/watch/one",
            "title": "One",
            "uploader": None,
            "duration": None,
            "upload_date": None,
            "playlist_index": 3,
            "metadata_json": {
                "id": "one",
                "url": "https://example.com/watch/one",
                "title": "One",
                "uploader": "NA",
                "duration": None,
                "playlist_index": 3,
            },
        }
    ]


def test_discovery_upserts_and_preserves_first_seen(db_session):
    playlist, _, _ = _playlist(db_session)
    first_seen = NOW - timedelta(days=10)
    existing = Item(
        playlist_id=playlist.id,
        source_id="one",
        source_url="https://example.com/old",
        title="old",
        first_seen_at=first_seen,
        last_seen_at=first_seen,
    )
    db_session.add(existing)
    db_session.commit()

    stats = apply_discovery(
        db_session, playlist.id, [_entry("one", title="new"), _entry("two")], now=NOW
    )

    db_session.refresh(existing)
    assert stats.new_items == 1
    assert existing.title == "new"
    assert existing.first_seen_at == first_seen
    assert existing.last_seen_at == NOW
    targets = db_session.scalars(select(Target).order_by(Target.id)).all()
    assert len(targets) == 1
    assert targets[0].item_id != existing.id


def test_empty_discovery_never_delists_or_updates_timestamp(db_session):
    playlist, _, _ = _playlist(db_session, profiles=1)
    item = Item(
        playlist_id=playlist.id,
        source_id="one",
        source_url="https://example.com/watch/one",
    )
    db_session.add(item)
    db_session.commit()

    stats = apply_discovery(db_session, playlist.id, [], now=NOW)

    db_session.refresh(item)
    db_session.refresh(playlist)
    assert stats.empty_result is True
    assert item.membership == ItemMembership.ACTIVE
    assert playlist.last_discover_at is None


def test_missing_item_is_delisted_without_changing_target_or_artifact(db_session):
    playlist, storage, assignments = _playlist(db_session, profiles=1)
    missing = Item(
        playlist_id=playlist.id,
        source_id="missing",
        source_url="https://example.com/watch/missing",
    )
    present = Item(
        playlist_id=playlist.id,
        source_id="present",
        source_url="https://example.com/watch/present",
    )
    db_session.add_all([missing, present])
    db_session.flush()
    target = Target(
        item_id=missing.id,
        playlist_profile_id=assignments[0].id,
        status=TargetStatus.DOWNLOADED,
    )
    db_session.add(target)
    db_session.flush()
    artifact = Artifact(
        target_id=target.id,
        role=ArtifactRole.SOURCE,
        storage_id=storage.id,
        relative_path="kept.mp4",
    )
    db_session.add(artifact)
    db_session.commit()

    stats = apply_discovery(db_session, playlist.id, [_entry("present")], now=NOW)

    db_session.refresh(missing)
    db_session.refresh(target)
    assert stats.delisted_items == 1
    assert missing.membership == ItemMembership.DELISTED
    assert missing.delisted_at == NOW
    assert target.status == TargetStatus.DOWNLOADED
    assert db_session.scalar(select(func.count()).select_from(Artifact)) == 1


def test_reappearing_item_becomes_active_and_keeps_pending_target(db_session):
    playlist, _, assignments = _playlist(db_session, profiles=1)
    item = Item(
        playlist_id=playlist.id,
        source_id="one",
        source_url="https://example.com/watch/one",
        membership=ItemMembership.DELISTED,
        delisted_at=NOW - timedelta(days=1),
    )
    db_session.add(item)
    db_session.flush()
    target = Target(item_id=item.id, playlist_profile_id=assignments[0].id)
    db_session.add(target)
    db_session.commit()

    apply_discovery(db_session, playlist.id, [_entry("one")], now=NOW)

    db_session.refresh(item)
    db_session.refresh(target)
    assert item.membership == ItemMembership.ACTIVE
    assert item.delisted_at is None
    assert target.status == TargetStatus.PENDING


def test_dry_run_reports_diff_without_mutating_domain_records(db_session):
    playlist, _, _ = _playlist(db_session, profiles=1)
    old = Item(
        playlist_id=playlist.id,
        source_id="old",
        source_url="https://example.com/watch/old",
    )
    db_session.add(old)
    db_session.commit()

    stats = apply_discovery(db_session, playlist.id, [_entry("new")], dry_run=True, now=NOW)

    assert stats.new_items == 1
    assert stats.delisted_items == 1
    assert db_session.scalar(select(func.count()).select_from(Item)) == 1
    assert db_session.scalar(select(func.count()).select_from(Target)) == 0
    db_session.refresh(old)
    db_session.refresh(playlist)
    assert old.membership == ItemMembership.ACTIVE
    assert playlist.last_discover_at is None
