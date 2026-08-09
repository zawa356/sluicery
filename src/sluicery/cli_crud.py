"""Phase 9 の Web UI まで使用する暫定 CRUD / preview CLI。

入力検証はこの CLI 層で行い、リポジトリ層には状態遷移を持ち込まない。
レコード削除・関連解除はいかなる実ファイル操作も呼び出さない。
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from sluicery.core.naming import NamingValidationError, sanitize_component
from sluicery.core.options import (
    OptionOverrides,
    OptionValidationError,
    build_discover_args,
    build_download_args,
    guard_freeform,
    parse_managed_options,
)
from sluicery.db.models import (
    Artifact,
    Item,
    LayoutStrategy,
    Playlist,
    PlaylistKindHint,
    PlaylistProfile,
    Profile,
    ProfileKind,
    Run,
    Storage,
    StorageKind,
    Target,
)
from sluicery.db.repositories.playlist import PlaylistRepository
from sluicery.db.repositories.playlist_profile import PlaylistProfileRepository
from sluicery.db.repositories.profile import ProfileRepository
from sluicery.db.repositories.storage import StorageRepository
from sluicery.downloader.ytdlp import mask_command_line
from sluicery.layout import LayoutContext, LayoutValidationError, resolve_layout

OpenSession = Callable[[], Session]
LoadSettings = Callable[[], Any]
_MISSING = object()


class CliValidationError(ValueError):
    pass


def resolve_record[ModelT: (Storage, Profile, Playlist)](
    session: Session,
    model: type[ModelT],
    identifier: str,
    *,
    label: str,
) -> ModelT:
    """整数 ID または一意な名前でレコードを解決する。"""
    try:
        numeric_id = int(identifier)
    except ValueError:
        numeric_id = None
    if numeric_id is not None:
        record = session.get(model, numeric_id)
        if record is not None:
            return record

    rows = list(session.scalars(select(model).where(model.name == identifier)))
    if not rows:
        raise CliValidationError(f"{label} が見つかりません: {identifier}")
    if len(rows) > 1:
        raise CliValidationError(f"同名の {label} が複数あります。ID を指定してください")
    return rows[0]


def find_playlist_profile(
    session: Session, playlist_id: int, profile_id: int
) -> PlaylistProfile | None:
    stmt = select(PlaylistProfile).where(
        PlaylistProfile.playlist_id == playlist_id,
        PlaylistProfile.profile_id == profile_id,
    )
    return session.scalars(stmt).first()


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    rendered = [[str(value) for value in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in rendered:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rendered:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def _masked_json(value: Any) -> str:
    sensitive_fragments = ("password", "secret", "token", "cookie", "credential", "key")

    def mask(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: "********"
                if any(fragment in key.lower() for fragment in sensitive_fragments)
                else mask(nested)
                for key, nested in item.items()
            }
        if isinstance(item, list):
            return [mask(nested) for nested in item]
        return item

    return json.dumps(mask(value), ensure_ascii=False, sort_keys=True)


def _masked_args(value: str | None) -> str:
    if not value:
        return "(なし)"
    try:
        return shlex.join(mask_command_line(shlex.split(value)))
    except ValueError:
        return "(構文不正のため表示不可)"


def _print_detail(fields: Sequence[tuple[str, Any]]) -> None:
    for key, value in fields:
        print(f"{key}: {value}")


def _add_bool_pair(
    parser: argparse.ArgumentParser,
    dest: str,
    enabled: str,
    disabled: str,
    *,
    allow_inherit: bool = False,
) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(enabled, dest=dest, action="store_const", const=True)
    group.add_argument(disabled, dest=dest, action="store_const", const=False)
    if allow_inherit:
        group.add_argument(
            f"--inherit-{dest.replace('_', '-')}",
            dest=dest,
            action="store_const",
            const=None,
        )
    parser.set_defaults(**{dest: _MISSING})


def _add_profile_fields(parser: argparse.ArgumentParser, *, require_identity: bool) -> None:
    parser.add_argument("--name", required=require_identity)
    parser.add_argument(
        "--kind",
        choices=[kind.value for kind in ProfileKind],
        required=require_identity,
    )
    parser.add_argument("--description")
    parser.add_argument("--ytdlp-args")
    parser.add_argument("--format-selector")
    parser.add_argument("--container")
    parser.add_argument("--audio-format")
    parser.add_argument("--audio-quality")
    parser.add_argument("--subtitle-langs")
    parser.add_argument("--concurrent-fragments", type=int)
    parser.add_argument("--layout-strategy", choices=[item.value for item in LayoutStrategy])
    parser.add_argument("--output-template")
    _add_bool_pair(
        parser,
        "audio_extract",
        "--audio-extract",
        "--no-audio-extract",
        allow_inherit=True,
    )
    _add_bool_pair(
        parser,
        "embed_metadata",
        "--embed-metadata",
        "--no-embed-metadata",
        allow_inherit=True,
    )
    _add_bool_pair(
        parser,
        "embed_thumbnail",
        "--embed-thumbnail",
        "--no-embed-thumbnail",
        allow_inherit=True,
    )
    _add_bool_pair(
        parser,
        "embed_chapters",
        "--embed-chapters",
        "--no-embed-chapters",
        allow_inherit=True,
    )
    _add_bool_pair(
        parser,
        "subtitle_auto",
        "--subtitle-auto",
        "--no-subtitle-auto",
        allow_inherit=True,
    )
    _add_bool_pair(
        parser,
        "subtitle_embed",
        "--subtitle-embed",
        "--no-subtitle-embed",
        allow_inherit=True,
    )
    _add_bool_pair(parser, "expert_mode", "--expert-mode", "--no-expert-mode")
    _add_bool_pair(parser, "allow_exec", "--allow-exec", "--no-allow-exec")


def _add_enable_pair(parser: argparse.ArgumentParser) -> None:
    _add_bool_pair(parser, "enabled", "--enable", "--disable")


def configure_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    storage = sub.add_parser("storage", help="Storage レコードを管理する（Phase 5 まで疎通なし）")
    storage_sub = storage.add_subparsers(dest="storage_command", required=True)
    storage_add = storage_sub.add_parser("add")
    storage_add.add_argument("--name", required=True)
    storage_add.add_argument("--kind", choices=[item.value for item in StorageKind], required=True)
    storage_add.add_argument("--path", required=True)
    storage_sub.add_parser("list")
    storage_show = storage_sub.add_parser("show")
    storage_show.add_argument("storage")
    storage_edit = storage_sub.add_parser("edit")
    storage_edit.add_argument("storage")
    storage_edit.add_argument("--name")
    storage_edit.add_argument("--path")
    _add_enable_pair(storage_edit)
    storage_remove = storage_sub.add_parser("remove")
    storage_remove.add_argument("storage")

    profile = sub.add_parser("profile", help="Profile レコードを管理する（暫定 CLI）")
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)
    profile_add = profile_sub.add_parser("add")
    _add_profile_fields(profile_add, require_identity=True)
    profile_sub.add_parser("list")
    profile_show = profile_sub.add_parser("show")
    profile_show.add_argument("profile")
    profile_edit = profile_sub.add_parser("edit")
    profile_edit.add_argument("profile")
    _add_profile_fields(profile_edit, require_identity=False)
    profile_remove = profile_sub.add_parser("remove")
    profile_remove.add_argument("profile")

    playlist = sub.add_parser("playlist", help="Playlist と Profile 割当を管理する（暫定 CLI）")
    playlist_sub = playlist.add_subparsers(dest="playlist_command", required=True)
    playlist_add = playlist_sub.add_parser("add")
    playlist_add.add_argument("--name", required=True)
    playlist_add.add_argument("--folder-name", required=True)
    playlist_add.add_argument("--url", required=True)
    playlist_add.add_argument(
        "--kind-hint",
        choices=[item.value for item in PlaylistKindHint],
        default=PlaylistKindHint.VIDEO.value,
    )
    playlist_add.add_argument("--ytdlp-args")
    playlist_add.add_argument("--disable", action="store_true")
    playlist_add.add_argument("--paused", action="store_true")
    playlist_sub.add_parser("list")
    playlist_show = playlist_sub.add_parser("show")
    playlist_show.add_argument("playlist")
    playlist_edit = playlist_sub.add_parser("edit")
    playlist_edit.add_argument("playlist")
    playlist_edit.add_argument("--name")
    playlist_edit.add_argument("--folder-name")
    playlist_edit.add_argument("--url")
    playlist_edit.add_argument("--kind-hint", choices=[item.value for item in PlaylistKindHint])
    playlist_edit.add_argument("--ytdlp-args")
    _add_enable_pair(playlist_edit)
    _add_bool_pair(playlist_edit, "paused", "--pause", "--resume")
    playlist_remove = playlist_sub.add_parser("remove")
    playlist_remove.add_argument("playlist")
    remove_mode = playlist_remove.add_mutually_exclusive_group(required=True)
    remove_mode.add_argument(
        "--keep-items",
        action="store_true",
        help="Playlist を無効化・一時停止し、関連レコードとファイルを保持する",
    )
    remove_mode.add_argument(
        "--delete-items",
        action="store_true",
        help="Item 等のDBレコードも削除する（実ファイルは削除しない）",
    )
    attach = playlist_sub.add_parser("attach-profile")
    attach.add_argument("playlist")
    attach.add_argument("profile")
    attach.add_argument("--storage")
    attach.add_argument("--subpath", default="{playlist.folder_name}")
    detach = playlist_sub.add_parser("detach-profile")
    detach.add_argument("playlist")
    detach.add_argument("profile")

    options = sub.add_parser("options", help="合成済み yt-dlp コマンドを確認する")
    options_sub = options.add_subparsers(dest="options_command", required=True)
    preview = options_sub.add_parser("preview")
    preview.add_argument("--playlist", required=True)
    preview.add_argument("--profile", required=True)
    preview.add_argument("--kind", choices=["discover", "download"], default="download")
    preview.add_argument("--args", dest="override_args")


def _validate_local_path(value: str) -> str:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise CliValidationError(
            "local Storage の path は traversal を含まない絶対パスにしてください"
        )
    return str(path)


def _validate_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CliValidationError("URL は http:// または https:// の完全な形式で指定してください")
    return value


def _validate_playlist_args(value: str | None) -> None:
    if not value:
        return
    try:
        tokens = shlex.split(value)
    except ValueError as exc:
        raise CliValidationError(f"ytdlp_args の引用符が不正です: {exc}") from exc
    for occurrence in parse_managed_options(tokens):
        if occurrence.canonical == "--output":
            raise CliValidationError(
                "`--output` は ytdlp_args ではなく Profile の custom template を使用してください"
            )
        if occurrence.canonical == "--download-archive":
            raise CliValidationError("`--download-archive` は使用できません")


def _profile_values(args: argparse.Namespace, existing: Profile | None = None) -> dict[str, Any]:
    names = (
        "name",
        "description",
        "ytdlp_args",
        "format_selector",
        "container",
        "audio_format",
        "audio_quality",
        "subtitle_langs",
        "concurrent_fragments",
        "output_template",
        "audio_extract",
        "embed_metadata",
        "embed_thumbnail",
        "embed_chapters",
        "subtitle_auto",
        "subtitle_embed",
        "expert_mode",
        "allow_exec",
    )
    tristate_names = {
        "audio_extract",
        "embed_metadata",
        "embed_thumbnail",
        "embed_chapters",
        "subtitle_auto",
        "subtitle_embed",
    }
    values: dict[str, Any] = {}
    for name in names:
        value = getattr(args, name)
        if name in tristate_names:
            if value is not _MISSING:
                values[name] = value
        elif value is not None and value is not _MISSING:
            values[name] = value
    if args.kind is not None:
        values["kind"] = ProfileKind(args.kind)
    if args.layout_strategy is not None:
        values["layout_strategy"] = LayoutStrategy(args.layout_strategy)
    if existing is None:
        values.setdefault("layout_strategy", LayoutStrategy.FLAT)
        values["expert_mode"] = (
            False if args.expert_mode is _MISSING else bool(args.expert_mode)
        )
        values["allow_exec"] = False if args.allow_exec is _MISSING else bool(args.allow_exec)
        values["postprocess_chain_json"] = []
    return values


def _validate_profile(values: dict[str, Any], current: Profile | None, settings: Any) -> None:
    def final(name: str, fallback: Any = None) -> Any:
        if name in values:
            return values[name]
        return getattr(current, name) if current is not None else fallback

    expert_mode = bool(final("expert_mode", False))
    allow_exec = bool(final("allow_exec", False))
    try:
        guarded = guard_freeform(
            final("ytdlp_args"),
            source_label=f"Profile {final('name', '(新規)')}",
            expert_mode=expert_mode,
            env_allow_exec=bool(settings.ALLOW_EXEC),
            profile_allow_exec=allow_exec,
        )
        for warning in guarded.warnings:
            print(f"WARNING: {warning}", file=sys.stderr)
        kind = final("kind")
        strategy = final("layout_strategy", LayoutStrategy.FLAT)
        resolve_layout(
            strategy.value,
            LayoutContext(
                playlist_name="preview",
                playlist_folder_name="preview",
                profile_name=final("name", "preview"),
                profile_kind=kind.value,
                subpath="preview",
                custom_output_template=final("output_template"),
            ),
        )
    except (OptionValidationError, LayoutValidationError) as exc:
        raise CliValidationError(str(exc)) from exc
    concurrent = final("concurrent_fragments")
    if concurrent is not None and concurrent < 1:
        raise CliValidationError("concurrent_fragments は1以上にしてください")


def _storage_command(args: argparse.Namespace, session: Session) -> int:
    repo = StorageRepository(session)
    command = args.storage_command
    if command == "add":
        if args.kind != StorageKind.LOCAL.value:
            raise CliValidationError("kind=remote/mount は Phase 5 まで登録できません")
        storage = repo.create(
            name=args.name,
            kind=StorageKind.LOCAL,
            enabled=True,
            config_json={"path": _validate_local_path(args.path)},
        )
        print(f"Storage を作成しました: id={storage.id}")
        return 0
    if command == "list":
        _print_table(
            ("ID", "名前", "kind", "有効", "path"),
            [
                (
                    row.id,
                    row.name,
                    row.kind.value,
                    row.enabled,
                    (row.config_json or {}).get("path", ""),
                )
                for row in repo.list()
            ],
        )
        return 0
    storage = resolve_record(session, Storage, args.storage, label="Storage")
    if command == "show":
        _print_detail(
            (
                ("id", storage.id),
                ("name", storage.name),
                ("kind", storage.kind.value),
                ("enabled", storage.enabled),
                ("config", _masked_json(storage.config_json or {})),
                (
                    "credentials",
                    "********（設定済み）" if storage.credentials_encrypted else "（未設定）",
                ),
            )
        )
        return 0
    if command == "edit":
        updates: dict[str, Any] = {}
        if args.name is not None:
            updates["name"] = args.name
        if args.path is not None:
            config = dict(storage.config_json or {})
            config["path"] = _validate_local_path(args.path)
            updates["config_json"] = config
        if args.enabled is not _MISSING:
            updates["enabled"] = args.enabled
        if not updates:
            raise CliValidationError("変更項目を1つ以上指定してください")
        repo.update(storage, **updates)
        print(f"Storage を更新しました: id={storage.id}")
        return 0
    linked_profiles = session.scalar(
        select(func.count())
        .select_from(PlaylistProfile)
        .where(PlaylistProfile.storage_id == storage.id)
    )
    linked_artifacts = session.scalar(
        select(func.count()).select_from(Artifact).where(Artifact.storage_id == storage.id)
    )
    if linked_profiles or linked_artifacts:
        raise CliValidationError("参照中の Storage は削除できません。割当を解除してください")
    repo.delete(storage)
    print(f"Storage レコードを削除しました: id={storage.id}（ファイル操作なし）")
    return 0


def _profile_command(args: argparse.Namespace, session: Session, settings: Any) -> int:
    repo = ProfileRepository(session)
    command = args.profile_command
    if command == "add":
        values = _profile_values(args)
        _validate_profile(values, None, settings)
        profile = repo.create(**values)
        print(f"Profile を作成しました: id={profile.id}")
        return 0
    if command == "list":
        _print_table(
            ("ID", "名前", "kind", "layout", "expert", "exec"),
            [
                (
                    row.id,
                    row.name,
                    row.kind.value,
                    row.layout_strategy.value,
                    row.expert_mode,
                    row.allow_exec,
                )
                for row in repo.list()
            ],
        )
        return 0
    profile = resolve_record(session, Profile, args.profile, label="Profile")
    if command == "show":
        _print_detail(
            (
                ("id", profile.id),
                ("name", profile.name),
                ("kind", profile.kind.value),
                ("layout_strategy", profile.layout_strategy.value),
                ("output_template", profile.output_template or "(なし)"),
                ("ytdlp_args", _masked_args(profile.ytdlp_args)),
                ("format_selector", profile.format_selector),
                ("audio_extract", profile.audio_extract),
                ("embed_metadata", profile.embed_metadata),
                ("embed_thumbnail", profile.embed_thumbnail),
                ("embed_chapters", profile.embed_chapters),
                ("subtitle_auto", profile.subtitle_auto),
                ("subtitle_embed", profile.subtitle_embed),
                ("expert_mode", profile.expert_mode),
                ("allow_exec", profile.allow_exec),
            )
        )
        return 0
    if command == "edit":
        values = _profile_values(args, profile)
        if not values:
            raise CliValidationError("変更項目を1つ以上指定してください")
        _validate_profile(values, profile, settings)
        repo.update(profile, **values)
        print(f"Profile を更新しました: id={profile.id}")
        return 0
    linked = session.scalar(
        select(func.count())
        .select_from(PlaylistProfile)
        .where(PlaylistProfile.profile_id == profile.id)
    )
    if linked:
        raise CliValidationError(
            "割当中の Profile は削除できません。先に detach-profile してください"
        )
    repo.delete(profile)
    print(f"Profile レコードを削除しました: id={profile.id}（ファイル操作なし）")
    return 0


def _playlist_command(args: argparse.Namespace, session: Session) -> int:
    repo = PlaylistRepository(session)
    command = args.playlist_command
    if command == "add":
        _validate_playlist_args(args.ytdlp_args)
        try:
            folder_name = sanitize_component(args.folder_name)
        except NamingValidationError as exc:
            raise CliValidationError(str(exc)) from exc
        playlist = repo.create(
            name=args.name,
            folder_name=folder_name,
            url=_validate_url(args.url),
            enabled=not args.disable,
            kind_hint=PlaylistKindHint(args.kind_hint),
            ytdlp_args=args.ytdlp_args,
            paused=args.paused,
            dedup_hardlink=False,
        )
        print(f"Playlist を作成しました: id={playlist.id}")
        return 0
    if command == "list":
        _print_table(
            ("ID", "名前", "folder", "kind", "有効", "paused"),
            [
                (row.id, row.name, row.folder_name, row.kind_hint.value, row.enabled, row.paused)
                for row in repo.list()
            ],
        )
        return 0
    playlist = resolve_record(session, Playlist, args.playlist, label="Playlist")
    if command == "show":
        associations = list(
            session.scalars(
                select(PlaylistProfile).where(PlaylistProfile.playlist_id == playlist.id)
            )
        )
        _print_detail(
            (
                ("id", playlist.id),
                ("name", playlist.name),
                ("folder_name", playlist.folder_name),
                ("url", playlist.url),
                ("kind_hint", playlist.kind_hint.value),
                ("enabled", playlist.enabled),
                ("paused", playlist.paused),
                ("ytdlp_args", _masked_args(playlist.ytdlp_args)),
                (
                    "profiles",
                    ", ".join(
                        f"profile={row.profile_id}/storage={row.storage_id}/subpath={row.subpath}"
                        for row in associations
                    )
                    or "(なし)",
                ),
            )
        )
        return 0
    if command == "edit":
        updates: dict[str, Any] = {}
        for name in ("name", "ytdlp_args", "enabled", "paused"):
            value = getattr(args, name)
            if value is not None and value is not _MISSING:
                updates[name] = value
        if args.folder_name is not None:
            try:
                updates["folder_name"] = sanitize_component(args.folder_name)
            except NamingValidationError as exc:
                raise CliValidationError(str(exc)) from exc
        if args.url is not None:
            updates["url"] = _validate_url(args.url)
        if args.kind_hint is not None:
            updates["kind_hint"] = PlaylistKindHint(args.kind_hint)
        if "ytdlp_args" in updates:
            _validate_playlist_args(updates["ytdlp_args"])
        if not updates:
            raise CliValidationError("変更項目を1つ以上指定してください")
        repo.update(playlist, **updates)
        print(f"Playlist を更新しました: id={playlist.id}")
        return 0
    if command == "attach-profile":
        profile = resolve_record(session, Profile, args.profile, label="Profile")
        if find_playlist_profile(session, playlist.id, profile.id) is not None:
            raise CliValidationError("この Profile は既に割り当て済みです")
        if args.storage:
            storage = resolve_record(session, Storage, args.storage, label="Storage")
        else:
            enabled_storages = StorageRepository(session).list_enabled()
            if len(enabled_storages) != 1:
                raise CliValidationError(
                    "--storage を省略できるのは有効な Storage が1件だけの場合です"
                )
            storage = enabled_storages[0]
        try:
            resolve_layout(
                profile.layout_strategy.value,
                LayoutContext(
                    playlist_name=playlist.name,
                    playlist_folder_name=playlist.folder_name,
                    profile_name=profile.name,
                    profile_kind=profile.kind.value,
                    subpath=args.subpath,
                    custom_output_template=profile.output_template,
                ),
            )
        except LayoutValidationError as exc:
            raise CliValidationError(str(exc)) from exc
        attached = PlaylistProfileRepository(session).create(
            playlist_id=playlist.id,
            profile_id=profile.id,
            storage_id=storage.id,
            subpath=args.subpath,
            enabled=True,
            sort_order=0,
        )
        print(f"Profile を割り当てました: playlist_profile id={attached.id}")
        return 0
    if command == "detach-profile":
        profile = resolve_record(session, Profile, args.profile, label="Profile")
        existing_association = find_playlist_profile(session, playlist.id, profile.id)
        if existing_association is None:
            raise CliValidationError("この Profile は割り当てられていません")
        target_count = session.scalar(
            select(func.count()).select_from(Target).where(
                Target.playlist_profile_id == existing_association.id
            )
        )
        if target_count:
            raise CliValidationError("Target が存在する割当は解除できません")
        PlaylistProfileRepository(session).delete(existing_association)
        print("Profile の割当を解除しました（ファイル操作なし）")
        return 0

    if args.keep_items:
        repo.update(playlist, enabled=False, paused=True)
        print(
            f"Playlist を無効化・一時停止しました: id={playlist.id}"
            "（Item・Artifact・ファイルを保持）"
        )
        return 0
    run_count = session.scalar(
        select(func.count()).select_from(Run).where(Run.playlist_id == playlist.id)
    )
    if run_count:
        raise CliValidationError("Run 履歴が参照する Playlist は削除できません")
    # Item→Target→Artifact は DB の ON DELETE CASCADE。実ファイル API は呼ばない。
    session.execute(delete(Item).where(Item.playlist_id == playlist.id))
    session.execute(delete(PlaylistProfile).where(PlaylistProfile.playlist_id == playlist.id))
    session.delete(playlist)
    session.commit()
    print(f"Playlist と Item 関連DBレコードを削除しました: id={playlist.id}（ファイル操作なし）")
    return 0


def _options_command(args: argparse.Namespace, session: Session, settings: Any) -> int:
    playlist = resolve_record(session, Playlist, args.playlist, label="Playlist")
    profile = resolve_record(session, Profile, args.profile, label="Profile")
    association = find_playlist_profile(session, playlist.id, profile.id)
    if association is None:
        raise CliValidationError("Playlist に Profile が割り当てられていません")
    overrides = OptionOverrides(ytdlp_args=args.override_args)
    if args.kind == "discover":
        built = build_discover_args(
            playlist,
            session=session,
            profile=profile,
            overrides=overrides,
            env_allow_exec=bool(settings.ALLOW_EXEC),
        )
    else:
        built = build_download_args(
            None,
            source_url="<target.source_url>",
            session=session,
            staging_dir=settings.STAGING_DIR,
            work_id="<work-id>",
            playlist=playlist,
            profile=profile,
            playlist_profile=association,
            overrides=overrides,
            env_allow_exec=bool(settings.ALLOW_EXEC),
        )

    print(f"コマンド: {shlex.join(['yt-dlp', *mask_command_line(built.args)])}")
    print("由来:")
    for origin in built.origins:
        masked = shlex.join(mask_command_line(origin.arguments))
        field = f" / {origin.field}" if origin.field else ""
        print(f"  [{origin.layer}] {masked} <- {origin.source}{field}")
    print("警告:")
    if built.warnings:
        for warning in built.warnings:
            print(f"  - {warning}")
    else:
        print("  (なし)")
    print(f"解決済み出力パス: {built.resolved_output_path or '(なし)'}")
    return 0


def dispatch(
    args: argparse.Namespace,
    *,
    open_session: OpenSession,
    load_settings: LoadSettings,
) -> int | None:
    if args.command not in {"storage", "profile", "playlist", "options"}:
        return None
    session = open_session()
    try:
        settings = load_settings()
        if args.command == "storage":
            return _storage_command(args, session)
        if args.command == "profile":
            return _profile_command(args, session, settings)
        if args.command == "playlist":
            return _playlist_command(args, session)
        return _options_command(args, session, settings)
    except CliValidationError as exc:
        session.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


__all__ = [
    "CliValidationError",
    "configure_parsers",
    "dispatch",
    "find_playlist_profile",
    "resolve_record",
]
