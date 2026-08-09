# AISTATE

> このファイルはセッション間の引き継ぎ用です。
> セッション開始時に最初に読み、セッション終了時に必ず更新してください。

最終更新: 2026-08-09 22:12
対応コミット: fb6874b docs: Phase 4レビュー対応と完了状態を記録

## プロジェクト概要

sluicery は yt-dlp を用いた自己ホスト型のプレイリスト同期サーバー。
詳細は `docs/要件定義.md`。

## 現在の進捗

要件定義 §20 の実装順序に対する現在地。

- [x] 1. リポジトリ骨格、Dockerfile、compose.yaml、Makefile、`.env.example`、entrypoint
- [x] 2. 設定読み込み、`SECRET_KEY` 検証、DB スキーマ + Alembic マイグレーション
- [x] 3. yt-dlp venv 管理（インストール、バージョン取得）と CLI ラッパ
- [x] 4. オプション合成モデル、ガード、コマンドラインプレビュー
- [ ] 5. Storage アダプタ（local / remote-rclone）、接続テスト、クレデンシャル暗号化  ← 次の着手点
- [ ] 6. Task キューとワーカー（network / compute の2クラス）
- [ ] 7. パイプライン（download → verify → postprocess(空) → publish → index）
- [ ] 8. 二相同期（discover / download）、状態遷移
- [ ] 9. 認証、Web UI 骨格（レイアウト、ログイン）
- [ ] 10. Playlist / Profile / Storage の CRUD 画面
- [ ] 11. Run 履歴、進捗表示、ログ閲覧、キャンセル
- [ ] 12. スケジューラ（分離スケジュール、時間帯制限、ジッター）
- [ ] 13. 整合性チェック、relink、手動リンク画面、差分レポート
- [ ] 14. フォーマット検査機能
- [ ] 15. yt-dlp 自動更新 + スモークテスト + ロールバック
- [ ] 16. retention（ドライラン必須）
- [ ] 17. 設定エクスポート / インポート
- [ ] 18. フック機構（インターフェース + eventlog 実装）
- [ ] 19. `compose.privileged.yaml`（mount アダプタ）、GPU 設定のコメントアウト配置
- [ ] 20. バックアップ / リストア、ドキュメント一式

## 直近の作業

- Phase 4 を実装。Profile 継承フィールドの三状態化、6層オプション合成、予約引数・
  `--exec` 二重ゲート、引数由来、`flat` / `custom` レイアウト、命名・欠損値方針を追加した
- Phase 9 までの暫定 Storage / Profile / Playlist CRUD と `options preview` を追加。
  Profile の nullable 値は `--inherit-*`、自由入力は `--clear-*` で未設定へ戻せる
- `ytdlp probe` / `fetch` を合成ビルダー経由へ置換し、取得 URL を HTTP(S) に限定して
  オプション終端 `--` の後ろへ置く。raw `ytdlp exec` は予約引数を拒否する
- 認証・Cookie・Proxy 系の長短オプション、URL の userinfo と認証 query / fragment を
  共通マスク層で伏せる。preview、show、probe / fetch、raw exec は同じ層を使う
- yt-dlp venv は `yt-dlp[default]` と導入契約 marker を使用。current / non-current の
  全バージョンを検証し、broken 版の `use` を拒否、`install` で再構築する（D-023）
- 開発機で Phase 4 実機検証18項目を完了。公式 Blender Open Movies を用いて、
  複数エントリ、動画2系統、opus・タグ・埋め込み画像、命名境界、所有者を確認した
- 独立レビューを実施し、全7指摘へ対応した。記録は `docs/reviews/phase4.md`
- レビュー修正後に `make test` 161件、Ruff、mypy が成功。compose 3サービスは稼働し、
  DB は `5b8c9d1e2f30`、yt-dlp 2026.07.04 は `ready`。実機 probe と通常 fetch も再成功した
- 公開前チェックリストの履歴監査を完了。71コミットを gitleaks で走査して漏えい0件。
  危険ファイル名・環境固有情報の履歴混入はなく、機密パターン一致は合成値・空設定・手順書だけだった
- `checkpoint/step-03.5` は Phase 3.5 完了点に付与済み。Phase 4 完了コミットへ
  `checkpoint/step-04` を付与する。push はユーザーの明示承認待ち

## 次にやること

1. 監査結果をユーザーへ報告し、明示的な承認があるまで push しない
2. 公開可否、指示書 / AISTATE の公開、Issues / Wiki / Projects、Dependabot の判断を待つ
3. 次セッションでは Phase 5 の指示書を読み、Storage アダプタの現状と要件を着手前確認する

## 未解決・保留

