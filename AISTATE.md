# AISTATE

> このファイルはセッション間の引き継ぎ用です。
> セッション開始時に最初に読み、セッション終了時に必ず更新してください。

最終更新: 2026-08-09 11:30
対応コミット: 26d80fb chore: .dockerignore を追加

## プロジェクト概要

sluicery は yt-dlp を用いた自己ホスト型のプレイリスト同期サーバー。
詳細は `docs/要件定義.md`。Phase 3 は `docs/phase3_指示書.md` の指示に基づき実施し、完了した。

## 現在の進捗

要件定義 §20 の実装順序に対する現在地。

- [x] 1. リポジトリ骨格・Docker 環境
- [x] 2. 設定読み込み・DB スキーマ
- [x] 3. yt-dlp venv 管理と CLI ラッパ
- [ ] 4. オプション合成モデル  ← 次の着手点
- [ ] 5. Storage アダプタ（local / remote-rclone）
- [ ] 6. Task キューとワーカー
- [ ] 7. パイプライン（download → verify → postprocess(空) → publish → index）
- [ ] 8. 二相同期（discover / download）、状態遷移
- [ ] 9. 認証、Web UI 骨格
- [ ] 10. Playlist / Profile / Storage の CRUD 画面
- [ ] 11. Run 履歴、進捗表示、ログ閲覧、キャンセル
- [ ] 12. スケジューラ
- [ ] 13. 整合性チェック、relink、手動リンク画面、差分レポート
- [ ] 14. フォーマット検査機能
- [ ] 15. yt-dlp 自動更新 + スモークテスト + ロールバック
- [ ] 16. retention（ドライラン必須）
- [ ] 17. 設定エクスポート / インポート
- [ ] 18. フック機構
- [ ] 19. compose.privileged.yaml、GPU 設定のコメントアウト配置
- [ ] 20. バックアップ / リストア、ドキュメント一式

## 直近の作業

- Phase 3（yt-dlp venv 管理・CLI ラッパ）を完了。実装順序 #3 に対応。
  `docs/phase3_指示書.md` §0 の P0 是正タスク6件（開発依存ロック分離、
  README セットアップ手順修正、worker 待機ループ化、内部設定キー命名規約の
  明記、compose healthcheck 化）と、本編（§1〜§10）を実装した
- 追加モジュール: `downloader/version.py`（venv インストール・切替・削除・
  状態判定・世代整理）、`downloader/ytdlp.py`（CLI ラッパ、プロセスグループ
  制御・タイムアウト）、`downloader/progress.py`（進捗パーサ）、
  `downloader/protocol.py`（出力プレフィックス規約）、`downloader/errors.py`
  （エラー分類ルール）、`db/repositories/ytdlp_release.py`
- CLI に `sluicery ytdlp status/list/install/use/remove/exec/probe/fetch`
  を追加。`probe`/`fetch` は Phase 4 のオプション合成が入るまでの暫定実装
- `app` は yt-dlp 未導入でも起動し（degraded 起動）、`ytdlp.auto_install`
  が true ならバックグラウンドで自動導入する。`worker` は yt-dlp が
  `ready` になるまで待機ループに入り、Phase 6 まで未実装の Task 処理を
  待つ間も終了しない（旧: restart ループになっていた問題の修正でもある）
- テスト69→70件追加（`tests/test_ytdlp_version.py` 等）。yt-dlp 実行を伴う
  ものは `tests/fixtures/fake_ytdlp.py` でモックし、実ネットワークは使わない
- **実機検証（`docs/phase3_指示書.md` §11.2 の20項目）を完了。** 過程で
  ユニットテストでは再現しなかった実バグを3件発見・修正済み（詳細は
  `docs/変更履歴.md`「修正」、コミット `77dcf04` `7c604b4`）：
  1. venv を `versions/.tmp-<uuid>/` で作ってから `versions/<version>/` に
     リネームすると、pip の console_scripts のシェバンが焼き込み済みの
     旧パスを指したままになり実行不能（`broken`）になる
  2. `install --force` が、解決済みバージョン名のディレクトリが既に
     存在する場合に新しい venv を無条件で破棄していたため、`broken` から
     `--force` で復旧できない
  3. `ytdlp fetch` で `--print` が暗黙に `--quiet` を付与し、
     `--progress-template` の進捗出力まで抑制していた（`--progress` で
     明示的に上書きして解消）
