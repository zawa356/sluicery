"""秘密と実行状態を除外した設定YAMLのexport / preview / import。"""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from sluicery.core import settings as core_settings
from sluicery.db.models import (
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
)
from sluicery.runner.base import mask_command_line

MAX_CONFIG_BYTES = 1024 * 1024
IMPORT_CONFIRMATION_TTL_SEC = 1800
CollisionMode = Literal["skip", "overwrite", "create"]
Action = Literal["create", "overwrite", "skip"]


class ConfigTransferError(ValueError):
    """設定文書またはimport操作が不正。"""


class ConfigImportConfirmationError(ConfigTransferError):
    """import確認tokenが不正、期限切れ、またはDB状態と不一致。"""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StorageConfig(_StrictModel):
    ref: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)
    kind: StorageKind
    enabled: bool
    config: dict[str, Any] | None = None
    requires_credentials: bool = False


class ProfileConfig(_StrictModel):
    ref: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    kind: ProfileKind
    ytdlp_args: str | None = Field(default=None, max_length=4000)
    format_selector: str | None = Field(default=None, max_length=1000)
    output_template: str | None = Field(default=None, max_length=1000)
    layout_strategy: LayoutStrategy
    audio_extract: bool | None = None
    audio_format: str | None = Field(default=None, max_length=50)
    audio_quality: str | None = Field(default=None, max_length=50)
    container: str | None = Field(default=None, max_length=50)
    embed_metadata: bool | None = None
    embed_thumbnail: bool | None = None
    embed_chapters: bool | None = None
    subtitle_langs: str | None = Field(default=None, max_length=255)
    subtitle_auto: bool | None = None
    subtitle_embed: bool | None = None
    expert_mode: bool
    allow_exec: bool
    concurrent_fragments: int | None = Field(default=None, ge=1)
    postprocess_chain: dict[str, Any] | None = None
    requires_secret_reentry: bool = False


class PlaylistConfig(_StrictModel):
    ref: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)
    folder_name: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=1, max_length=2000)
    enabled: bool
    kind_hint: PlaylistKindHint
    ytdlp_args: str | None = Field(default=None, max_length=4000)
    discover_cron: str | None = Field(default=None, max_length=100)
    download_cron: str | None = Field(default=None, max_length=100)
    paused: bool
    retention_policy: dict[str, Any] | None = None
    missing_policy: MissingPolicy
    dedup_hardlink: bool
    requires_cookie_reentry: bool = False
    requires_secret_reentry: bool = False
    requires_url_reentry: bool = False


class PlaylistProfileConfig(_StrictModel):
    playlist_ref: str
    profile_ref: str
    storage_ref: str
    subpath: str = Field(min_length=1, max_length=1000)
    enabled: bool
    sort_order: int


class ConfigDocument(_StrictModel):
    version: Literal[1] = 1
    storages: list[StorageConfig] = Field(default_factory=list, max_length=1000)
    profiles: list[ProfileConfig] = Field(default_factory=list, max_length=1000)
    playlists: list[PlaylistConfig] = Field(default_factory=list, max_length=1000)
    playlist_profiles: list[PlaylistProfileConfig] = Field(
        default_factory=list, max_length=5000
    )
    settings: dict[str, Any] = Field(default_factory=dict, max_length=1000)

    @model_validator(mode="after")
    def validate_references(self) -> ConfigDocument:
        ref_sets: dict[str, set[str]] = {}
        for label, rows in (
            ("Storage", self.storages),
            ("Profile", self.profiles),
            ("Playlist", self.playlists),
        ):
            refs = [row.ref for row in rows]
            names = [row.name for row in rows]
            if len(refs) != len(set(refs)):
                raise ValueError(f"{label} refが重複しています")
            if len(names) != len(set(names)):
                raise ValueError(f"{label}名が文書内で重複しています")
            ref_sets[label] = set(refs)
        assignment_keys: set[tuple[str, str]] = set()
        for row in self.playlist_profiles:
            if row.playlist_ref not in ref_sets["Playlist"]:
                raise ValueError("playlist_profileのPlaylist参照が不正です")
            if row.profile_ref not in ref_sets["Profile"]:
                raise ValueError("playlist_profileのProfile参照が不正です")
            if row.storage_ref not in ref_sets["Storage"]:
                raise ValueError("playlist_profileのStorage参照が不正です")
            key = (row.playlist_ref, row.profile_ref)
            if key in assignment_keys:
                raise ValueError("playlist_profileが文書内で重複しています")
            assignment_keys.add(key)
        unknown = set(self.settings) - set(core_settings.CODE_DEFAULTS)
        if unknown:
            raise ValueError("未知または内部用の設定キーが含まれています")
        return self


