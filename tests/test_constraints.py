from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from sluicery.db.models import Item, Playlist, PlaylistKindHint, PlaylistProfile, Profile, Target
from sluicery.db.repositories.user import DuplicateUserError, UserRepository


@pytest.fixture
def playlist(db_session) -> Playlist:
    p = Playlist(name="p1", folder_name="p1", url="http://x", kind_hint=PlaylistKindHint.VIDEO)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


def test_item_unique_playlist_id_source_id(db_session, playlist: Playlist) -> None:
    db_session.add(Item(playlist_id=playlist.id, source_id="abc", source_url="http://x/abc"))
    db_session.commit()

    db_session.add(Item(playlist_id=playlist.id, source_id="abc", source_url="http://x/abc2"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_target_unique_item_id_playlist_profile_id(db_session, playlist: Playlist) -> None:
    from sluicery.db.models import ProfileKind, Storage, StorageKind

    storage = Storage(name="s1", kind=StorageKind.LOCAL)
    profile = Profile(name="pr1", kind=ProfileKind.VIDEO)
    db_session.add_all([storage, profile])
    db_session.commit()

    pp = PlaylistProfile(playlist_id=playlist.id, profile_id=profile.id, storage_id=storage.id)
    item = Item(playlist_id=playlist.id, source_id="abc", source_url="http://x/abc")
    db_session.add_all([pp, item])
    db_session.commit()

    db_session.add(Target(item_id=item.id, playlist_profile_id=pp.id))
    db_session.commit()

    db_session.add(Target(item_id=item.id, playlist_profile_id=pp.id))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_second_user_creation_is_rejected(db_session) -> None:
    repo = UserRepository(db_session)
    repo.create_single(username="admin", password_hash="x")
    with pytest.raises(DuplicateUserError):
        repo.create_single(username="admin2", password_hash="y")
