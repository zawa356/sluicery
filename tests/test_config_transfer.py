from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from sluicery.core import settings as core_settings
from sluicery.core.config_transfer import (
    ConfigImportConfirmationError,
    ConfigImportSigner,
    ConfigTransferError,
    apply_config_import,
    dump_config_yaml,
    export_config,
    load_config_yaml,
    preview_config_import,
)
from sluicery.db.models import (
    Item,
    LayoutStrategy,
    MissingPolicy,
    Playlist,
    PlaylistKindHint,
    PlaylistProfile,
    Profile,
    ProfileKind,
    Setting,
    Storage,
    StorageKind,
    metadata_obj,
)
from sluicery.db.session import create_engine_for, create_session_factory


def _configuration_graph(db_session):
    storage = Storage(
        name="remote-media",
        kind=StorageKind.REMOTE,
        enabled=True,
        config_json={
            "protocol": "smb",
            "host": "nas.invalid",
            "share": "media",
            "token": "config-token-secret",
            "api_key": "api-key-secret",
            "access_key": "access-key-secret",
            "private_key": "private-key-secret",
            "auth_header": "auth-header-secret",
        },
        credentials_encrypted={
            "user": "secret-user",
            "password": "credential-secret",
            "domain": "secret-domain",
        },
    )
    profile = Profile(
        name="video",
        description="main profile",
        kind=ProfileKind.VIDEO,
        layout_strategy=LayoutStrategy.FLAT,
        format_selector="best",
        ytdlp_args="--video-password profile-argument-secret",
    )
    playlist = Playlist(
        name="source-list",
        folder_name="source-list",
        url="https://example.com/list?token=playlist-url-secret",
        enabled=True,
        paused=False,
        kind_hint=PlaylistKindHint.VIDEO,
        missing_policy=MissingPolicy.LEAVE,
        retention_policy_json={"enabled": True, "keep_latest": 10},
        cookie_enabled=True,
        cookies_encrypted={"netscape": "cookie-value-secret"},
    )
    db_session.add_all([storage, profile, playlist])
    db_session.flush()
    assignment = PlaylistProfile(
        playlist_id=playlist.id,
        profile_id=profile.id,
        storage_id=storage.id,
        subpath="{playlist.folder_name}",
    )
    item = Item(
        playlist_id=playlist.id,
        source_id="private-item",
        source_url="https://example.com/private-item?secret=item-secret",
    )
    db_session.add_all([assignment, item])
    core_settings.set_override(db_session, "download.retries", 7)
    core_settings.set_override(
        db_session,
        "ytdlp.smoketest_url",
        "https://example.com/video?token=smoketest-url-secret",
    )
    db_session.add(Setting(key="_internal.secret", value_json='"internal-secret"'))
    db_session.commit()
    return storage, profile, playlist


def test_export_yaml_excludes_all_secret_and_execution_state(db_session) -> None:
    _configuration_graph(db_session)

    exported = dump_config_yaml(export_config(db_session))

    for secret in (
        "config-token-secret",
        "api-key-secret",
        "access-key-secret",
        "private-key-secret",
        "auth-header-secret",
        "secret-user",
        "credential-secret",
        "secret-domain",
        "cookie-value-secret",
        "profile-argument-secret",
        "playlist-url-secret",
        "item-secret",
        "internal-secret",
        "smoketest-url-secret",
    ):
        assert secret not in exported
    assert "requires_credentials: true" in exported
    assert "requires_cookie_reentry: true" in exported
    assert "requires_secret_reentry: true" in exported
    assert "requires_url_reentry: true" in exported
    assert "download.retries: 7" in exported
    assert "_internal" not in exported
    assert "private-item" not in exported


def test_import_creates_portable_config_disabled_until_secrets_are_reentered(
    db_session, tmp_path: Path
) -> None:
    _configuration_graph(db_session)
    document = load_config_yaml(dump_config_yaml(export_config(db_session)))
    marker = tmp_path / "media-marker"
    marker.write_text("must remain", encoding="utf-8")

    target_engine = create_engine_for(tmp_path / "import-target.db")
    metadata_obj.create_all(target_engine)
    target_factory = create_session_factory(target_engine)
    try:
        with target_factory() as target:
            plan = preview_config_import(target, document, "overwrite")
            result = apply_config_import(target, document, plan)
            assert result.created == 5
            storage = target.scalar(select(Storage))
            playlist = target.scalar(select(Playlist))
            assert storage is not None and storage.enabled is False
            assert storage.credentials_encrypted is None
            assert storage.config_json is not None
            assert "token" not in storage.config_json
            assert playlist is not None and playlist.paused is True
            assert playlist.cookie_enabled is False
            assert playlist.cookies_encrypted is None
            assert playlist.retention_policy_json is not None
            assert playlist.retention_policy_json["enabled"] is False
            assert target.scalar(select(func.count()).select_from(Item)) == 0
            assert core_settings.get(target, "download.retries") == 7
    finally:
        target_engine.dispose()
    assert marker.read_text(encoding="utf-8") == "must remain"


