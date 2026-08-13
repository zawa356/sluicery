from __future__ import annotations

from pathlib import Path

import pytest

from sluicery.core.options import (
    OptionOverrides,
    OptionValidationError,
    build_discover_args,
    build_download_args,
    guard_raw_exec_args,
)
from sluicery.db.models import (
    LayoutStrategy,
    Playlist,
    PlaylistKindHint,
    PlaylistProfile,
    Profile,
    ProfileKind,
)


def _records(
    *,
    profile_args: str | None = None,
    playlist_args: str | None = None,
    expert_mode: bool = False,
    allow_exec: bool = False,
) -> tuple[Playlist, Profile, PlaylistProfile]:
    playlist = Playlist(
        id=10,
        name="一覧",
        folder_name="動画",
        url="https://example.com/playlist",
        kind_hint=PlaylistKindHint.VIDEO,
        ytdlp_args=playlist_args,
    )
    profile = Profile(
        id=20,
        name="1080p",
        kind=ProfileKind.VIDEO,
        layout_strategy=LayoutStrategy.FLAT,
        ytdlp_args=profile_args,
        expert_mode=expert_mode,
        allow_exec=allow_exec,
        postprocess_chain_json={},
    )
    association = PlaylistProfile(
        id=30,
        playlist_id=playlist.id,
        profile_id=profile.id,
        storage_id=1,
        subpath="{playlist.folder_name}/{profile.name}",
    )
    return playlist, profile, association


def _download(db_session, *, overrides: OptionOverrides | None = None, **record_kwargs):
    playlist, profile, association = _records(**record_kwargs)
    return build_download_args(
        None,
        source_url="https://example.com/item",
        session=db_session,
        staging_dir=Path("/data/staging"),
        work_id="work-1",
        playlist=playlist,
        profile=profile,
        playlist_profile=association,
        overrides=overrides,
    )


def _option_value(args: tuple[str, ...], option: str) -> str:
    index = len(args) - 1 - args[::-1].index(option)
    return args[index + 1]


def test_structured_layers_inherit_and_override_in_l2_l3_l4_l6_order(db_session) -> None:
    playlist, profile, association = _records()
    profile.format_selector = "profile-format"
    profile.embed_metadata = None
    profile.embed_thumbnail = False
    profile.concurrent_fragments = None

    command = build_download_args(
        None,
        source_url="https://example.com/item",
        session=db_session,
        staging_dir=Path("/data/staging"),
        work_id="work-1",
        playlist=playlist,
        profile=profile,
        playlist_profile=association,
        overrides=OptionOverrides(structured={"format_selector": "override-format"}),
    )

    assert _option_value(command.args, "--format") == "override-format"
    assert "--embed-metadata" in command.args  # L4 None なので L3 true を継承
    assert "--no-embed-thumbnail" in command.args
    assert _option_value(command.args, "--concurrent-fragments") == "3"
    format_origin = next(origin for origin in command.origins if "--format" in origin.arguments)
    assert (format_origin.layer, format_origin.field) == ("L6", "format_selector")


def test_explicit_false_disables_inherited_audio_extract(db_session) -> None:
    playlist, profile, association = _records()
    profile.kind = ProfileKind.MUSIC
    profile.audio_extract = False
    command = build_download_args(
        None,
        source_url="https://example.com/item",
        session=db_session,
        staging_dir=Path("/data/staging"),
        work_id="work-1",
        playlist=playlist,
        profile=profile,
        playlist_profile=association,
    )
    assert "--extract-audio" not in command.args


def test_freeform_layers_are_concatenated_in_order(db_session) -> None:
    command = _download(
        db_session,
        profile_args="--format profile",
        playlist_args="--format playlist",
        overrides=OptionOverrides(ytdlp_args="--format temporary"),
    )
    positions = [
        command.args.index(value) for value in ("profile", "playlist", "temporary")
    ]
    assert positions == sorted(positions)
    assert _option_value(command.args, "--format") == "temporary"


def test_download_always_enables_partial_file_resume(db_session) -> None:
    command = _download(db_session, profile_args="--no-continue")

    assert command.args.index("--continue") > command.args.index("--no-continue")
    origin = next(origin for origin in command.origins if "--continue" in origin.arguments)
    assert origin.layer == "L1"


def test_download_emits_structured_after_move_result(db_session) -> None:
    command = _download(db_session)

    templates = [
        command.args[index + 1]
        for index, value in enumerate(command.args[:-1])
        if value == "--print"
    ]
    assert any("SLUICERY_RESULT" in value and "%(format_id)j" in value for value in templates)


def test_download_contains_staging_layout_protocol_and_progress_fix(db_session) -> None:
    command = _download(db_session)
    assert _option_value(command.args, "--paths") == "temp:/data/staging/work-1/.tmp"
    assert "home:/data/staging/work-1" in command.args
    assert _option_value(command.args, "--output").startswith("動画/1080p/")
    assert "--progress-template" in command.args
    assert "--print" in command.args
    assert "--progress" in command.args  # --print が暗黙に付ける quiet を上書き
    assert "--windows-filenames" in command.args
    assert "--download-archive" not in command.args
    assert command.args[-2:] == ("--", "https://example.com/item")
    assert command.resolved_output_path == Path(
        "/data/staging/work-1/動画/1080p/"
        "%(upload_date>%Y-%m-%d&{} |)s%(title).120B [%(id)s].%(ext)s"
    )


def test_expert_reserved_override_suppresses_matching_l1_argument(db_session) -> None:
    command = _download(
        db_session,
        profile_args="--paths home:/expert --print custom",
        expert_mode=True,
    )
    assert command.args.count("--paths") == 1
    assert _option_value(command.args, "--paths") == "home:/expert"
    assert command.args.count("--print") == 1
    assert command.warnings


def test_exec_gate_is_applied_during_composition(db_session) -> None:
    with pytest.raises(OptionValidationError, match="両方が必要"):
        _download(
            db_session,
            profile_args="--exec echo-ok",
            expert_mode=True,
            allow_exec=True,
        )


def test_discover_has_identity_protocol_without_layout_arguments(db_session) -> None:
    playlist, profile, _association = _records()
    command = build_discover_args(playlist, session=db_session, profile=profile)
    assert "--flat-playlist" in command.args
    assert "--simulate" in command.args
    assert "--print" in command.args
    assert "--output" not in command.args
    assert "--paths" not in command.args
    assert "--progress-template" not in command.args
    assert command.resolved_output_path is None
    assert command.timeout.idle_sec == 300
    assert command.args[-2:] == ("--", "https://example.com/playlist")


@pytest.mark.parametrize("source_url", ["--exec", "ftp://example.com/file", "not-a-url"])
def test_source_url_must_be_complete_http_url(db_session, source_url: str) -> None:
    playlist, profile, association = _records()
    with pytest.raises(OptionValidationError, match="URL"):
        build_download_args(
            None,
            source_url=source_url,
            session=db_session,
            staging_dir=Path("/data/staging"),
            work_id="work-1",
            playlist=playlist,
            profile=profile,
            playlist_profile=association,
        )


@pytest.mark.parametrize("option", ["--exec", "--exec-before-download", "-oresult"])
def test_raw_exec_rejects_reserved_options(option: str) -> None:
    with pytest.raises(OptionValidationError, match="ytdlp exec"):
        guard_raw_exec_args([option, "value"])


def test_unknown_temporary_structured_field_is_rejected(db_session) -> None:
    with pytest.raises(OptionValidationError, match="未対応"):
        _download(
            db_session,
            overrides=OptionOverrides(structured={"unknown": "value"}),
        )
