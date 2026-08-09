from __future__ import annotations

import unicodedata

import pytest

from sluicery.core.naming import (
    NamingValidationError,
    sanitize_component,
    sanitize_relative_path,
)
from sluicery.layout import LayoutContext, LayoutValidationError, resolve_layout, resolve_subpath


def _ctx(**overrides: str | None) -> LayoutContext:
    values: dict[str, str | None] = {
        "playlist_name": "お気に入り",
        "playlist_folder_name": "動画:一覧",
        "profile_name": "1080p",
        "profile_kind": "video",
        "subpath": "{playlist.folder_name}/{profile.name}",
        "custom_output_template": None,
    }
    values.update(overrides)
    return LayoutContext(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["CON", "con.txt", "NUL", "COM1", "lpt9.log"])
def test_windows_reserved_name_is_prefixed(name: str) -> None:
    assert sanitize_component(name).startswith("_")


def test_component_replaces_forbidden_and_removes_control_and_trailing() -> None:
    assert sanitize_component('a<b>:c*?"|\x00. ') == "a_b__c____"


def test_component_and_subpath_are_normalized_to_nfc() -> None:
    nfd = unicodedata.normalize("NFD", "動画")
    assert sanitize_component(nfd) == "動画"
    assert sanitize_relative_path(f"親/{nfd}") == "親/動画"


@pytest.mark.parametrize("path", ["../outside", "a/../b", "/absolute", "C:/absolute", "a//b"])
def test_relative_path_rejects_traversal_and_absolute(path: str) -> None:
    with pytest.raises(NamingValidationError):
        sanitize_relative_path(path)


def test_subpath_expands_documented_variables_and_sanitizes() -> None:
    assert resolve_subpath("{playlist.folder_name}/{profile.kind}/{playlist.name}", _ctx()) == (
        "動画_一覧/video/お気に入り"
    )


def test_unknown_subpath_variable_is_rejected() -> None:
    with pytest.raises(LayoutValidationError, match="展開できません"):
        resolve_subpath("{playlist.unknown}", _ctx())


def test_flat_layout_uses_empty_fallback_instead_of_na() -> None:
    resolved = resolve_layout("flat", _ctx(subpath="{playlist.folder_name}"))
    assert "|)" in resolved.output_template
    assert "NA" not in resolved.output_template
    assert ".120B" in resolved.output_template
    assert resolved.relative_output_template.startswith("動画_一覧/")


@pytest.mark.parametrize(
    "template",
    [
        "%(title)s.%(ext)s",
        "/absolute/%(title)s [%(id)s].%(ext)s",
        "../escape/%(title)s [%(id)s].%(ext)s",
    ],
)
def test_custom_rejects_missing_suffix_absolute_and_traversal(template: str) -> None:
    with pytest.raises(LayoutValidationError):
        resolve_layout(
            "custom",
            _ctx(custom_output_template=template, subpath="{playlist.folder_name}"),
        )


def test_custom_accepts_required_suffix_and_warns_for_nested_path() -> None:
    resolved = resolve_layout(
        "custom",
        _ctx(
            custom_output_template="season/%(title)s [%(id)s].%(ext)s",
            subpath="{playlist.folder_name}",
        ),
    )
    assert resolved.output_template.endswith("[%(id)s].%(ext)s")
    assert resolved.warnings


def test_flat_rejects_other_kind() -> None:
    with pytest.raises(LayoutValidationError, match="video / music"):
        resolve_layout("flat", _ctx(profile_kind="other"))
