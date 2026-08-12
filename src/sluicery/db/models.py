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
    Boolean,
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
from sqlalchemy.types import TypeDecorator

from sluicery.db.crypto import EncryptedJSON


class UTCDateTime(TypeDecorator):
    """UTC aware な datetime を往復させる（§3.3）。

    SQLite の DateTime(timezone=True) は書き込み時に tzinfo を無言で
    捨て、読み出し時も naive datetime を返す（SQLAlchemy+SQLite の既知の
    制約）。このカラム型はアプリ側で明示的に UTC を付け外しすることで、
    要件定義 §3.3・テスト §9.2「保存した値が UTC aware で読み出せる」を
    実際に満たす。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive な datetime は保存できません（UTC aware にすること）")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


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

    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )


def _enum_column(
    enum_cls: type[enum.Enum],
    *,
    name: str,
    nullable: bool = False,
    default: enum.Enum | None = None,
):
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


class StorageKind(enum.StrEnum):
    LOCAL = "local"
    REMOTE = "remote"
    MOUNT = "mount"


class ProfileKind(enum.StrEnum):
    VIDEO = "video"
    MUSIC = "music"
    OTHER = "other"


class LayoutStrategy(enum.StrEnum):
    FLAT = "flat"
    CUSTOM = "custom"


class PlaylistKindHint(enum.StrEnum):
    VIDEO = "video"
    MUSIC = "music"
    MIXED = "mixed"


class ItemMembership(enum.StrEnum):
    ACTIVE = "active"
    DELISTED = "delisted"


class TargetStatus(enum.StrEnum):
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


class ArtifactRole(enum.StrEnum):
    SOURCE = "source"
    DERIVED = "derived"


class TaskType(enum.StrEnum):
    DISCOVER = "discover"
    DOWNLOAD = "download"
    VERIFY = "verify"
    POSTPROCESS = "postprocess"
    PUBLISH = "publish"
    INDEX = "index"
    INTEGRITY_CHECK = "integrity_check"
    RETENTION = "retention"
    UPDATE_YTDLP = "update_ytdlp"
    # Phase 6 のキュー検証専用。worker.enable_test_tasks=false（既定）では
    # CLI から投入できず、ワーカーにもハンドラを登録しない。
    NOOP = "noop"
    SLEEP = "sleep"
    FAIL = "fail"
    FAIL_UNAVAILABLE = "fail_unavailable"
    FAIL_BLOCKED = "fail_blocked"
    SPAWN = "spawn"


class WorkerClass(enum.StrEnum):
    NETWORK = "network"
    COMPUTE = "compute"


class TaskStatus(enum.StrEnum):
    """要件定義に値の指定がないため実装時に確定（差分は基本設計 §3 に記録）。"""

    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class RunTrigger(enum.StrEnum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    API = "api"


class RunStatus(enum.StrEnum):
    """要件定義に列挙の明記はないが `run.status` に必要なため追加（基本設計 §3 に記録）。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class YtdlpReleaseSource(enum.StrEnum):
    """Phase 3 で新設（要件定義 §5.3 の更新履歴記録に対応。基本設計 §3 に記録）。"""

    INITIAL = "initial"
    MANUAL = "manual"
    AUTO = "auto"


class YtdlpReleaseStatus(enum.StrEnum):
    INSTALLED = "installed"
    ACTIVE = "active"
    REMOVED = "removed"


# ---- テーブル ----