@dataclass(frozen=True)
class ImportOperation:
    entity: str
    identity: str
    action: Action
    note: str = ""


@dataclass(frozen=True)
class ConfigImportPlan:
    mode: CollisionMode
    operations: tuple[ImportOperation, ...]
    fingerprint: str

    @property
    def creates(self) -> int:
        return sum(row.action == "create" for row in self.operations)

    @property
    def overwrites(self) -> int:
        return sum(row.action == "overwrite" for row in self.operations)

    @property
    def skips(self) -> int:
        return sum(row.action == "skip" for row in self.operations)


@dataclass(frozen=True)
class ConfigImportResult:
    created: int
    overwritten: int
    skipped: int


_SENSITIVE_CONFIG_KEYS = {
    "authorization",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "domain",
    "password",
    "passwd",
    "secret",
    "token",
    "user",
    "username",
}


def _without_sensitive_config(value: object) -> tuple[object, bool]:
    if isinstance(value, dict):
        result: dict[str, object] = {}
        omitted = False
        for key, nested in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _SENSITIVE_CONFIG_KEYS or any(
                fragment in normalized
                for fragment in ("password", "secret", "token", "cookie", "credential")
            ):
                omitted = True
                continue
            cleaned, nested_omitted = _without_sensitive_config(nested)
            result[str(key)] = cleaned
            omitted = omitted or nested_omitted
        return result, omitted
    if isinstance(value, list):
        result_list = []
        omitted = False
        for nested in value:
            cleaned, nested_omitted = _without_sensitive_config(nested)
            result_list.append(cleaned)
            omitted = omitted or nested_omitted
        return result_list, omitted
    return value, False


def _portable_argument_text(value: str | None) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    try:
        tokens = shlex.split(value)
    except ValueError:
        return None, True
    if list(mask_command_line(tokens)) != tokens:
        return None, True
    return value, False


def _portable_source_url(value: str) -> tuple[str, bool]:
    if list(mask_command_line([value])) != [value]:
        return "https://invalid.local/reenter-source-url", True
    return value, False


def export_config(session: Session) -> ConfigDocument:
    """DB設定だけをportableな文書へ変換する。秘密と実行状態は読まない。"""
    storages = list(session.scalars(select(Storage).order_by(Storage.id)))
    profiles = list(session.scalars(select(Profile).order_by(Profile.id)))
    playlists = list(session.scalars(select(Playlist).order_by(Playlist.id)))
    assignments = list(
        session.scalars(select(PlaylistProfile).order_by(PlaylistProfile.id))
    )
    storage_refs = {row.id: f"storage-{index}" for index, row in enumerate(storages, 1)}
    profile_refs = {row.id: f"profile-{index}" for index, row in enumerate(profiles, 1)}
    playlist_refs = {row.id: f"playlist-{index}" for index, row in enumerate(playlists, 1)}

    storage_docs: list[StorageConfig] = []
    for storage_row in storages:
        cleaned, omitted = _without_sensitive_config(storage_row.config_json)
        storage_docs.append(
            StorageConfig(
                ref=storage_refs[storage_row.id],
                name=storage_row.name,
                kind=storage_row.kind,
                enabled=storage_row.enabled,
                config=cleaned if isinstance(cleaned, dict) else None,
                requires_credentials=(
                    storage_row.credentials_encrypted is not None
                    or storage_row.kind == StorageKind.REMOTE
                    or omitted
                ),
            )
        )
    profile_docs: list[ProfileConfig] = []
    for profile_row in profiles:
        ytdlp_args, args_omitted = _portable_argument_text(profile_row.ytdlp_args)
        postprocess_chain, postprocess_omitted = _without_sensitive_config(
            profile_row.postprocess_chain_json
        )
        profile_docs.append(
            ProfileConfig(
                ref=profile_refs[profile_row.id],
                name=profile_row.name,
                description=profile_row.description,
                kind=profile_row.kind,
                ytdlp_args=ytdlp_args,
                format_selector=profile_row.format_selector,
                output_template=profile_row.output_template,
                layout_strategy=profile_row.layout_strategy,
                audio_extract=profile_row.audio_extract,
                audio_format=profile_row.audio_format,
                audio_quality=profile_row.audio_quality,
                container=profile_row.container,
                embed_metadata=profile_row.embed_metadata,
                embed_thumbnail=profile_row.embed_thumbnail,
                embed_chapters=profile_row.embed_chapters,
                subtitle_langs=profile_row.subtitle_langs,
                subtitle_auto=profile_row.subtitle_auto,
                subtitle_embed=profile_row.subtitle_embed,
                expert_mode=profile_row.expert_mode,
                allow_exec=profile_row.allow_exec,
                concurrent_fragments=profile_row.concurrent_fragments,
                postprocess_chain=(
                    postprocess_chain if isinstance(postprocess_chain, dict) else None
                ),
                requires_secret_reentry=args_omitted or postprocess_omitted,
            )
        )
    playlist_docs: list[PlaylistConfig] = []
    for playlist_row in playlists:
        ytdlp_args, args_omitted = _portable_argument_text(playlist_row.ytdlp_args)
        source_url, url_omitted = _portable_source_url(playlist_row.url)
        playlist_docs.append(
            PlaylistConfig(
                ref=playlist_refs[playlist_row.id],
                name=playlist_row.name,
                folder_name=playlist_row.folder_name,
                url=source_url,
                enabled=playlist_row.enabled,
                kind_hint=playlist_row.kind_hint,
                ytdlp_args=ytdlp_args,
                discover_cron=playlist_row.discover_cron,
                download_cron=playlist_row.download_cron,
                paused=playlist_row.paused,
                retention_policy=playlist_row.retention_policy_json,
                missing_policy=playlist_row.missing_policy,
                dedup_hardlink=playlist_row.dedup_hardlink,
                requires_cookie_reentry=(
                    playlist_row.cookie_enabled
                    or playlist_row.cookies_encrypted is not None
                ),
                requires_secret_reentry=args_omitted,
                requires_url_reentry=url_omitted,
            )
        )
    assignment_docs = [
        PlaylistProfileConfig(
            playlist_ref=playlist_refs[row.playlist_id],
            profile_ref=profile_refs[row.profile_id],
            storage_ref=storage_refs[row.storage_id],
            subpath=row.subpath,
            enabled=row.enabled,
            sort_order=row.sort_order,
        )
        for row in assignments
    ]
    overrides = {
        entry.key: entry.value
        for entry in core_settings.list_all(session)
        if entry.is_override
    }
    return ConfigDocument(
        storages=storage_docs,
        profiles=profile_docs,
        playlists=playlist_docs,
        playlist_profiles=assignment_docs,
        settings=overrides,
    )