@pytest.mark.parametrize(
    ("mode", "expected_names", "expected_description"),
    [
        ("skip", ["video"], "old"),
        ("overwrite", ["video"], "imported"),
        ("create", ["video", "video"], "old"),
    ],
)
def test_collision_modes_are_previewed_and_applied(
    db_session, mode, expected_names, expected_description
) -> None:
    existing = Profile(
        name="video",
        description="old",
        kind=ProfileKind.VIDEO,
        layout_strategy=LayoutStrategy.FLAT,
    )
    db_session.add(existing)
    db_session.commit()
    document = load_config_yaml(
        """
version: 1
storages: []
profiles:
  - ref: profile-1
    name: video
    description: imported
    kind: video
    layout_strategy: flat
    expert_mode: false
    allow_exec: false
playlists: []
playlist_profiles: []
settings: {}
"""
    )

    plan = preview_config_import(db_session, document, mode)
    assert plan.operations[0].action == (
        "skip" if mode == "skip" else "overwrite" if mode == "overwrite" else "create"
    )
    apply_config_import(db_session, document, plan)

    profiles = list(db_session.scalars(select(Profile).order_by(Profile.id)))
    assert [row.name for row in profiles] == expected_names
    assert profiles[0].description == expected_description


def test_confirmation_rejects_database_change(db_session, secret_key) -> None:
    _configuration_graph(db_session)
    document = export_config(db_session)
    plan = preview_config_import(db_session, document, "overwrite")
    signer = ConfigImportSigner(secret_key)
    token = signer.issue(document, plan)
    loaded_document, loaded_plan = signer.load(token)
    storage = db_session.scalar(select(Storage))
    assert storage is not None
    storage.enabled = False
    db_session.commit()

    with pytest.raises(ConfigImportConfirmationError, match="DB状態が変化"):
        apply_config_import(db_session, loaded_document, loaded_plan)


def test_import_rejects_unknown_or_secret_fields() -> None:
    with pytest.raises(ConfigTransferError):
        load_config_yaml(
            """
version: 1
storages:
  - ref: s1
    name: media
    kind: local
    enabled: true
    password: leaked
profiles: []
playlists: []
playlist_profiles: []
settings: {}
"""
        )

    with pytest.raises(ConfigTransferError, match="alias / anchor"):
        load_config_yaml("version: 1\nstorages: &rows []\nprofiles: *rows\n")

    with pytest.raises(ConfigTransferError):
        load_config_yaml(
            """
version: 1
storages:
  - ref: s1
    name: media
    kind: remote
    enabled: true
    config: {protocol: smb, host: host, share: media, api_key: leaked}
profiles: []
playlists: []
playlist_profiles: []
settings: {}
"""
        )

    with pytest.raises(ConfigTransferError):
        load_config_yaml(
            """
version: 1
storages: []
profiles:
  - ref: p1
    name: unsafe
    kind: video
    layout_strategy: flat
    expert_mode: true
    allow_exec: false
    ytdlp_args: "--extractor-args youtube:po_token=LEAK"
playlists: []
playlist_profiles: []
settings: {}
"""
        )


def test_overwrite_clears_existing_secrets_and_requires_reenable(db_session) -> None:
    storage, profile, playlist = _configuration_graph(db_session)
    document = export_config(db_session)
    # import文書側のmarkerを偽装しても既存secretを再利用できない。
    document.storages[0].requires_credentials = False
    document.playlists[0].requires_cookie_reentry = False
    plan = preview_config_import(db_session, document, "overwrite")

    apply_config_import(db_session, document, plan)

    db_session.refresh(storage)
    db_session.refresh(profile)
    db_session.refresh(playlist)
    assert storage.enabled is False
    assert storage.credentials_encrypted is None
    assert profile.ytdlp_args is None
    assert playlist.paused is True
    assert playlist.cookie_enabled is False
    assert playlist.cookies_encrypted is None
    assert playlist.retention_policy_json is not None
    assert playlist.retention_policy_json["enabled"] is False
