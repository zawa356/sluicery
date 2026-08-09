"""venv を一時ディレクトリから最終ディレクトリへリネームした際の回帰テスト。

実機検証（docs/phase3_指示書.md §11.2 #4）で、pip の console_scripts が
シェバンにインストール時の絶対パスを焼き込むため、`versions/.tmp-<uuid>/`
で作った venv を `versions/<version>/` にリネームすると `bin/yt-dlp` が
`no such file or directory` で実行できなくなるバグが見つかった
（`_relocate_shebangs()` で修正）。ここではモックではなく実際に
`python -m venv` を使い、同じ再現条件で検証する（ネットワークは使わない）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sluicery.db.models import YtdlpReleaseSource
from sluicery.downloader import version as version_mod
from sluicery.downloader.version import InstallStatus, get_status, install, ytdlp_root


def _fake_pip_install_with_baked_in_shebang(venv_dir: Path, version: str | None) -> None:
    """pip install の代わりに、その場のパスをシェバンに焼き込んだ
    console_script 相当のファイルを直接生成する（ネットワーク不使用）。
    """
    script = venv_dir / "bin" / "yt-dlp"
    script.write_text(f"#!{venv_dir}/bin/python3\nprint('9999.09.09')\n")
    script.chmod(0o755)


@pytest.fixture(autouse=True)
def _fake_pip_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # _create_venv は実物を使う（python -m venv、ネットワーク不要）。
    monkeypatch.setattr(version_mod, "_pip_install_ytdlp", _fake_pip_install_with_baked_in_shebang)
    monkeypatch.setattr(version_mod, "_probe_version", lambda venv_dir: "9999.09.09")


def test_yt_dlp_script_executable_after_rename(tmp_path: Path, db_session) -> None:
    root = ytdlp_root(tmp_path)
    install(root, db_session, version="9999.09.09", source=YtdlpReleaseSource.MANUAL)

    result = get_status(root)
    assert result.status == InstallStatus.READY, result
    assert result.version_output == "9999.09.09"
