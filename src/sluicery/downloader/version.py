"""yt-dlp の venv 管理（要件定義 §5.2, docs/phase3_指示書.md §2）。

- yt-dlp は永続 volume 上の venv にインストールする。イメージには焼き込まない
- インストール・切替・削除を行うのは `app` サービスのみ。`worker-*` は
  `current` symlink 越しに読み取り専用でアクセスする（§2.4）
- symlink の差し替えは `os.symlink` + `os.replace` で原子的に行う（§2.3）。
  `os.remove` してから `os.symlink` する実装は、その間に別プロセスが
  `current` を参照すると失敗するため禁止
"""

from __future__ import annotations

import fcntl
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from sluicery.db.models import YtdlpRelease, YtdlpReleaseSource, YtdlpReleaseStatus
from sluicery.db.repositories.ytdlp_release import YtdlpReleaseRepository

# venv 作成・pip install の上限(要件定義に明示のタイムアウト設定キーがないため
# 固定値とする。§3.4 の `ytdlp.*_timeout_sec` 系は yt-dlp 本体の実行用)。
INSTALL_TIMEOUT_SEC = 600
VERSION_PROBE_TIMEOUT_SEC = 30


class InstallStatus(StrEnum):
    """導入状態(§2.5)。"""

    READY = "ready"
    NOT_INSTALLED = "not_installed"
    BROKEN = "broken"


class YtdlpInstallError(RuntimeError):
    """venv 作成・pip install・バージョン確認のいずれかが失敗した。"""


class UnknownVersionError(RuntimeError):
    """指定されたバージョンが導入済み一覧に存在しない。"""


class CurrentVersionRemovalError(RuntimeError):
    """`current` が指すバージョンを削除しようとした。"""


@dataclass(frozen=True)
class StatusResult:
    status: InstallStatus
    current_version: str | None
    version_output: str | None


def ytdlp_root(data_dir: Path) -> Path:
    return data_dir / "ytdlp"


def versions_dir(root: Path) -> Path:
    return root / "versions"


def current_link(root: Path) -> Path:
    return root / "current"


def lock_path(root: Path) -> Path:
    return root / ".lock"


def current_ytdlp_bin(root: Path) -> Path:
    """`current` 越しにアクセスする実行ファイルパス。バージョン番号を意識しない。"""
    return current_link(root) / "bin" / "yt-dlp"


@contextmanager
def _locked(root: Path) -> Iterator[None]:
    """3サービス同時実行下でも venv 操作を直列化する(§2.4)。"""
    root.mkdir(parents=True, exist_ok=True)
    with lock_path(root).open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def read_current_version(root: Path) -> str | None:
    """`current` symlink の参照先バージョンを返す。symlink が無ければ None。

    symlink がダングリング(参照先が存在しない)でも、参照先の名前は返す
    (`broken` 状態の表示に使うため)。
    """
    link = current_link(root)
    if not link.is_symlink():
        return None
    return Path(link.readlink()).name