- 設計判断は `docs/基本設計.md` §7 に D-011〜D-015 として記録済み。
  未決定事項2件（`target.status` の `blocked` に対応する `TaskStatus` が
  無いこと、コンテナに init が無く孫プロセスの zombie が reap されない
  可能性があること）を「検討事項」として同ファイルに記録
- `docs/phase3_指示書.md` §11.2 の完了条件20項目、全て実機で確認済み

## 次にやること

1. 実装順序 #4（オプション合成モデル、`core/options.py`）に着手
2. 着手前に `docs/要件定義.md` §9（yt-dlp オプションの合成モデル）を再読すること
3. `ytdlp probe`/`fetch` の暫定固定オプション（`downloader/ytdlp.py` の
   呼び出し元、`cli.py` の `_cmd_ytdlp_probe`/`_build_fetch_args`）を
   Phase 4 のレイヤー合成に置き換えること（`docs/phase3_指示書.md` §9.1）
4. **Phase 3.5（`docs/phase3.5_指示書_改訂版.md`、要件定義 §20 の実装順序
   には含まれない独立タスク）に、ユーザーの明示的な指示により着手済み。**
   §0 の P0 是正6件のうち5件が完了（詳細は `docs/変更履歴.md`）。残るは
   §0.6/§6.2 由来の `docs/公開前チェックリスト.md` 新設。この後 §2〜§5
   （README 改訂・deployment.md・troubleshooting.md 新設・VM 実機検証）に
   進むには、ライセンス選定（§6.1、着手前にユーザー確認が必須）と VM
   実機検証環境へのアクセス方法をユーザーに確認する必要がある（未確認）。
   §7 の履歴監査で一度停止し、ユーザーの承認を得るまで push しない

## 未解決・保留

| # | 内容 | 状態 |
|---|---|---|
| 1 | Alembic の `revision --autogenerate` は、SQLite の CHECK 制約比較の既知の制限により、実際の変更がなくても「削除→再作成」の偽陽性 diff を出す（`docs/基本設計.md` D-008）。マイグレーション追加時、生成物からこの偽陽性を手で取り除く必要がある | 恒常的な既知の制限（対応不要、注意事項） |
| 2 | `target.status` には `blocked`（外的要因で保留）があるが `TaskStatus` に対応する値が無い。Phase 6/7 で Storage 到達不能等により Task を保留する際、Task 側の表現を決める必要がある（`docs/基本設計.md` §3 の検討事項） | Phase 6/7 着手時に判断 |
| 3 | コンテナに init プロセスが無く（`compose.yaml` に `init:` 未指定）、`killpg` で終了させた孫プロセス（yt-dlp が起動する ffmpeg 等）が zombie として残り続ける可能性がある（`downloader/ytdlp.py` のプロセスグループ終了テスト作成中に判明。`docs/基本設計.md` §7 の検討事項） | Phase 6（worker の実処理）着手時に評価 |
| 4 | `sluicery ytdlp probe` は generic extractor 対象の URL では `uploader`/`duration` 等が取れない（`formats` は取れる）。YouTube 等のフル対応は未検証 | Phase 4 でオプション合成に置き換える際に再確認 |

## 重要な前提（忘れやすいもの）

- 配信元での削除は絶対にローカルファイルの削除に伝播させない
- `blocked` はリトライ回数を消費しない
- ファイル名の `[<source_id>]` は末尾（拡張子直前）に固定。relink がこれに依存している
- `SECRET_KEY` 未設定時は起動を拒否する
- `SECRET_KEY` はローテーション非対応（鍵紛失・変更時はクレデンシャル再入力。D-004）
- 運用パラメータ（Staging しきい値、cron 式、download.*、ytdlp.* 等）は `.env` ではなく
  `setting` テーブル側。既定値はコード側（`core/settings.py` の `CODE_DEFAULTS`）