def dump_config_yaml(document: ConfigDocument) -> str:
    return yaml.safe_dump(
        document.model_dump(mode="json"),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def load_config_yaml(raw: bytes | str) -> ConfigDocument:
    encoded = raw.encode() if isinstance(raw, str) else raw
    if not encoded or len(encoded) > MAX_CONFIG_BYTES:
        raise ConfigTransferError("YAMLは1 byte以上1 MiB以下にしてください")
    try:
        if any(
            isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
            for token in yaml.scan(encoded)
        ):
            raise ConfigTransferError("YAML alias / anchorは使用できません")
        parsed = yaml.safe_load(encoded)
        document = ConfigDocument.model_validate(parsed)
    except ConfigTransferError:
        raise
    except (yaml.YAMLError, UnicodeDecodeError, ValidationError, ValueError) as exc:
        raise ConfigTransferError("設定YAMLの形式または値が不正です") from exc
    for key, value in document.settings.items():
        try:
            core_settings.validate_override(key, value)
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigTransferError(f"設定値が不正です: {key}") from exc
    return document


def _one_by_name(session: Session, model: type[Any], name: str) -> Any | None:
    rows = list(session.scalars(select(model).where(model.name == name)))
    if len(rows) > 1:
        raise ConfigTransferError(f"既存DBに同名{name}が複数あり衝突を解決できません")
    return rows[0] if rows else None


def _action(existing: object | None, mode: CollisionMode) -> Action:
    if existing is None:
        return "create"
    if mode == "overwrite":
        return "overwrite"
    if mode == "skip":
        return "skip"
    return "create"


def preview_config_import(
    session: Session, document: ConfigDocument, mode: CollisionMode
) -> ConfigImportPlan:
    if mode not in {"skip", "overwrite", "create"}:
        raise ConfigTransferError("衝突時の動作が不正です")
    operations: list[ImportOperation] = []
    state: list[dict[str, Any]] = []
    resolved: dict[str, Any | None] = {}
    for entity, model, rows in (
        ("Storage", Storage, document.storages),
        ("Profile", Profile, document.profiles),
        ("Playlist", Playlist, document.playlists),
    ):
        for row in rows:
            existing = _one_by_name(session, model, row.name)
            action = _action(existing, mode)
            resolved[row.ref] = existing if action != "create" else None
            note = ""
            if isinstance(row, StorageConfig) and row.requires_credentials:
                note = "クレデンシャル要再入力"
            elif isinstance(row, ProfileConfig) and row.requires_secret_reentry:
                note = "秘密を含むオプション要再入力"
            elif isinstance(row, PlaylistConfig):
                requirements = []
                if row.requires_cookie_reentry:
                    requirements.append("Cookie")
                if row.requires_secret_reentry:
                    requirements.append("秘密を含むオプション")
                if row.requires_url_reentry:
                    requirements.append("source URL")
                if requirements:
                    note = " / ".join(requirements) + "要再入力"
            operations.append(ImportOperation(entity, row.name, action, note))
            state.append(
                {
                    "entity": entity,
                    "ref": row.ref,
                    "existing_id": getattr(existing, "id", None),
                    "updated_at": getattr(existing, "updated_at", None),
                    "action": action,
                }
            )
    for assignment_row in document.playlist_profiles:
        playlist = resolved[assignment_row.playlist_ref]
        profile = resolved[assignment_row.profile_ref]
        existing = None
        if playlist is not None and profile is not None:
            existing = session.scalar(
                select(PlaylistProfile).where(
                    PlaylistProfile.playlist_id == playlist.id,
                    PlaylistProfile.profile_id == profile.id,
                )
            )
        action = _action(existing, mode)
        identity = f"{assignment_row.playlist_ref} / {assignment_row.profile_ref}"
        operations.append(ImportOperation("playlist_profile", identity, action))
        state.append(
            {
                "entity": "playlist_profile",
                "ref": identity,
                "existing_id": getattr(existing, "id", None),
                "updated_at": getattr(existing, "updated_at", None),
                "action": action,
            }
        )
    for key in sorted(document.settings):
        existing = session.get(Setting, key)
        action = _action(existing, mode)
        if mode == "create" and existing is not None:
            action = "skip"  # Setting keyは一意で複製できない。
        operations.append(ImportOperation("setting", key, action))
        state.append(
            {
                "entity": "setting",
                "ref": key,
                "existing_value": existing.value_json if existing else None,
                "updated_at": existing.updated_at if existing else None,
                "action": action,
            }
        )
    encoded = json.dumps(state, default=str, sort_keys=True, separators=(",", ":")).encode()
    return ConfigImportPlan(
        mode, tuple(operations), hashlib.sha256(encoded).hexdigest()
    )


def _model_values(row: BaseModel, *, exclude: set[str]) -> dict[str, Any]:
    return row.model_dump(mode="python", exclude=exclude)


def apply_config_import(
    session: Session,
    document: ConfigDocument,
    plan: ConfigImportPlan,
) -> ConfigImportResult:
    """確認済み差分を単一DB transactionへ適用する。Storage I/Oは行わない。"""
    current = preview_config_import(session, document, plan.mode)
    if current.fingerprint != plan.fingerprint:
        raise ConfigImportConfirmationError(
            "差分確認後にDB状態が変化しました。再確認してください"
        )
    operations = iter(current.operations)
    mapped: dict[str, Any] = {}
    created = overwritten = skipped = 0
    try:
        for model, rows in (
            (Storage, document.storages),
            (Profile, document.profiles),
            (Playlist, document.playlists),
        ):
            for row in rows:
                operation = next(operations)
                existing = _one_by_name(session, model, row.name)
                if operation.action == "skip":
                    assert existing is not None
                    mapped[row.ref] = existing
                    skipped += 1
                    continue
                if isinstance(row, StorageConfig):
                    values = _model_values(
                        row, exclude={"ref", "config", "requires_credentials"}
                    )
                    values["config_json"] = row.config
                    if row.requires_credentials and (
                        existing is None or existing.credentials_encrypted is None
                    ):
                        values["enabled"] = False
                elif isinstance(row, ProfileConfig):
                    excluded = {"ref", "postprocess_chain", "requires_secret_reentry"}
                    if row.requires_secret_reentry and existing is not None:
                        excluded.add("ytdlp_args")
                    values = _model_values(row, exclude=excluded)
                    values["postprocess_chain_json"] = row.postprocess_chain
                else:
                    assert isinstance(row, PlaylistConfig)
                    excluded = {
                        "ref",
                        "retention_policy",
                        "requires_cookie_reentry",
                        "requires_secret_reentry",
                        "requires_url_reentry",
                    }
                    if row.requires_secret_reentry and existing is not None:
                        excluded.add("ytdlp_args")
                    if row.requires_url_reentry and existing is not None:
                        excluded.add("url")
                    values = _model_values(
                        row,
                        exclude=excluded,
                    )
                    values["retention_policy_json"] = row.retention_policy
                    if (
                        row.requires_cookie_reentry
                        and (existing is None or existing.cookies_encrypted is None)
                    ) or row.requires_secret_reentry or row.requires_url_reentry:
                        values["paused"] = True
                        values["cookie_enabled"] = False
                    elif row.requires_cookie_reentry:
                        values["cookie_enabled"] = True
                if operation.action == "overwrite":
                    assert existing is not None
                    for key, value in values.items():
                        setattr(existing, key, value)
                    obj = existing
                    overwritten += 1
                else:
                    obj = model(**values)
                    session.add(obj)
                    created += 1
                session.flush()
                mapped[row.ref] = obj
        for assignment_row in document.playlist_profiles:
            operation = next(operations)
            playlist = mapped[assignment_row.playlist_ref]
            profile = mapped[assignment_row.profile_ref]
            storage = mapped[assignment_row.storage_ref]
            existing = session.scalar(
                select(PlaylistProfile).where(
                    PlaylistProfile.playlist_id == playlist.id,
                    PlaylistProfile.profile_id == profile.id,
                )
            )
            if operation.action == "skip":
                skipped += 1
                continue
            values = {
                "playlist_id": playlist.id,
                "profile_id": profile.id,
                "storage_id": storage.id,
                "subpath": assignment_row.subpath,
                "enabled": assignment_row.enabled,
                "sort_order": assignment_row.sort_order,
            }
            if operation.action == "overwrite":
                assert existing is not None
                for key, value in values.items():
                    setattr(existing, key, value)
                overwritten += 1
            else:
                session.add(PlaylistProfile(**values))
                created += 1
            session.flush()
        for key in sorted(document.settings):
            operation = next(operations)
            if operation.action == "skip":
                skipped += 1
                continue
            value = core_settings.validate_override(key, document.settings[key])
            encoded = json.dumps(value, ensure_ascii=False)
            existing = session.get(Setting, key)
            if existing is None:
                session.add(Setting(key=key, value_json=encoded))
                created += 1
            else:
                existing.value_json = encoded
                overwritten += 1
        session.commit()
    except Exception:
        session.rollback()
        raise
    return ConfigImportResult(created, overwritten, skipped)


class ConfigImportSigner:
    """import文書と差分snapshotを改ざん不可・期限付きtokenにする。"""

    def __init__(self, secret_key: str) -> None:
        self._serializer = URLSafeSerializer(secret_key, salt="sluicery-config-import-v1")

    def issue(self, document: ConfigDocument, plan: ConfigImportPlan) -> str:
        return self._serializer.dumps(
            {
                "document": document.model_dump(mode="json"),
                "mode": plan.mode,
                "fingerprint": plan.fingerprint,
                "issued_at": datetime.now(UTC).timestamp(),
            }
        )

    def load(
        self, token: str, *, ttl_sec: int = IMPORT_CONFIRMATION_TTL_SEC
    ) -> tuple[ConfigDocument, ConfigImportPlan]:
        try:
            payload = self._serializer.loads(token)
            issued_at = datetime.fromtimestamp(float(payload["issued_at"]), UTC)
            document = ConfigDocument.model_validate(payload["document"])
            mode: CollisionMode = payload["mode"]
            fingerprint = str(payload["fingerprint"])
        except (BadSignature, KeyError, TypeError, ValueError, ValidationError) as exc:
            raise ConfigImportConfirmationError("import確認tokenが不正です") from exc
        if mode not in {"skip", "overwrite", "create"}:
            raise ConfigImportConfirmationError("import確認tokenが不正です")
        age = (datetime.now(UTC) - issued_at).total_seconds()
        if age < 0 or age > ttl_sec:
            raise ConfigImportConfirmationError(
                "import確認の有効期限が切れました。再確認してください"
            )
        return document, ConfigImportPlan(mode, (), fingerprint)


__all__ = [
    "CollisionMode",
    "ConfigDocument",
    "ConfigImportConfirmationError",
    "ConfigImportPlan",
    "ConfigImportResult",
    "ConfigImportSigner",
    "ConfigTransferError",
    "IMPORT_CONFIRMATION_TTL_SEC",
    "MAX_CONFIG_BYTES",
    "apply_config_import",
    "dump_config_yaml",
    "export_config",
    "load_config_yaml",
    "preview_config_import",
]
