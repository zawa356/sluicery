from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from sluicery.config import Settings
from sluicery.db.models import (
    Item,
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
from sluicery.web.app import create_app
from sluicery.web.auth import ensure_initial_user


def _csrf(response) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def _client(base_env, session_factory) -> tuple[TestClient, Settings]:
    settings = Settings()
    settings.ADMIN_PASSWORD = "correct-password"
    ensure_initial_user(session_factory, settings)
    client = TestClient(create_app(settings=settings, session_factory=session_factory))
    csrf = _csrf(client.get("/login"))
    response = client.post(
        "/login",
        data={
            "csrf_token": csrf,
            "username": settings.ADMIN_USERNAME,
            "password": "correct-password",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client, settings


def _playlist_form(csrf: str, **overrides: str) -> dict[str, str]:
    values = {
        "csrf_token": csrf,
        "name": "Web Playlist",
        "folder_name": "web-playlist",
        "url": "https://example.com/playlist",
        "kind_hint": "video",
        "ytdlp_args": "",
        "enabled": "yes",
    }
    values.update(overrides)
    return values


def test_playlist_create_edit_and_list(base_env, session_factory) -> None:
    client, _settings = _client(base_env, session_factory)
    csrf = _csrf(client.get("/playlists/new"))

    created = client.post(
        "/playlists/new",
        data=_playlist_form(csrf),
        follow_redirects=False,
    )

    assert created.status_code == 303
    detail_url = created.headers["location"]
    assert client.get("/playlists").text.count("Web Playlist") == 1
    edit_page = client.get(f"{detail_url}/edit")
    updated = client.post(
        f"{detail_url}/edit",
        data=_playlist_form(
            _csrf(edit_page),
            name="Renamed Playlist",
            paused="yes",
            discover_cron="5 */2 * * *",
            download_cron="15 */4 * * *",
        ),
        follow_redirects=False,
    )
    assert updated.status_code == 303
    detail = client.get(detail_url)
    assert "Renamed Playlist" in detail.text
    with session_factory() as db:
        playlist = db.scalar(select(Playlist))
        assert playlist is not None
        assert playlist.paused is True
        assert playlist.url == "https://example.com/playlist"
        assert playlist.discover_cron == "5 */2 * * *"
        assert playlist.download_cron == "15 */4 * * *"


def test_playlist_form_rejects_cookie_argument(base_env, session_factory) -> None:
    client, _settings = _client(base_env, session_factory)
    csrf = _csrf(client.get("/playlists/new"))

    response = client.post(
        "/playlists/new",
        data=_playlist_form(csrf, ytdlp_args="--cookies /tmp/not-allowed"),
    )

    assert response.status_code == 422
    assert "PlaylistのCookie設定" in response.text
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Playlist)) == 0


def test_playlist_form_rejects_invalid_cron(base_env, session_factory) -> None:
    client, _settings = _client(base_env, session_factory)
    csrf = _csrf(client.get("/playlists/new"))

    response = client.post(
        "/playlists/new",
        data=_playlist_form(csrf, discover_cron="not-a-cron"),
    )

    assert response.status_code == 422
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Playlist)) == 0


def test_playlist_detail_paginates_filters_and_updates_target(
    base_env, session_factory
) -> None:
    client, _settings = _client(base_env, session_factory)
    with session_factory() as db:
        playlist = Playlist(
            name="Many",
            folder_name="many",
            url="https://example.com/many",
            kind_hint=PlaylistKindHint.VIDEO,
        )
        profile = Profile(
            name="Video",
            kind=ProfileKind.VIDEO,
            layout_strategy=LayoutStrategy.FLAT,
        )
        storage = Storage(name="Local", kind=StorageKind.LOCAL, config_json={"path": "out"})
        db.add_all([playlist, profile, storage])
        db.flush()
        assignment = PlaylistProfile(
            playlist_id=playlist.id,
            profile_id=profile.id,
            storage_id=storage.id,
            subpath="many",
        )
        db.add(assignment)
        db.flush()
        for index in range(51):
            item = Item(
                playlist_id=playlist.id,
                source_id=f"item-{index:02d}",
                source_url=f"https://example.com/item/{index}",
                title=f"Title {index:02d}",
                playlist_index=index,
            )
            db.add(item)
            db.flush()
            db.add(
                Target(
                    item_id=item.id,
                    playlist_profile_id=assignment.id,
                    status=TargetStatus.FAILED if index == 50 else TargetStatus.PENDING,
                )
            )
        db.commit()
        playlist_id = playlist.id
    first = client.get(f"/playlists/{playlist_id}")
    second = client.get(f"/playlists/{playlist_id}?page=2")
    filtered = client.get(
        f"/playlists/{playlist_id}?status_filter=failed&q=Title%2050"
    )

    assert "1 / 2" in first.text
    assert "Title 00" in first.text and "Title 50" not in first.text
    assert "Title 50" in second.text
    assert "Title 50" in filtered.text and "failed" in filtered.text
    csrf = _csrf(filtered)
    with session_factory() as db:
        failed_target = db.scalar(select(Target).where(Target.status == TargetStatus.FAILED))
        assert failed_target is not None
        target_id = failed_target.id
    retried = client.post(
        f"/playlists/{playlist_id}/targets/{target_id}/action",
        data={"csrf_token": csrf, "action": "retry"},
        follow_redirects=False,
    )
    assert retried.status_code == 303
    with session_factory() as db:
        assert db.get(Target, target_id).status == TargetStatus.PENDING


def test_playlist_delete_modes_never_delete_media(
    base_env, session_factory, tmp_path: Path
) -> None:
    client, _settings = _client(base_env, session_factory)
    marker = tmp_path / "existing-media.mkv"
    marker.write_bytes(b"keep")
    with session_factory() as db:
        keep = Playlist(
            name="Keep",
            folder_name="keep",
            url="https://example.com/keep",
            kind_hint=PlaylistKindHint.VIDEO,
        )
        delete_row = Playlist(
            name="Delete",
            folder_name="delete",
            url="https://example.com/delete",
            kind_hint=PlaylistKindHint.VIDEO,
        )
        db.add_all([keep, delete_row])
        db.flush()
        db.add(
            Item(
                playlist_id=delete_row.id,
                source_id="source",
                source_url="https://example.com/source",
            )
        )
        db.commit()
        keep_id, delete_id = keep.id, delete_row.id
    csrf = _csrf(client.get(f"/playlists/{keep_id}"))

    kept = client.post(
        f"/playlists/{keep_id}/delete",
        data={"csrf_token": csrf, "mode": "keep_items"},
        follow_redirects=False,
    )
    deleted = client.post(
        f"/playlists/{delete_id}/delete",
        data={"csrf_token": csrf, "mode": "delete_items"},
        follow_redirects=False,
    )

    assert kept.status_code == deleted.status_code == 303
    with session_factory() as db:
        keep = db.get(Playlist, keep_id)
        assert keep is not None and not keep.enabled and keep.paused
        assert db.get(Playlist, delete_id) is None
        assert db.scalar(select(func.count()).select_from(Item)) == 0
    assert marker.read_bytes() == b"keep"
