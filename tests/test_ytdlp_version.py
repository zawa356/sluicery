from __future__ import annotations

from pathlib import Path

import pytest

from sluicery.db.models import YtdlpReleaseSource, YtdlpReleaseStatus
from sluicery.downloader import version as version_mod
from sluicery.downloader.version import (
    CurrentVersionRemovalError,
    InstallStatus,
    UnknownVersionError,
    get_status,
    install,
    list_versions,
    prune_old_versions,
    read_current_version,
    remove,
    use,
    ytdlp_root,
)

_REAL_PIP_INSTALL_YTDLP = version_mod._pip_install_ytdlp


def test_pip_install_requests_default_extras(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def _capture_run(command: list[str], **_kwargs: object) -> None:
        calls.append(command)

    monkeypatch.setattr(version_mod.subprocess, "run", _capture_run)

    _REAL_PIP_INSTALL_YTDLP(tmp_path, "2026.07.04")

    assert calls == [
        [str(tmp_path / "bin" / "pip"), "install", "yt-dlp[default]==2026.07.04"]
    ]


@pytest.fixture(autouse=True)
def _fake_venv_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """実際の venv 作成・pip install・yt-dlp 実行をしない疑似実装に差し替える。

    `--version` の実行結果を正しく検証したいので、`bin/yt-dlp` 自体は
    実行可能な小さいシェルスクリプトとして書き出す（get_status() は本物の
    subprocess 呼び出しのまま検証する）。
    """

    def _fake_create_venv(path: Path) -> None:
        (path / "bin").mkdir(parents=True)

    def _fake_pip_install(venv_dir: Path, version: str | None) -> None:
        if version is not None:
            (venv_dir / ".requested_version").write_text(version)

    def _fake_probe_version(venv_dir: Path) -> str:
        marker = venv_dir / ".requested_version"
        resolved = marker.read_text() if marker.exists() else "9999.01.01"
        bin_path = venv_dir / "bin" / "yt-dlp"
        bin_path.write_text(f"#!/bin/sh\necho {resolved}\n")
        bin_path.chmod(0o755)
        return resolved

    monkeypatch.setattr(version_mod, "_create_venv", _fake_create_venv)
    monkeypatch.setattr(version_mod, "_pip_install_ytdlp", _fake_pip_install)
    monkeypatch.setattr(version_mod, "_probe_version", _fake_probe_version)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return ytdlp_root(tmp_path)


def test_status_not_installed_when_no_current(root: Path) -> None:
    result = get_status(root)
    assert result.status == InstallStatus.NOT_INSTALLED
    assert result.current_version is None


def test_status_broken_when_install_contract_is_missing(root: Path, db_session) -> None:
    install(root, db_session, version="2026.01.01", source=YtdlpReleaseSource.MANUAL)
    version_mod._install_contract_path(version_mod.current_link(root)).unlink()

    result = get_status(root)

    assert result.status == InstallStatus.BROKEN
    assert result.current_version == "2026.01.01"


def test_install_repairs_current_with_old_contract(root: Path, db_session) -> None:
    install(root, db_session, version="2026.01.01", source=YtdlpReleaseSource.MANUAL)
    contract = version_mod._install_contract_path(version_mod.current_link(root))
    contract.unlink()
    assert get_status(root).status == InstallStatus.BROKEN

    install(root, db_session, version="2026.01.01", source=YtdlpReleaseSource.MANUAL)

    assert get_status(root).status == InstallStatus.READY
    assert contract.read_text(encoding="utf-8").strip() == version_mod.INSTALL_CONTRACT_VERSION


def test_first_install_bootstraps_and_activates(root: Path, db_session) -> None:
    release = install(root, db_session, version="2026.01.01", source=YtdlpReleaseSource.MANUAL)
    assert release.status == YtdlpReleaseStatus.ACTIVE
    assert read_current_version(root) == "2026.01.01"

    result = get_status(root)
    assert result.status == InstallStatus.READY
    assert result.current_version == "2026.01.01"
    assert result.version_output == "2026.01.01"


def test_second_explicit_install_does_not_switch_current(root: Path, db_session) -> None:
    install(root, db_session, version="2026.02.01", source=YtdlpReleaseSource.MANUAL)
    install(root, db_session, version="2026.01.01", source=YtdlpReleaseSource.MANUAL)

    assert read_current_version(root) == "2026.02.01"
    versions = {r.version for r in list_versions(db_session)}
    assert versions == {"2026.01.01", "2026.02.01"}


def test_install_already_present_is_noop_without_force(root: Path, db_session) -> None:
    install(root, db_session, version="2026.01.01", source=YtdlpReleaseSource.MANUAL)
    marker = version_mod.versions_dir(root) / "2026.01.01" / "bin" / "yt-dlp"
    original_mtime = marker.stat().st_mtime

    install(root, db_session, version="2026.01.01", source=YtdlpReleaseSource.MANUAL)
    assert marker.stat().st_mtime == original_mtime


def test_install_force_replaces_existing_directory(root: Path, db_session) -> None:
    """--force は既存の同名バージョンディレクトリを新しい venv で置き換える。

    実機検証（docs/phase3_指示書.md §11.2 #18: broken からの復旧）で、
    force=True でも「既存があれば新しい方を捨てて既存を採用する」通常経路が
    そのまま使われ、broken な既存ディレクトリを上書きできないバグが見つかった。
    """
    import time

    install(root, db_session, version="2026.01.01", source=YtdlpReleaseSource.MANUAL)
    marker = version_mod.versions_dir(root) / "2026.01.01" / "bin" / "yt-dlp"
    original_mtime = marker.stat().st_mtime
    time.sleep(0.01)

    install(root, db_session, version="2026.01.01", source=YtdlpReleaseSource.MANUAL, force=True)
    assert marker.stat().st_mtime > original_mtime


def test_use_switches_current_and_deactivates_old(root: Path, db_session) -> None:
    install(root, db_session, version="2026.02.01", source=YtdlpReleaseSource.MANUAL)
    install(root, db_session, version="2026.01.01", source=YtdlpReleaseSource.MANUAL)

    use(root, db_session, "2026.01.01")
    assert read_current_version(root) == "2026.01.01"

    releases = {r.version: r for r in list_versions(db_session)}
    assert releases["2026.01.01"].status == YtdlpReleaseStatus.ACTIVE
    assert releases["2026.02.01"].status == YtdlpReleaseStatus.INSTALLED
    assert releases["2026.02.01"].deactivated_at is not None


def test_use_unknown_version_raises(root: Path, db_session) -> None:
    install(root, db_session, version="2026.01.01", source=YtdlpReleaseSource.MANUAL)
    with pytest.raises(UnknownVersionError):
        use(root, db_session, "9999.99.99")


def test_remove_refuses_current_version(root: Path, db_session) -> None:
    install(root, db_session, version="2026.01.01", source=YtdlpReleaseSource.MANUAL)
    with pytest.raises(CurrentVersionRemovalError):
        remove(root, db_session, "2026.01.01")


def test_remove_deletes_directory_and_marks_removed(root: Path, db_session) -> None:
    install(root, db_session, version="2026.02.01", source=YtdlpReleaseSource.MANUAL)
    install(root, db_session, version="2026.01.01", source=YtdlpReleaseSource.MANUAL)

    remove(root, db_session, "2026.01.01")

    assert not (version_mod.versions_dir(root) / "2026.01.01").exists()
    remaining = {r.version for r in list_versions(db_session)}
    assert remaining == {"2026.02.01"}


def test_status_broken_when_current_target_missing(root: Path, db_session) -> None:
    install(root, db_session, version="2026.01.01", source=YtdlpReleaseSource.MANUAL)
    import shutil

    shutil.rmtree(version_mod.versions_dir(root) / "2026.01.01")

    result = get_status(root)
    assert result.status == InstallStatus.BROKEN
    assert result.current_version == "2026.01.01"


def test_install_recovers_from_broken_current(root: Path, db_session) -> None:
    install(root, db_session, version="2026.01.01", source=YtdlpReleaseSource.MANUAL)
    import shutil

    shutil.rmtree(version_mod.versions_dir(root) / "2026.01.01")
    assert get_status(root).status == InstallStatus.BROKEN

    install(root, db_session, version="2026.02.01", source=YtdlpReleaseSource.MANUAL, force=True)
    assert get_status(root).status == InstallStatus.READY
    assert read_current_version(root) == "2026.02.01"


def test_prune_keeps_current_and_prior_active(root: Path, db_session) -> None:
    for v in ["2026.01.01", "2026.02.01", "2026.03.01", "2026.04.01"]:
        install(root, db_session, version=v, source=YtdlpReleaseSource.MANUAL)
    # 最初の install（2026.01.01）だけが bootstrap で自動 active 化されている。
    # 以降の explicit install は current を切り替えないため、ここで current は
    # まだ 2026.01.01 のまま。
    assert read_current_version(root) == "2026.01.01"

    use(root, db_session, "2026.03.01")  # current を切替え、2026.01.01 を prior active にする

    removed = prune_old_versions(root, db_session, keep_versions=1)

    remaining = {r.version for r in list_versions(db_session)}
    assert "2026.03.01" in remaining  # current
    assert "2026.01.01" in remaining  # 直前に active だった
    assert removed == ["2026.02.01"]


def test_prune_noop_when_within_keep_versions(root: Path, db_session) -> None:
    install(root, db_session, version="2026.01.01", source=YtdlpReleaseSource.MANUAL)
    removed = prune_old_versions(root, db_session, keep_versions=3)
    assert removed == []
