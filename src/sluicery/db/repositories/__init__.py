"""リポジトリ層（docs/phase2_指示書.md §7）。

CRUD と検索を担う。一般の状態遷移ルールは Phase 7〜8 の `core/` に置くが、
Taskの所有権付き実行状態更新はアトミック性のため `TaskRepository` に閉じ込める。
セッションは外部から受け取る。
"""

from sluicery.db.repositories.artifact import ArtifactRepository
from sluicery.db.repositories.auth_session import AuthSessionRepository
from sluicery.db.repositories.base import BaseRepository
from sluicery.db.repositories.event_log import EventLogRepository
from sluicery.db.repositories.item import ItemRepository
from sluicery.db.repositories.playlist import PlaylistRepository
from sluicery.db.repositories.playlist_profile import PlaylistProfileRepository
from sluicery.db.repositories.profile import ProfileRepository
from sluicery.db.repositories.run import RunRepository
from sluicery.db.repositories.setting import SettingRepository
from sluicery.db.repositories.storage import StorageRepository
from sluicery.db.repositories.target import TargetRepository
from sluicery.db.repositories.task import TaskRepository
from sluicery.db.repositories.user import DuplicateUserError, UserRepository
from sluicery.db.repositories.ytdlp_release import YtdlpReleaseRepository

__all__ = [
    "ArtifactRepository",
    "BaseRepository",
    "DuplicateUserError",
    "EventLogRepository",
    "ItemRepository",
    "PlaylistProfileRepository",
    "PlaylistRepository",
    "ProfileRepository",
    "RunRepository",
    "SettingRepository",
    "StorageRepository",
    "TargetRepository",
    "TaskRepository",
    "UserRepository",
    "AuthSessionRepository",
    "YtdlpReleaseRepository",
]
