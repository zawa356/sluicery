"""SQLAlchemy ORM モデル定義（要件定義 §7 の全テーブル）。

- タイムスタンプは全て UTC aware（§3.3 の方針）。`server_default=func.now()` は
  SQLite で naive になるため使わず、アプリ側（Python の default/onupdate）で生成する
- Enum は Python 側 `Enum`、DB 側は文字列カラム + CHECK 制約とする（§4.2）
- 主キーは整数 autoincrement。`setting` のみ `key` を主キーとする
  （要件定義の論理設計に `id` 列がなく、`key` が自然キーのため。差分は
  `docs/基本設計.md` §3 に記録）
- レコード削除がファイル削除を引き起こす実装はしない（設計原則1）。
  Phase 2 の時点ではファイル操作コード自体を書かないため、リポジトリ層・
  モデル層のどちらにもその種のロジックは存在しない
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from sluicery.db.crypto import EncryptedJSON

# Alembic の batch モードで制約名が必要になるため、Phase 2 で必ず設定する（後から入れるのは困難）。
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata_obj = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    metadata = metadata_obj


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    """`created_at` / `updated_at` を持つ Mixin（§4.5）。"""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


def _enum_column(enum_cls: type[enum.Enum], *, name: str, nullable: bool = False, default: enum.Enum | None = None):
    return mapped_column(
        SAEnum(
            enum_cls,
            name=name,
            native_enum=False,
            create_constraint=True,
            validate_strings=True,
            values_callable=lambda obj: [e.value for e in obj],
        ),
        nullable=nullable,
        default=default,
    )


# ---- Enum（値は要件定義 §7.2 から逐語で取る） ----


class StorageKind(str, enum.Enum):
    LOCAL = "local"
    REMOTE = "remote"
    MOUNT = "mount"


class ProfileKind(str, enum.Enum):
    VIDEO = "video"
    MUSIC = "music"
    OTHER = "other"


class LayoutStrategy(str, enum.Enum):
    FLAT = "flat"
    CUSTOM = "custom"


class PlaylistKindHint(str, enum.Enum):
    VIDEO = "video"
    MUSIC = "music"
    MIXED = "mixed"


class ItemMembership(str, enum.Enum):
    ACTIVE = "active"
    DELISTED = "delisted"


class TargetStatus(str, enum.Enum):
    PENDING = "pending"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    DOWNLOADED = "downloaded"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    MISSING = "missing"
    IGNORED = "ignored"


class ArtifactRole(str, enum.Enum):
    SOURCE = "source"
    DERIVED = "derived"


class TaskType(str, enum.Enum):
    DISCOVER = "discover"
    DOWNLOAD = "download"
    VERIFY = "verify"
    POSTPROCESS = "postprocess"
    PUBLISH = "publish"
    INDEX = "index"
    INTEGRITY_CHECK = "integrity_check"
    RETENTION = "retention"
    UPDATE_YTDLP = "update_ytdlp"


class WorkerClass(str, enum.Enum):
    NETWORK = "network"
    COMPUTE = "compute"


class TaskStatus(str, enum.Enum):
    """要件定義に値の指定がないため実装時に確定（差分は基本設計 §3 に記録）。"""

    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunTrigger(str, enum.Enum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    API = "api"


class RunStatus(str, enum.Enum):
    """要件定義に列挙の明記はないが `run.status` に必要なため追加（基本設計 §3 に記録）。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---- テーブル ----


