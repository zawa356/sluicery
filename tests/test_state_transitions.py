from __future__ import annotations

import pytest

from sluicery.core.target_state import (
    InvalidStateTransition,
    advance_target,
    transition_item,
    transition_target,
)
from sluicery.db.models import (
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


def _item_and_target(db_session, *, status: TargetStatus = TargetStatus.PENDING):
    storage = Storage(name="s", kind=StorageKind.LOCAL, config_json={"path": "out"})
    profile = Profile(name="p", kind=ProfileKind.VIDEO, layout_strategy=LayoutStrategy.FLAT)
    playlist = Playlist(
        name="p", folder_name="p", url="https://example.com", kind_hint=PlaylistKindHint.VIDEO
    )
    db_session.add_all([storage, profile, playlist])
    db_session.flush()
    assignment = PlaylistProfile(
        playlist_id=playlist.id, profile_id=profile.id, storage_id=storage.id
    )
    item = Item(playlist_id=playlist.id, source_id="one", source_url="https://example.com/one")
    db_session.add_all([assignment, item])
    db_session.flush()
    target = Target(item_id=item.id, playlist_profile_id=assignment.id, status=status)
    db_session.add(target)
    db_session.commit()
    return item, target


def test_target_normal_transitions_are_applied_by_cas(db_session):
    _, target = _item_and_target(db_session)

    transition_target(db_session, target.id, TargetStatus.QUEUED)
    transition_target(db_session, target.id, TargetStatus.DOWNLOADING)
    transition_target(db_session, target.id, TargetStatus.PROCESSING)
    transition_target(db_session, target.id, TargetStatus.DOWNLOADED)

    db_session.refresh(target)
    assert target.status == TargetStatus.DOWNLOADED


def test_target_invalid_transition_is_rejected_without_update(db_session):
    _, target = _item_and_target(db_session)

    with pytest.raises(InvalidStateTransition, match="pending -> downloaded"):
        transition_target(db_session, target.id, TargetStatus.DOWNLOADED)

    db_session.refresh(target)
    assert target.status == TargetStatus.PENDING


def test_target_any_state_can_become_blocked(db_session):
    _, target = _item_and_target(db_session, status=TargetStatus.DOWNLOADED)

    transition_target(
        db_session,
        target.id,
        TargetStatus.BLOCKED,
        error="storage unavailable",
        blocked_reason="storage unavailable",
    )

    db_session.refresh(target)
    assert target.status == TargetStatus.BLOCKED
    assert target.blocked_reason == "storage unavailable"


def test_item_membership_only_toggles_between_active_and_delisted(db_session):
    item, _ = _item_and_target(db_session)

    transition_item(db_session, item.id, ItemMembership.DELISTED)
    db_session.refresh(item)
    assert item.membership == ItemMembership.DELISTED
    assert item.delisted_at is not None

    transition_item(db_session, item.id, ItemMembership.ACTIVE)
    db_session.refresh(item)
    assert item.membership == ItemMembership.ACTIVE
    assert item.delisted_at is None

    with pytest.raises(InvalidStateTransition, match="active -> active"):
        transition_item(db_session, item.id, ItemMembership.ACTIVE)


@pytest.mark.parametrize("start", [TargetStatus.FAILED, TargetStatus.BLOCKED])
def test_advance_target_resumes_through_every_normal_state(db_session, start):
    _, target = _item_and_target(db_session, status=start)

    advance_target(db_session, target.id, TargetStatus.PROCESSING)

    db_session.refresh(target)
    assert target.status == TargetStatus.PROCESSING


def test_advance_target_rejects_backward_execution_stage(db_session):
    _, target = _item_and_target(db_session, status=TargetStatus.PROCESSING)

    with pytest.raises(InvalidStateTransition, match="processing -> downloading"):
        advance_target(db_session, target.id, TargetStatus.DOWNLOADING)
