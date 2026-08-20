"""Phase 9 の Web UI まで使用する暫定 CRUD / preview CLI。

入力検証はこの CLI 層で行い、リポジトリ層には状態遷移を持ち込まない。
レコード削除・関連解除はいかなる実ファイル操作も呼び出さない。
"""

from __future__ import annotations

import argparse
import getpass
import json
import shlex
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
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
    MissingPolicy,
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
from sluicery.storage import create_storage_adapter
from sluicery.storage.base import (
    ConnectionStage,
    ConnectionTestResult,
    StorageOperationError,
    StoragePathError,
    validate_relative_path,
)
from sluicery.storage.mount_cifs import MountStorageConfig, mount_storage_available
from sluicery.storage.rclone import RcloneConfigurationError, RcloneObscureError
from sluicery.storage.remote_rclone import UnsupportedRemoteProtocolError

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


def _add_clearable_value(
    parser: argparse.ArgumentParser,
    dest: str,
    option: str,
    clear_option: str,
    *,
    value_type: type[Any] | None = None,
) -> None:
    group = parser.add_mutually_exclusive_group()
    kwargs: dict[str, Any] = {"dest": dest}
    if value_type is not None:
        kwargs["type"] = value_type
    group.add_argument(option, **kwargs)
    group.add_argument(clear_option, dest=dest, action="store_const", const=None)
    parser.set_defaults(**{dest: _MISSING})