- 内部状態（`SECRET_KEY` 指紋等）は `_internal.*` 名前空間に置き `CODE_DEFAULTS` に
  登録しない。`sluicery settings` コマンドや将来の設定エクスポートの対象から自動的に外れる
- DB のタイムスタンプは独自の `UTCDateTime` 型を使う。生の `DateTime(timezone=True)`
  を新しいカラムに使わないこと（SQLite で tzinfo が保持されないバグを踏む）
- `MEDIA_ROOT` 環境変数の値をコンテナ内でファイルパスとして直接使わない
  （ホスト側パスであり、コンテナ内では常に `/mnt/media` に固定。D-010）
- リポジトリ層に状態遷移ロジックを書かない（Phase 7〜8 の `core/` に置く）
- yt-dlp の venv インストール・切替・削除は `app` サービスのみが行う。`worker` は
  `current` symlink 越しに読み取り専用でアクセスする
- venv をリネームしたら `_relocate_shebangs()` を必ず通す（pip の console_scripts は
  再配置可能でない。シェバンにインストール時の絶対パスが焼き込まれる）
- yt-dlp の `--print` は暗黙に `--quiet` を付与し `--progress-template` の出力まで
  抑制する。進捗表示が要る場面では `--progress` を明示すること
- yt-dlp/子プロセスは必ずプロセスグループ単位で終了させる（`os.killpg`）。親だけ
  kill すると ffmpeg 等が孤児として残る
- 未知の yt-dlp エラーメッセージは `failed`（安全側、リトライ対象）に分類する。
  `unavailable`/`blocked` に倒すのは確実にパターンが一致した場合のみ（D-014）
- `git push` / `gh repo create` / `gh repo edit` は `docs/公開前チェックリスト.md` の履歴監査完了とユーザーの明示的な承認を得た後にのみ許可（`CLAUDE.md` §4.1、`docs/phase3.5_指示書_改訂版.md` §6.2）

## 環境メモ

- 起動: `docker compose up -d --build`（`.env` は `.env.example` からコピーして作成。
  リポジトリには含まれない）。`make` が使える環境では `make up`
- テスト: `make test`（dev 依存込みの `sluicery:local-test` イメージをビルドしコンテナ内で
  pytest 実行。70件パス確認済み）。`make lint` で `ruff check` / `mypy` もクリーン
- DB: `data/sluicery.db`（named volume `data` 内）
- yt-dlp venv: `data/ytdlp/`（`versions/<version>/`、`current` symlink、`.lock`）
- マイグレーション: `sluicery db upgrade`（`AUTO_MIGRATE=true` なら `app` 起動時に自動実行）
- ログ: `data/logs/`（yt-dlp 実行の stderr 全文もここ、`ytdlp-<uuid>.log`）
- CLI: `docker compose exec app python3 -m sluicery.cli {config check | db ... | settings ... | ytdlp ...}`
- 実機検証で使った試験用 URL: `https://download.blender.org/peach/trailer/trailer_1080p.mov`
  （Blender Foundation、Creative Commons。D-015、`docs/基本設計.md` に記録）
- 現在稼働中の実機検証環境: `docker compose up -d` 済み（`data` volume はクリーン状態から
  構築し直したもの）。`ytdlp.auto_install`/`ytdlp.idle_timeout_sec` は既定値に戻し済み

## 既知の落とし穴

- SQLite の WAL モードでもワーカーとの同時書き込みが競合しないよう、書き込みトランザクションは短く保つ
- `docker compose down -v` は volume を消す。開発中は使わない
- `make purge` は Staging（`data` volume 内）も消す。進行中ダウンロードの中間ファイルを失う
- Alembic の autogenerate は CHECK 制約の偽陽性 diff を出す（上記未解決 #1）
- yt-dlp の venv リネーム後はシェバン書き換えが必須（上記「重要な前提」参照。忘れると
  `ytdlp status` が `broken` になる）
- `--print` は `--quiet` を暗黙付与し進捗出力を消す。進捗が要る場合は `--progress` を足す
- コンテナに init が無く、孫プロセスの zombie が reap されない可能性がある（未解決 #3）