def get_status(root: Path) -> StatusResult:
    """3値の導入状態を判定する(§2.5)。"""
    link = current_link(root)
    if not link.is_symlink():
        return StatusResult(InstallStatus.NOT_INSTALLED, None, None)

    version = read_current_version(root)
    bin_path = current_ytdlp_bin(root)
    try:
        proc = subprocess.run(
            [str(bin_path), "--version"],
            capture_output=True,
            text=True,
            timeout=VERSION_PROBE_TIMEOUT_SEC,
            env={"LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return StatusResult(InstallStatus.BROKEN, version, None)

    if proc.returncode != 0:
        return StatusResult(InstallStatus.BROKEN, version, None)
    return StatusResult(InstallStatus.READY, version, proc.stdout.strip())


def list_versions(session: Session) -> list[YtdlpRelease]:
    """導入済み(`removed` を除く)バージョンを新しい順で返す。"""
    return YtdlpReleaseRepository(session).list_installed()


def _format_subprocess_error(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        stderr = (exc.stderr or "").strip()
        tail = stderr[-2000:]
        return f"コマンドが失敗しました(終了コード {exc.returncode}): {tail}"
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"コマンドがタイムアウトしました({exc.timeout}秒)"
    return str(exc)


def _create_venv(path: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "venv", str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=INSTALL_TIMEOUT_SEC,
    )


def _pip_install_ytdlp(venv_dir: Path, version: str | None) -> None:
    pip_path = venv_dir / "bin" / "pip"
    spec = "yt-dlp" if version is None else f"yt-dlp=={version}"
    subprocess.run(
        [str(pip_path), "install", spec],
        check=True,
        capture_output=True,
        text=True,
        timeout=INSTALL_TIMEOUT_SEC,
    )


def _probe_version(venv_dir: Path) -> str:
    bin_path = venv_dir / "bin" / "yt-dlp"
    proc = subprocess.run(
        [str(bin_path), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=VERSION_PROBE_TIMEOUT_SEC,
        env={"LC_ALL": "C"},
    )
    return proc.stdout.strip()


def _record_install(session: Session, version: str, source: YtdlpReleaseSource) -> YtdlpRelease:
    repo = YtdlpReleaseRepository(session)
    existing = repo.get_by_version(version)
    if existing is not None:
        if existing.status == YtdlpReleaseStatus.REMOVED:
            existing.status = YtdlpReleaseStatus.INSTALLED
            existing.installed_at = datetime.now(UTC)
            session.commit()
        return existing
    return repo.create(version=version, source=source, status=YtdlpReleaseStatus.INSTALLED)


def _switch_symlink(root: Path, version: str) -> None:
    """`os.symlink` + `os.replace` で `current` を原子的に差し替える(§2.3)。"""
    target = versions_dir(root) / version
    tmp_link = root / f".current-{uuid4().hex}"
    tmp_link.symlink_to(target)
    tmp_link.replace(current_link(root))


def _activate(root: Path, session: Session, release: YtdlpRelease) -> None:
    """symlink を切り替え、DB 上も `active` を1件に保つ。"""
    _switch_symlink(root, release.version)

    repo = YtdlpReleaseRepository(session)
    old_active = repo.get_active()
    if old_active is not None and old_active.id != release.id:
        old_active.status = YtdlpReleaseStatus.INSTALLED
        old_active.deactivated_at = datetime.now(UTC)
    release.status = YtdlpReleaseStatus.ACTIVE
    release.activated_at = datetime.now(UTC)
    session.commit()


def install(
    root: Path,
    session: Session,
    *,
    version: str | None = None,
    source: YtdlpReleaseSource = YtdlpReleaseSource.MANUAL,
    force: bool = False,
) -> YtdlpRelease:
    """yt-dlp を導入する(§2.2)。

    導入するだけで `use` のような明示的な切替はしない。ただし `current` が
    まだ存在しない(初回導入・または `broken` からの復旧)場合に限り、導入した
    バージョンをそのまま `current` にする(そうしないと `app` が永遠に
    `not_installed` のままになる)。
    """
    with _locked(root):
        versions_dir(root).mkdir(parents=True, exist_ok=True)

        if version is not None and not force:
            existing_dir = versions_dir(root) / version
            if existing_dir.is_dir():
                repo = YtdlpReleaseRepository(session)
                release = repo.get_by_version(version)
                if release is not None and release.status != YtdlpReleaseStatus.REMOVED:
                    if get_status(root).status != InstallStatus.READY:
                        _activate(root, session, release)
                    return release

        needs_activation = get_status(root).status != InstallStatus.READY

        tmp_dir = versions_dir(root) / f".tmp-{uuid4().hex}"
        try:
            _create_venv(tmp_dir)
            _pip_install_ytdlp(tmp_dir, version)
            installed_version = _probe_version(tmp_dir)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise YtdlpInstallError(_format_subprocess_error(exc)) from exc

        final_dir = versions_dir(root) / installed_version
        if final_dir.is_dir():
            # 既に同名のバージョンが存在する場合は、一時ディレクトリを破棄して
            # 既存を採用する(§2.2 手順5)。
            shutil.rmtree(tmp_dir, ignore_errors=True)
        else:
            tmp_dir.rename(final_dir)

        release = _record_install(session, installed_version, source)
        if needs_activation:
            _activate(root, session, release)
        return release


def use(root: Path, session: Session, version: str) -> YtdlpRelease:
    """導入済みバージョンへ切り替える。未導入バージョンは拒否する。"""
    with _locked(root):
        target_dir = versions_dir(root) / version
        if not target_dir.is_dir():
            raise UnknownVersionError(f"バージョン {version} は導入されていません")

        repo = YtdlpReleaseRepository(session)
        release = repo.get_by_version(version)
        if release is None or release.status == YtdlpReleaseStatus.REMOVED:
            raise UnknownVersionError(f"バージョン {version} は導入されていません")

        _activate(root, session, release)
        return release


def remove(root: Path, session: Session, version: str) -> None:
    """導入済みバージョンを削除する。`current` の削除は拒否する。"""
    with _locked(root):
        if read_current_version(root) == version:
            raise CurrentVersionRemovalError(
                "current のバージョンは削除できません。先に `ytdlp use` で切り替えてください"
            )

        target_dir = versions_dir(root) / version
        if not target_dir.is_dir():
            raise UnknownVersionError(f"バージョン {version} は導入されていません")

        shutil.rmtree(target_dir)

        repo = YtdlpReleaseRepository(session)
        release = repo.get_by_version(version)
        if release is not None:
            release.status = YtdlpReleaseStatus.REMOVED
            session.commit()


def prune_old_versions(root: Path, session: Session, keep_versions: int) -> list[str]:
    """保持世代数(`ytdlp.keep_versions`)を超えた古いバージョンを削除する(§2.6)。

    `current` と、直前に `active` だったバージョンは世代数に関わらず必ず残す
    (Phase 15 のロールバック機構を失わないため)。
    """
    with _locked(root):
        repo = YtdlpReleaseRepository(session)
        installed = repo.list_installed()  # installed_at 降順
        if len(installed) <= keep_versions:
            return []

        protected: set[str] = set()
        current_ver = read_current_version(root)
        if current_ver is not None:
            protected.add(current_ver)

        deactivated = [r for r in installed if r.deactivated_at is not None]
        if deactivated:
            prior_active = max(deactivated, key=lambda r: r.deactivated_at)  # type: ignore[arg-type,return-value]
            protected.add(prior_active.version)

        keep_n = max(keep_versions, len(protected))
        keep_set = {r.version for r in installed[:keep_n]} | protected

        removed: list[str] = []
        for release in installed:
            if release.version in keep_set:
                continue
            target_dir = versions_dir(root) / release.version
            shutil.rmtree(target_dir, ignore_errors=True)
            release.status = YtdlpReleaseStatus.REMOVED
            removed.append(release.version)
        if removed:
            session.commit()
        return removed


__all__ = [
    "INSTALL_TIMEOUT_SEC",
    "VERSION_PROBE_TIMEOUT_SEC",
    "CurrentVersionRemovalError",
    "InstallStatus",
    "StatusResult",
    "UnknownVersionError",
    "YtdlpInstallError",
    "current_link",
    "current_ytdlp_bin",
    "get_status",
    "install",
    "list_versions",
    "lock_path",
    "prune_old_versions",
    "read_current_version",
    "remove",
    "use",
    "versions_dir",
    "ytdlp_root",
]