def _add_profile_fields(parser: argparse.ArgumentParser, *, require_identity: bool) -> None:
    parser.add_argument("--name", required=require_identity)
    parser.add_argument(
        "--kind",
        choices=[kind.value for kind in ProfileKind],
        required=require_identity,
    )
    _add_clearable_value(parser, "description", "--description", "--clear-description")
    _add_clearable_value(parser, "ytdlp_args", "--ytdlp-args", "--clear-ytdlp-args")
    _add_clearable_value(
        parser, "format_selector", "--format-selector", "--inherit-format-selector"
    )
    _add_clearable_value(parser, "container", "--container", "--inherit-container")
    _add_clearable_value(
        parser, "audio_format", "--audio-format", "--inherit-audio-format"
    )
    _add_clearable_value(
        parser, "audio_quality", "--audio-quality", "--inherit-audio-quality"
    )
    _add_clearable_value(
        parser, "subtitle_langs", "--subtitle-langs", "--inherit-subtitle-langs"
    )
    _add_clearable_value(
        parser,
        "concurrent_fragments",
        "--concurrent-fragments",
        "--inherit-concurrent-fragments",
        value_type=int,
    )
    parser.add_argument("--layout-strategy", choices=[item.value for item in LayoutStrategy])
    _add_clearable_value(
        parser, "output_template", "--output-template", "--clear-output-template"
    )
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
    storage = sub.add_parser("storage", help="Storage レコードと接続を管理する")
    storage_sub = storage.add_subparsers(dest="storage_command", required=True)
    storage_add = storage_sub.add_parser("add")
    storage_add.add_argument("--name", required=True)
    storage_add.add_argument(
        "--kind", choices=[kind.value for kind in StorageKind], required=True
    )
    storage_add.add_argument("--path")
    storage_add.add_argument("--protocol", choices=["smb", "cifs", "nfs"])
    storage_add.add_argument("--host")
    storage_add.add_argument("--share")
    storage_add.add_argument("--port", type=int)
    storage_add.add_argument("--user")
    storage_add.add_argument("--domain")
    storage_add.add_argument(
        "--password-stdin",
        action="store_true",
        help="password を標準入力の先頭行から読み取る（引数では受け取らない）",
    )
    storage_sub.add_parser("list")
    storage_show = storage_sub.add_parser("show")
    storage_show.add_argument("storage")
    storage_edit = storage_sub.add_parser("edit")
    storage_edit.add_argument("storage")
    storage_edit.add_argument("--name")
    storage_edit.add_argument("--path")
    storage_edit.add_argument("--host")
    storage_edit.add_argument("--share")
    storage_edit.add_argument("--port", type=int)
    storage_edit.add_argument("--user")
    domain_group = storage_edit.add_mutually_exclusive_group()
    domain_group.add_argument("--domain")
    domain_group.add_argument("--clear-domain", action="store_true")
    password_group = storage_edit.add_mutually_exclusive_group()
    password_group.add_argument("--prompt-password", action="store_true")
    password_group.add_argument("--password-stdin", action="store_true")
    _add_enable_pair(storage_edit)
    storage_remove = storage_sub.add_parser("remove")
    storage_remove.add_argument("storage")
    storage_test = storage_sub.add_parser("test", help="4段階の接続テストを実行する")
    storage_test.add_argument("storage")
    storage_space = storage_sub.add_parser("space", help="空き容量を表示する")
    storage_space.add_argument("storage")
    storage_ls = storage_sub.add_parser("ls", help="Storage root からの相対パスを一覧する")
    storage_ls.add_argument("storage")
    storage_ls.add_argument("rel_path", nargs="?", default="")
    storage_push = storage_sub.add_parser("push", help="Phase 7 までの暫定単一ファイル publish")
    storage_push.add_argument("storage")
    storage_push.add_argument("local_path", type=Path)
    storage_push.add_argument("dest_rel")
    storage_push.add_argument("--overwrite", action="store_true")

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
    playlist_add.add_argument("--dedup-hardlink", action="store_true")
    playlist_add.add_argument(
        "--missing-policy",
        choices=[item.value for item in MissingPolicy],
        default=MissingPolicy.LEAVE.value,
    )
    playlist_sub.add_parser("list")
    playlist_show = playlist_sub.add_parser("show")
    playlist_show.add_argument("playlist")
    playlist_edit = playlist_sub.add_parser("edit")
    playlist_edit.add_argument("playlist")
    playlist_edit.add_argument("--name")
    playlist_edit.add_argument("--folder-name")
    playlist_edit.add_argument("--url")
    playlist_edit.add_argument("--kind-hint", choices=[item.value for item in PlaylistKindHint])
    playlist_edit.add_argument(
        "--missing-policy", choices=[item.value for item in MissingPolicy]
    )
    _add_clearable_value(
        playlist_edit, "ytdlp_args", "--ytdlp-args", "--clear-ytdlp-args"
    )
    _add_enable_pair(playlist_edit)
    _add_bool_pair(playlist_edit, "paused", "--pause", "--resume")
    _add_bool_pair(
        playlist_edit,
        "dedup_hardlink",
        "--dedup-hardlink",
        "--no-dedup-hardlink",
    )
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
    try:
        if path.is_absolute():
            boundary = Path("/mnt/media")
            resolved = path.resolve(strict=False)
            if not resolved.is_relative_to(boundary):
                raise CliValidationError(
                    "local Storage の絶対 path は /mnt/media 配下にしてください"
                )
            return str(resolved)
        return validate_relative_path(value, allow_empty=True)
    except StoragePathError as exc:
        raise CliValidationError(str(exc)) from exc