class User(Base, TimestampMixin):
    """単一ユーザー。2件目の作成はリポジトリ層で禁止する（`UserRepository.create_single()`）。"""

    __tablename__ = "user"
    __table_args__ = (UniqueConstraint("username"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Storage(Base, TimestampMixin):
    __tablename__ = "storage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[StorageKind] = _enum_column(StorageKind, name="storage_kind")
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    config_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    credentials_encrypted: Mapped[dict | None] = mapped_column(EncryptedJSON(), nullable=True)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_check_result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Profile(Base, TimestampMixin):
    __tablename__ = "profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    kind: Mapped[ProfileKind] = _enum_column(ProfileKind, name="profile_kind")
    ytdlp_args: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    format_selector: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    output_template: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    layout_strategy: Mapped[LayoutStrategy] = _enum_column(
        LayoutStrategy, name="profile_layout_strategy", default=LayoutStrategy.FLAT
    )
    audio_extract: Mapped[bool] = mapped_column(default=False, nullable=False)
    audio_format: Mapped[str | None] = mapped_column(String(50), nullable=True)
    audio_quality: Mapped[str | None] = mapped_column(String(50), nullable=True)
    container: Mapped[str | None] = mapped_column(String(50), nullable=True)
    embed_metadata: Mapped[bool] = mapped_column(default=True, nullable=False)
    embed_thumbnail: Mapped[bool] = mapped_column(default=True, nullable=False)
    embed_chapters: Mapped[bool] = mapped_column(default=False, nullable=False)
    subtitle_langs: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subtitle_auto: Mapped[bool] = mapped_column(default=False, nullable=False)
    subtitle_embed: Mapped[bool] = mapped_column(default=False, nullable=False)
    expert_mode: Mapped[bool] = mapped_column(default=False, nullable=False)
    allow_exec: Mapped[bool] = mapped_column(default=False, nullable=False)
    concurrent_fragments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    postprocess_chain_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Playlist(Base, TimestampMixin):
    __tablename__ = "playlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    folder_name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    kind_hint: Mapped[PlaylistKindHint] = _enum_column(PlaylistKindHint, name="playlist_kind_hint")
    ytdlp_args: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    discover_cron: Mapped[str | None] = mapped_column(String(100), nullable=True)
    download_cron: Mapped[str | None] = mapped_column(String(100), nullable=True)
    paused: Mapped[bool] = mapped_column(default=False, nullable=False)
    retention_policy_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    dedup_hardlink: Mapped[bool] = mapped_column(default=False, nullable=False)
    last_discover_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_download_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlaylistProfile(Base, TimestampMixin):
    """Playlist と Profile の関連。出力先はこの組に紐づく。"""

    __tablename__ = "playlist_profile"
    __table_args__ = (UniqueConstraint("playlist_id", "profile_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    playlist_id: Mapped[int] = mapped_column(ForeignKey("playlist.id"), nullable=False)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profile.id"), nullable=False)
    storage_id: Mapped[int] = mapped_column(ForeignKey("storage.id"), nullable=False)
    subpath: Mapped[str] = mapped_column(String(1000), default="{playlist.folder_name}", nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Item(Base, TimestampMixin):
    """Playlist 配下の論理的な1本。ファイルパスを持たない。"""

    __tablename__ = "item"
    __table_args__ = (
        UniqueConstraint("playlist_id", "source_id"),
        Index("ix_item_playlist_id_membership", "playlist_id", "membership"),
        Index("ix_item_source_id", "source_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    playlist_id: Mapped[int] = mapped_column(ForeignKey("playlist.id"), nullable=False)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    title: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    uploader: Mapped[str | None] = mapped_column(String(500), nullable=True)
    duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upload_date: Mapped[str | None] = mapped_column(String(20), nullable=True)
    playlist_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    membership: Mapped[ItemMembership] = _enum_column(
        ItemMembership, name="item_membership", default=ItemMembership.ACTIVE
    )
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    delisted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Target(Base, TimestampMixin):
    """Item x Profile。取得状態を持つ。"""

    __tablename__ = "target"
    __table_args__ = (
        UniqueConstraint("item_id", "playlist_profile_id"),
        Index("ix_target_status", "status"),
        Index("ix_target_playlist_profile_id_status", "playlist_profile_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"), nullable=False)
    playlist_profile_id: Mapped[int] = mapped_column(ForeignKey("playlist_profile.id"), nullable=False)
    status: Mapped[TargetStatus] = _enum_column(TargetStatus, name="target_status", default=TargetStatus.PENDING)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Artifact(Base, TimestampMixin):
    """実体ファイル。1 Target に複数存在しうる。現バージョンで生成されるのは role=source のみ。"""

    __tablename__ = "artifact"
    __table_args__ = (Index("ix_artifact_storage_id", "storage_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("target.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[ArtifactRole] = _enum_column(ArtifactRole, name="artifact_role", default=ArtifactRole.SOURCE)
    storage_id: Mapped[int] = mapped_column(ForeignKey("storage.id"), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(2000), nullable=False)
    absolute_path_cache: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    container: Mapped[str | None] = mapped_column(String(50), nullable=True)
    format_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    video_codec: Mapped[str | None] = mapped_column(String(100), nullable=True)
    audio_codec: Mapped[str | None] = mapped_column(String(100), nullable=True)
    filesize: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(255), nullable=True)
    produced_by_task_id: Mapped[int | None] = mapped_column(ForeignKey("task.id"), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Task(Base):
    """`created_at` を要件定義の論理設計にない列として追加する（claim_next の順序決定に必要。
    基本設計 §3 に記録）。`updated_at` は追加しない（要件になく用途もないため）。
    """

    __tablename__ = "task"
    __table_args__ = (
        Index("ix_task_status_worker_class_priority", "status", "worker_class", "priority"),
        Index("ix_task_depends_on_task_id", "depends_on_task_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[TaskType] = _enum_column(TaskType, name="task_type")
    target_ref_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_ref_id: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    worker_class: Mapped[WorkerClass] = _enum_column(WorkerClass, name="task_worker_class")
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[TaskStatus] = _enum_column(TaskStatus, name="task_status", default=TaskStatus.PENDING)
    depends_on_task_id: Mapped[int | None] = mapped_column(ForeignKey("task.id"), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    log_excerpt: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("run.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class Run(Base):
    __tablename__ = "run"
    __table_args__ = (Index("ix_run_started_at", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger: Mapped[RunTrigger] = _enum_column(RunTrigger, name="run_trigger")
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    playlist_id: Mapped[int | None] = mapped_column(ForeignKey("playlist.id"), nullable=True)
    status: Mapped[RunStatus] = _enum_column(RunStatus, name="run_status", default=RunStatus.RUNNING)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stats_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    log_path: Mapped[str | None] = mapped_column(String(2000), nullable=True)


class Setting(Base):
    """運用パラメータの上書き値のみを保持する（既定値はコード側、core.settings 参照）。"""

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value_json: Mapped[str] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class EventLog(Base):
    """フック発火の記録（拡張点の動作確認用）。イミュータブルなので updated_at は持たない。"""

    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    delivered_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


__all__ = [
    "ArtifactRole",
    "Base",
    "ItemMembership",
    "LayoutStrategy",
    "NAMING_CONVENTION",
    "PlaylistKindHint",
    "ProfileKind",
    "RunStatus",
    "RunTrigger",
    "StorageKind",
    "TargetStatus",
    "TaskStatus",
    "TaskType",
    "WorkerClass",
    "metadata_obj",
    # models
    "Artifact",
    "EventLog",
    "Item",
    "Playlist",
    "PlaylistProfile",
    "Profile",
    "Run",
    "Setting",
    "Storage",
    "Target",
    "Task",
    "User",
]