class User(Base, TimestampMixin):
    """単一ユーザー。2件目の作成はリポジトリ層で禁止する（`UserRepository.create_single()`）。"""

    __tablename__ = "user"
    __table_args__ = (UniqueConstraint("username"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Storage(Base, TimestampMixin):
    __tablename__ = "storage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[StorageKind] = _enum_column(StorageKind, name="storage_kind")
    enabled: Mapped[bool] = mapped_column(default=True)
    config_json: Mapped[dict | None] = mapped_column(JSON)
    credentials_encrypted: Mapped[dict | None] = mapped_column(EncryptedJSON())
    last_check_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_check_result_json: Mapped[dict | None] = mapped_column(JSON)


class Profile(Base, TimestampMixin):
    __tablename__ = "profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(1000))
    kind: Mapped[ProfileKind] = _enum_column(ProfileKind, name="profile_kind")
    ytdlp_args: Mapped[str | None] = mapped_column(String(4000))
    format_selector: Mapped[str | None] = mapped_column(String(1000))
    output_template: Mapped[str | None] = mapped_column(String(1000))
    layout_strategy: Mapped[LayoutStrategy] = _enum_column(
        LayoutStrategy, name="profile_layout_strategy", default=LayoutStrategy.FLAT
    )
    audio_extract: Mapped[bool | None] = mapped_column(nullable=True)
    audio_format: Mapped[str | None] = mapped_column(String(50))
    audio_quality: Mapped[str | None] = mapped_column(String(50))
    container: Mapped[str | None] = mapped_column(String(50))
    embed_metadata: Mapped[bool | None] = mapped_column(nullable=True)
    embed_thumbnail: Mapped[bool | None] = mapped_column(nullable=True)
    embed_chapters: Mapped[bool | None] = mapped_column(nullable=True)
    subtitle_langs: Mapped[str | None] = mapped_column(String(255))
    subtitle_auto: Mapped[bool | None] = mapped_column(nullable=True)
    subtitle_embed: Mapped[bool | None] = mapped_column(nullable=True)
    expert_mode: Mapped[bool] = mapped_column(default=False)
    allow_exec: Mapped[bool] = mapped_column(default=False)
    concurrent_fragments: Mapped[int | None] = mapped_column(Integer)
    postprocess_chain_json: Mapped[dict | None] = mapped_column(JSON)


class Playlist(Base, TimestampMixin):
    __tablename__ = "playlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))
    folder_name: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(2000))
    enabled: Mapped[bool] = mapped_column(default=True)
    kind_hint: Mapped[PlaylistKindHint] = _enum_column(PlaylistKindHint, name="playlist_kind_hint")
    ytdlp_args: Mapped[str | None] = mapped_column(String(4000))
    discover_cron: Mapped[str | None] = mapped_column(String(100))
    download_cron: Mapped[str | None] = mapped_column(String(100))
    paused: Mapped[bool] = mapped_column(default=False)
    retention_policy_json: Mapped[dict | None] = mapped_column(JSON)
    dedup_hardlink: Mapped[bool] = mapped_column(default=False)
    last_discover_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_download_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class PlaylistProfile(Base, TimestampMixin):
    """Playlist と Profile の関連。出力先はこの組に紐づく。"""

    __tablename__ = "playlist_profile"
    __table_args__ = (UniqueConstraint("playlist_id", "profile_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    playlist_id: Mapped[int] = mapped_column(ForeignKey("playlist.id"))
    profile_id: Mapped[int] = mapped_column(ForeignKey("profile.id"))
    storage_id: Mapped[int] = mapped_column(ForeignKey("storage.id"))
    subpath: Mapped[str] = mapped_column(String(1000), default="{playlist.folder_name}")
    enabled: Mapped[bool] = mapped_column(default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Item(Base, TimestampMixin):
    """Playlist 配下の論理的な1本。ファイルパスを持たない。"""

    __tablename__ = "item"
    __table_args__ = (
        UniqueConstraint("playlist_id", "source_id"),
        Index("ix_item_playlist_id_membership", "playlist_id", "membership"),
        Index("ix_item_source_id", "source_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    playlist_id: Mapped[int] = mapped_column(ForeignKey("playlist.id"))
    source_id: Mapped[str] = mapped_column(String(255))
    source_url: Mapped[str] = mapped_column(String(2000))
    title: Mapped[str | None] = mapped_column(String(1000))
    uploader: Mapped[str | None] = mapped_column(String(500))
    duration: Mapped[int | None] = mapped_column(Integer)
    upload_date: Mapped[str | None] = mapped_column(String(20))
    playlist_index: Mapped[int | None] = mapped_column(Integer)
    membership: Mapped[ItemMembership] = _enum_column(
        ItemMembership, name="item_membership", default=ItemMembership.ACTIVE
    )
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow)
    delisted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Target(Base, TimestampMixin):
    """Item x Profile。取得状態を持つ。"""

    __tablename__ = "target"
    __table_args__ = (
        UniqueConstraint("item_id", "playlist_profile_id"),
        Index("ix_target_status", "status"),
        Index("ix_target_playlist_profile_id_status", "playlist_profile_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("item.id", ondelete="CASCADE"))
    playlist_profile_id: Mapped[int] = mapped_column(ForeignKey("playlist_profile.id"))
    status: Mapped[TargetStatus] = _enum_column(
        TargetStatus, name="target_status", default=TargetStatus.PENDING
    )
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(4000))
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    blocked_reason: Mapped[str | None] = mapped_column(String(1000))
    downloaded_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Artifact(Base, TimestampMixin):
    """実体ファイル。1 Target に複数存在しうる。現バージョンで生成されるのは role=source のみ。"""

    __tablename__ = "artifact"
    __table_args__ = (
        UniqueConstraint("target_id", "role", "storage_id", "relative_path"),
        Index("ix_artifact_storage_id", "storage_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("target.id", ondelete="CASCADE"))
    role: Mapped[ArtifactRole] = _enum_column(
        ArtifactRole, name="artifact_role", default=ArtifactRole.SOURCE
    )
    storage_id: Mapped[int] = mapped_column(ForeignKey("storage.id"))
    relative_path: Mapped[str] = mapped_column(String(2000))
    absolute_path_cache: Mapped[str | None] = mapped_column(String(2000))
    container: Mapped[str | None] = mapped_column(String(50))
    format_id: Mapped[str | None] = mapped_column(String(100))
    video_codec: Mapped[str | None] = mapped_column(String(100))
    audio_codec: Mapped[str | None] = mapped_column(String(100))
    filesize: Mapped[int | None] = mapped_column(Integer)
    duration: Mapped[int | None] = mapped_column(Integer)
    checksum: Mapped[str | None] = mapped_column(String(255))
    produced_by_task_id: Mapped[int | None] = mapped_column(ForeignKey("task.id"))
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    missing_since: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Task(Base):
    """`created_at` を要件定義の論理設計にない列として追加する（claim_next の順序決定に必要。
    基本設計 §3 に記録）。`updated_at` は追加しない（要件になく用途もないため）。
    """

    __tablename__ = "task"
    __table_args__ = (
        Index(
            "ix_task_claim",
            "status",
            "worker_class",
            "priority",
            "scheduled_at",
            "available_at",
            "blocked_until",
        ),
        Index("ix_task_depends_on_task_id", "depends_on_task_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[TaskType] = _enum_column(TaskType, name="task_type")
    target_ref_type: Mapped[str] = mapped_column(String(50))
    target_ref_id: Mapped[int] = mapped_column(Integer)
    payload_json: Mapped[dict | None] = mapped_column(JSON)
    worker_class: Mapped[WorkerClass] = _enum_column(WorkerClass, name="task_worker_class")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[TaskStatus] = _enum_column(
        TaskStatus, name="task_status", default=TaskStatus.PENDING
    )
    depends_on_task_id: Mapped[int | None] = mapped_column(ForeignKey("task.id"))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    scheduled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    available_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    blocked_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    blocked_reason: Mapped[str | None] = mapped_column(String(4000))
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    worker_id: Mapped[str | None] = mapped_column(String(255))
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(String(4000))
    log_excerpt: Mapped[str | None] = mapped_column(String(4000))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("run.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow)


class Run(Base):
    __tablename__ = "run"
    __table_args__ = (Index("ix_run_started_at", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger: Mapped[RunTrigger] = _enum_column(RunTrigger, name="run_trigger")
    kind: Mapped[str] = mapped_column(String(50))
    playlist_id: Mapped[int | None] = mapped_column(ForeignKey("playlist.id"))
    status: Mapped[RunStatus] = _enum_column(
        RunStatus, name="run_status", default=RunStatus.RUNNING
    )
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    stats_json: Mapped[dict | None] = mapped_column(JSON)
    log_path: Mapped[str | None] = mapped_column(String(2000))


class Setting(Base):
    """運用パラメータの上書き値のみを保持する（既定値はコード側、core.settings 参照）。"""

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value_json: Mapped[str] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class YtdlpRelease(Base, TimestampMixin):
    """yt-dlp の venv 導入・切替履歴（要件定義 §5.3、Phase 3 で新設。基本設計 §3 に記録）。

    `active` は高々1件（切替時に旧アクティブ行を `installed` に戻す）。この制約は
    DB レベルでは強制せず、`downloader/version.py` の切替処理で担保する
    （`user` の2件目禁止と同じ考え方、D-009）。
    """

    __tablename__ = "ytdlp_release"
    __table_args__ = (
        UniqueConstraint("version"),
        Index("ix_ytdlp_release_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[str] = mapped_column(String(100))
    installed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow)
    source: Mapped[YtdlpReleaseSource] = _enum_column(
        YtdlpReleaseSource, name="ytdlp_release_source"
    )
    status: Mapped[YtdlpReleaseStatus] = _enum_column(
        YtdlpReleaseStatus, name="ytdlp_release_status", default=YtdlpReleaseStatus.INSTALLED
    )
    activated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    deactivated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    smoketest_result_json: Mapped[dict | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(String(1000))


class EventLog(Base):
    """フック発火の記録（拡張点の動作確認用）。イミュータブルなので updated_at は持たない。"""

    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(100))
    payload_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utcnow)
    delivered_json: Mapped[dict | None] = mapped_column(JSON)


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
    "YtdlpReleaseSource",
    "YtdlpReleaseStatus",
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
    "YtdlpRelease",
]
