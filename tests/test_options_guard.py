from __future__ import annotations

import pytest

from sluicery.core.options import (
    OptionValidationError,
    guard_freeform,
    parse_managed_options,
)


@pytest.mark.parametrize(
    ("text", "canonical"),
    [
        ("--output value", "--output"),
        ("--output=value", "--output"),
        ("-o value", "--output"),
        ("-ovalue", "--output"),
        ("-P /tmp", "--paths"),
        ("-O after_move:x", "--print"),
        ("-j", "--dump-json"),
        ("-q", "--quiet"),
        ("-s", "--simulate"),
    ],
)
def test_parser_resolves_aliases_and_value_forms(text: str, canonical: str) -> None:
    import shlex

    occurrences = parse_managed_options(shlex.split(text))
    assert occurrences[0].canonical == canonical


@pytest.mark.parametrize(
    "option",
    [
        "--paths x",
        "--print x",
        "--print-to-file x y",
        "--progress-template x",
        "--load-info-json x",
        "--dump-json",
        "--dump-single-json",
    ],
)
def test_reserved_options_rejected_without_expert_mode(option: str) -> None:
    with pytest.raises(OptionValidationError, match="予約引数"):
        guard_freeform(option, source_label="Profile")


def test_expert_mode_allows_reserved_and_records_override() -> None:
    guarded = guard_freeform(
        "--paths=/tmp --print x",
        source_label="Profile",
        expert_mode=True,
    )
    assert guarded.reserved_overrides == frozenset({"--paths", "--print"})
    assert len(guarded.warnings) == 2


@pytest.mark.parametrize("expert_mode", [False, True])
def test_output_always_uses_custom_template_path(expert_mode: bool) -> None:
    with pytest.raises(OptionValidationError, match="layout_strategy=custom"):
        guard_freeform("-o out.%(ext)s", source_label="Profile", expert_mode=expert_mode)


@pytest.mark.parametrize("expert_mode", [False, True])
def test_download_archive_is_always_rejected(expert_mode: bool) -> None:
    with pytest.raises(OptionValidationError, match="唯一の状態管理源"):
        guard_freeform(
            "--download-archive archive.txt",
            source_label="Profile",
            expert_mode=expert_mode,
        )


@pytest.mark.parametrize(
    ("env_allow", "profile_allow", "accepted"),
    [(False, False, False), (False, True, False), (True, False, False), (True, True, True)],
)
def test_exec_requires_both_gates(env_allow: bool, profile_allow: bool, accepted: bool) -> None:
    if not accepted:
        with pytest.raises(OptionValidationError, match="両方が必要"):
            guard_freeform(
                "--exec echo-ok",
                source_label="Profile",
                expert_mode=True,
                env_allow_exec=env_allow,
                profile_allow_exec=profile_allow,
            )
        return
    guarded = guard_freeform(
        "--exec echo-ok",
        source_label="Profile",
        env_allow_exec=env_allow,
        profile_allow_exec=profile_allow,
    )
    assert guarded.reserved_overrides == frozenset({"--exec"})
    assert guarded.warnings


@pytest.mark.parametrize(
    "option",
    [
        "--quiet",
        "--no-progress",
        "--simulate",
        "--restrict-filenames",
        "--no-windows-filenames",
        "--no-newline",
    ],
)
def test_warning_options_are_allowed_with_warning(option: str) -> None:
    guarded = guard_freeform(option, source_label="Playlist")
    assert guarded.tokens
    assert guarded.warnings


def test_simulate_is_not_warned_for_discover() -> None:
    guarded = guard_freeform(
        "--simulate", source_label="Playlist", command_kind="discover"
    )
    assert guarded.warnings == ()


def test_unbalanced_quotes_are_rejected() -> None:
    with pytest.raises(OptionValidationError, match="引用符"):
        guard_freeform("--format 'broken", source_label="Profile")


@pytest.mark.parametrize("option", ["--cookies", "--cookies-from-browser"])
def test_cookie_options_are_rejected_even_in_expert_mode(option: str) -> None:
    with pytest.raises(OptionValidationError, match="Cookie|cookies"):
        guard_freeform(
            f"{option} value",
            source_label="test",
            expert_mode=True,
            env_allow_exec=True,
            profile_allow_exec=True,
        )
