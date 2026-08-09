"""`ytdlp fetch` の固定引数の回帰テスト。

実機検証（docs/phase3_指示書.md §11.2 #9）で、`--print` が暗黙的に
`--quiet` を付与し、`--progress-template` による進捗出力まで抑制して
しまうことが判明した（yt-dlp の挙動。`--print` の help に
"Implies --quiet" と明記されている）。`--progress` で明示的に上書き
しないと `on_progress` コールバックが一度も呼ばれない。
"""

from __future__ import annotations

from pathlib import Path

from sluicery.cli import _build_fetch_args


def test_fetch_args_include_progress_to_override_print_implied_quiet() -> None:
    args = _build_fetch_args("https://example.com/video", Path("/data/staging"))

    assert "--progress" in args
    print_idx = args.index("--print")
    progress_idx = args.index("--progress")
    # --print より前（または少なくとも同じコマンドライン中）に --progress が
    # 存在すればよいが、分かりやすさのため位置関係も確認しておく。
    assert progress_idx < print_idx

    assert "--progress-template" in args
    assert args[0] == "https://example.com/video"