def _validate_remote_values(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    missing = [
        option
        for option in ("protocol", "host", "share", "user")
        if not getattr(args, option, None)
    ]
    if missing:
        raise CliValidationError(
            "remote Storage に必要な項目が不足しています: " + ", ".join(missing)
        )
    if args.protocol != "smb":
        raise CliValidationError("Phase 5 で実装・検証済みの protocol は smb だけです")
    if "/" in args.share or "\\" in args.share or args.share in {".", ".."}:
        raise CliValidationError("share はパス区切りを含まない共有名で指定してください")
    port = args.port if args.port is not None else 445
    if not 1 <= port <= 65535:
        raise CliValidationError("port は1〜65535で指定してください")
    try:
        remote_path = validate_relative_path(args.path or "", allow_empty=True)
    except StoragePathError as exc:
        raise CliValidationError(str(exc)) from exc
    password = _read_password(password_stdin=args.password_stdin, prompt=True)
    return (
        {
            "protocol": "smb",
            "host": args.host,
            "share": args.share,
            "path": remote_path,
            "port": port,
        },
        {"user": args.user, "password": password, "domain": args.domain or ""},
    )


def _read_password(*, password_stdin: bool, prompt: bool) -> str:
    if password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    elif prompt:
        password = getpass.getpass("Storage password: ")
    else:
        raise CliValidationError("password の入力方法が指定されていません")
    if not password:
        raise CliValidationError("password を空にできません")
    return password


def _validate_mount_values(
    args: argparse.Namespace,
) -> tuple[dict[str, Any], dict[str, str] | None]:
    if not mount_storage_available():
        raise CliValidationError(
            "mount Storageはcompose.privileged.yaml明示指定時だけ利用できます"
        )
    protocol = args.protocol or "cifs"
    default_port = 445 if protocol == "cifs" else 2049
    try:
        config = MountStorageConfig.parse(
            {
                "protocol": protocol,
                "host": args.host,
                "share": args.share,
                "path": args.path or "",
                "port": args.port if args.port is not None else default_port,
            }
        )
    except ValueError as exc:
        raise CliValidationError(str(exc)) from exc
    portable: dict[str, Any] = {
        "protocol": config.protocol,
        "host": config.host,
        "share": config.share,
        "path": config.path,
        "port": config.port,
    }
    if protocol == "nfs":
        return portable, None
    if not args.user:
        raise CliValidationError("CIFS mountには--userが必要です")
    password = _read_password(password_stdin=args.password_stdin, prompt=True)
    return portable, {"user": args.user, "password": password, "domain": args.domain or ""}


_STAGE_LABELS = {
    ConnectionStage.CONNECTIVITY: "疎通",
    ConnectionStage.AUTHENTICATION: "認証",
    ConnectionStage.LISTING: "一覧",
    ConnectionStage.WRITE: "書き込み",
}


def _connection_result_json(result: ConnectionTestResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "stages": [
            {
                "stage": stage.stage.value,
                "status": stage.status.value,
                "message": stage.message,
                "classification": stage.classification.value,
                "reason_code": stage.reason_code,
            }
            for stage in result.stages
        ],
        "cleanup_warning": result.cleanup_warning,
    }


def _adapter_for(storage: Storage, session: Session):
    from sluicery.core.settings import OperationalSettings

    try:
        return create_storage_adapter(storage, OperationalSettings(session))
    except (
        ValueError,
        StorageOperationError,
        RcloneConfigurationError,
        RcloneObscureError,
        UnsupportedRemoteProtocolError,
    ) as exc:
        raise CliValidationError(str(exc)) from exc


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
    nullable_names = {
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
    }
    values: dict[str, Any] = {}
    for name in names:
        value = getattr(args, name)
        if name in nullable_names:
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
        if args.kind == StorageKind.LOCAL.value:
            if args.path is None:
                raise CliValidationError("local Storage には --path が必要です")
            storage = repo.create(
                name=args.name,
                kind=StorageKind.LOCAL,
                enabled=True,
                config_json={"path": _validate_local_path(args.path)},
            )
        elif args.kind == StorageKind.REMOTE.value:
            config, credentials = _validate_remote_values(args)
            # credentials_encrypted は設定エクスポート（Phase 17）の対象外。
            storage = repo.create(
                name=args.name,
                kind=StorageKind.REMOTE,
                enabled=True,
                config_json=config,
                credentials_encrypted=credentials,
            )
        else:
            config, mount_credentials = _validate_mount_values(args)
            storage = repo.create(
                name=args.name,
                kind=StorageKind.MOUNT,
                enabled=True,
                config_json=config,
                credentials_encrypted=mount_credentials,
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
                ("last_check_at", storage.last_check_at or "（未実行）"),
                ("last_check_result", _masked_json(storage.last_check_result_json or {})),
            )
        )
        return 0
    if command == "edit":
        updates: dict[str, Any] = {}
        if args.name is not None:
            updates["name"] = args.name
        if args.path is not None:
            config = dict(storage.config_json or {})
            try:
                config["path"] = (
                    _validate_local_path(args.path)
                    if storage.kind == StorageKind.LOCAL
                    else validate_relative_path(args.path, allow_empty=True)
                )
            except StoragePathError as exc:
                raise CliValidationError(str(exc)) from exc
            if storage.kind == StorageKind.MOUNT:
                if not mount_storage_available():
                    raise CliValidationError(
                        "mount Storageはcompose.privileged.yaml明示指定時だけ編集できます"
                    )
                try:
                    MountStorageConfig.parse(config)
                except ValueError as exc:
                    raise CliValidationError(str(exc)) from exc
            updates["config_json"] = config
        remote_fields = ("host", "share", "port")
        if any(getattr(args, field) is not None for field in remote_fields):
            if storage.kind not in {StorageKind.REMOTE, StorageKind.MOUNT}:
                raise CliValidationError("host/share/port は remote / mount Storage 専用です")
            config = dict(updates.get("config_json", storage.config_json or {}))
            for field in remote_fields:
                value = getattr(args, field)
                if value is not None:
                    config[field] = value
            if not 1 <= int(config.get("port", 445)) <= 65535:
                raise CliValidationError("port は1〜65535で指定してください")
            if storage.kind == StorageKind.REMOTE:
                share = config.get("share", "")
                if "/" in share or "\\" in share or share in {".", ".."}:
                    raise CliValidationError(
                        "share はパス区切りを含まない共有名で指定してください"
                    )
            else:
                if not mount_storage_available():
                    raise CliValidationError(
                        "mount Storageはcompose.privileged.yaml明示指定時だけ編集できます"
                    )
                try:
                    MountStorageConfig.parse(config)
                except ValueError as exc:
                    raise CliValidationError(str(exc)) from exc
            updates["config_json"] = config
        credential_change = (
            args.user is not None
            or args.domain is not None
            or args.clear_domain
            or args.prompt_password
            or args.password_stdin
        )
        if credential_change:
            mount_cifs = bool(
                storage.kind == StorageKind.MOUNT
                and (storage.config_json or {}).get("protocol") == "cifs"
            )
            if storage.kind != StorageKind.REMOTE and not mount_cifs:
                raise CliValidationError("認証情報はremoteまたはCIFS mount専用です")
            if storage.kind == StorageKind.MOUNT and not mount_storage_available():
                raise CliValidationError(
                    "mount Storageはcompose.privileged.yaml明示指定時だけ編集できます"
                )
            credentials = dict(storage.credentials_encrypted or {})
            if args.user is not None:
                credentials["user"] = args.user
            if args.domain is not None:
                credentials["domain"] = args.domain
            elif args.clear_domain:
                credentials["domain"] = ""
            if args.prompt_password or args.password_stdin:
                credentials["password"] = _read_password(
                    password_stdin=args.password_stdin, prompt=args.prompt_password
                )
            updates["credentials_encrypted"] = credentials
        if args.enabled is not _MISSING:
            updates["enabled"] = args.enabled
        if not updates:
            raise CliValidationError("変更項目を1つ以上指定してください")
        repo.update(storage, **updates)
        print(f"Storage を更新しました: id={storage.id}")
        return 0
    if command == "test":
        adapter = _adapter_for(storage, session)
        try:
            result = adapter.test_connection()
        except (
            ValueError,
            StorageOperationError,
            RcloneConfigurationError,
            RcloneObscureError,
        ) as exc:
            raise CliValidationError(str(exc)) from exc
        payload = _connection_result_json(result)
        repo.update(
            storage,
            last_check_at=datetime.now(UTC),
            last_check_result_json=payload,
        )
        for stage in result.stages:
            print(
                f"{_STAGE_LABELS[stage.stage]}: {stage.status.value} - "
                f"{stage.message} ({stage.classification.value})"
            )
        if result.cleanup_warning:
            print(f"WARNING: {result.cleanup_warning}")
        return 0 if result.ok else 1
    if command == "space":
        adapter = _adapter_for(storage, session)
        try:
            free = adapter.free_space()
        except StorageOperationError as exc:
            raise CliValidationError(str(exc)) from exc
        print("取得不可" if free is None else f"{free} bytes")
        return 0
    if command == "ls":
        adapter = _adapter_for(storage, session)
        try:
            rows = list(adapter.list_recursive(args.rel_path))
        except (StorageOperationError, StoragePathError) as exc:
            raise CliValidationError(str(exc)) from exc
        _print_table(
            ("相対パス", "サイズ", "更新日時"),
            [
                (
                    item.relative_path,
                    item.size if item.size is not None else "?",
                    item.modified_at or "",
                )
                for item in rows
            ],
        )
        return 0
    if command == "push":
        # Phase 7 の pipeline 実装までの検証専用経路。Artifact は作成しない。
        adapter = _adapter_for(storage, session)
        try:
            result = adapter.publish(args.local_path, args.dest_rel, overwrite=args.overwrite)
        except (StorageOperationError, StoragePathError) as exc:
            raise CliValidationError(str(exc)) from exc
        if not result.success:
            temporary = f"（一時ファイル: {result.temporary_rel}）" if result.temporary_rel else ""
            raise CliValidationError(f"{result.message}{temporary}")
        print(f"publish 完了: {result.dest_rel} ({result.size} bytes)")
        if result.reason_code == "ok_source_retained":
            print(f"WARNING: {result.message}")
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
                ("description", profile.description or "(なし)"),
                ("kind", profile.kind.value),
                ("layout_strategy", profile.layout_strategy.value),
                ("output_template", profile.output_template or "(なし)"),
                ("ytdlp_args", _masked_args(profile.ytdlp_args)),
                ("format_selector", profile.format_selector or "(継承)"),
                ("container", profile.container or "(継承)"),
                ("audio_format", profile.audio_format or "(継承)"),
                ("audio_quality", profile.audio_quality or "(継承)"),
                ("subtitle_langs", profile.subtitle_langs or "(継承)"),
                (
                    "concurrent_fragments",
                    profile.concurrent_fragments
                    if profile.concurrent_fragments is not None
                    else "(継承)",
                ),
                ("audio_extract", profile.audio_extract),
                ("embed_metadata", profile.embed_metadata),
                ("embed_thumbnail", profile.embed_thumbnail),
                ("embed_chapters", profile.embed_chapters),
                ("subtitle_auto", profile.subtitle_auto),
                ("subtitle_embed", profile.subtitle_embed),
                ("expert_mode", profile.expert_mode),
                ("allow_exec", profile.allow_exec),
                ("postprocess_chain", _masked_json(profile.postprocess_chain_json or [])),
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
            missing_policy=MissingPolicy(args.missing_policy),
            dedup_hardlink=args.dedup_hardlink,
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
                ("url", mask_command_line([playlist.url])[0]),
                ("kind_hint", playlist.kind_hint.value),
                ("enabled", playlist.enabled),
                ("paused", playlist.paused),
                ("missing_policy", playlist.missing_policy.value),
                ("dedup_hardlink", playlist.dedup_hardlink),
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
        for name in ("name", "enabled", "paused", "dedup_hardlink"):
            value = getattr(args, name)
            if value is not None and value is not _MISSING:
                updates[name] = value
        if args.ytdlp_args is not _MISSING:
            updates["ytdlp_args"] = args.ytdlp_args
        if args.folder_name is not None:
            try:
                requested_folder_name = sanitize_component(args.folder_name)
            except NamingValidationError as exc:
                raise CliValidationError(str(exc)) from exc
            if requested_folder_name != playlist.folder_name:
                raise CliValidationError(
                    "既存Playlistのfolder_nameはplaylist editでは変更できません。"
                    "Webの「フォルダも移動する」で対象件数を確認して実行してください"
                )
        if args.url is not None:
            updates["url"] = _validate_url(args.url)
        if args.kind_hint is not None:
            updates["kind_hint"] = PlaylistKindHint(args.kind_hint)
        if args.missing_policy is not None:
            updates["missing_policy"] = MissingPolicy(args.missing_policy)
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
    try:
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
                source_url=playlist.url,
                session=session,
                staging_dir=settings.STAGING_DIR,
                work_id="<work-id>",
                playlist=playlist,
                profile=profile,
                playlist_profile=association,
                overrides=overrides,
                env_allow_exec=bool(settings.ALLOW_EXEC),
            )
    except (OptionValidationError, LayoutValidationError) as exc:
        raise CliValidationError(str(exc)) from exc

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