| # | 内容 | 状態 |
|---|---|---|
| 1 | Alembic の autogenerate は SQLite の CHECK 制約比較で偽陽性 diff を出す（D-008） | マイグレーション追加時に手で除去 |
| 2 | `target.status=blocked` に対応する `TaskStatus` がない | Phase 6/7 着手時に判断 |
| 3 | compose に init がなく、yt-dlp の孫プロセスが zombie として残る可能性がある | Phase 6 着手時に評価 |
| 4 | GitHub リポジトリの public 化 | 見送り中・判断待ち |
| 5 | Issues / Wiki / Projects の要否 | 未確認 |
| 6 | Dependabot alerts の要否 | 未確認 |
| 7 | README・`docs/deployment.md` の clone URL が `<repo>` のまま | public 化時に差し替え |
| 8 | 1秒区間を `--download-sections` で切り出す追加試験は ffmpeg `-11`（SIGSEGV）で失敗した。通常 fetch は成功 | ffmpeg 静的ビルドの健全性に関わる可能性がある。Phase 7（verify で ffprobe / ffmpeg を使用）着手時に再評価する |
| 9 | Phase 4 の16コミットとタグの GitHub push | 公開前監査済み・ユーザー承認待ち |

generic extractor で `uploader` / `duration` / `upload_date` が欠損する件は Phase 4 で再確認済み。
命名は空文字または `0` へ fallback し、`NA` を混入させない（D-019）。

## 重要な前提（忘れやすいもの）

- 配信元での削除は絶対にローカルファイルの削除に伝播させない
- `blocked` はリトライ回数を消費しない
- ファイル名の `[<source_id>]` は末尾（拡張子直前）に固定。relink がこれに依存する
- `SECRET_KEY` 未設定時は起動を拒否する
- `SECRET_KEY` はローテーション非対応（鍵紛失・変更時はクレデンシャル再入力。D-004）
- 運用パラメータは `.env` ではなく `setting` テーブル、既定値は `core/settings.py` の `CODE_DEFAULTS`
- 内部状態は `_internal.*` 名前空間に置き `CODE_DEFAULTS` に登録しない
- DB のタイムスタンプは独自の `UTCDateTime` 型を使う
- `MEDIA_ROOT` はホスト側パスであり、コンテナ内では常に `/mnt/media` を使う（D-010）
- リポジトリ層に状態遷移ロジックを書かない
- yt-dlp venv の変更は `app` のみ。worker は `current` symlink 越しに読み取る
- venv をリネームしたら `_relocate_shebangs()` を必ず通す
- yt-dlp の各 venv は current / non-current を問わず導入契約と実行可能性を確認する
- yt-dlp の `--print` は暗黙に `--quiet` を付与する。進捗が必要なら `--progress` を明示する
- 取得 URL は HTTP(S) に限定し、必ず yt-dlp のオプション終端 `--` の後ろへ置く
- yt-dlp/子プロセスはプロセスグループ単位で終了させる
- 未知の yt-dlp エラーは `failed` に分類する（D-014）
- `git push` / `gh repo create` / `gh repo edit` は履歴監査とユーザー承認後のみ許可

## 環境メモ

- 起動: `make up` または `docker compose up -d --build`
- テスト: `make test`。lint: `make lint`
- DB: `data/sluicery.db`（named volume `data` 内）
- yt-dlp venv: `data/ytdlp/`（`versions/<version>/`、`current` symlink、`.lock`）
- マイグレーション: `sluicery db upgrade`（`AUTO_MIGRATE=true` なら起動時に自動実行）
- ログ: `data/logs/`
- CLI: `docker compose exec app python3 -m sluicery.cli ...`。ファイル生成時は
  `docker compose exec --user "$(id -u):$(id -g)" app ...` を使う
- D-015 試験 URL: `https://download.blender.org/peach/trailer/trailer_1080p.mov`
- Phase 4 試験 Playlist: Blender Studio 公式 Open Movies（URL・権利根拠は D-022）
- 開発機の compose 環境は起動済み。3サービス正常、DB current=head、yt-dlp `ready`
- 公開前監査: 71コミット、gitleaks 漏えい0件。`.env` / `backups/` / `data/` は ignore 済み。
  `Zone.Identifier` は作業ツリーで ignore され、履歴には存在しない
- VM（Ubuntu 22.04.5）には `~/sluicery`、`~/sluicery.bundle`、`~/alt-media`、
  `/mnt/media` が残る。片付けはユーザー判断であり Phase 4 では触らない

## 既知の落とし穴

- SQLite WAL でも書き込みトランザクションは短く保つ
- `docker compose down -v` と `make purge` は volume / Staging を消す
- Alembic autogenerate の CHECK 制約偽陽性をそのままコミットしない
- yt-dlp venv リネーム後のシェバン修正を忘れない
- `--print` による進捗抑制を合成側で補償する
- コンテナに init がなく、孫プロセスの zombie が残る可能性がある
