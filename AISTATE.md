# AISTATE

> このファイルはセッション間の引き継ぎ用です。
> セッション開始時に最初に読み、セッション終了時に必ず更新してください。

最終更新: 2026-08-09 20:35
対応コミット: 779c9b6 docs: AISTATEをGitHub private push完了・public化保留の状態に更新

## プロジェクト概要

sluicery は yt-dlp を用いた自己ホスト型のプレイリスト同期サーバー。
詳細は `docs/要件定義.md`。

## 現在の進捗

要件定義 §20 の実装順序に対する現在地。

- [x] 1. リポジトリ骨格、Dockerfile、compose.yaml、Makefile、`.env.example`、entrypoint
- [x] 2. 設定読み込み、`SECRET_KEY` 検証、DB スキーマ + Alembic マイグレーション
- [x] 3. yt-dlp venv 管理（インストール、バージョン取得）と CLI ラッパ
- [ ] 4. オプション合成モデル、ガード、コマンドラインプレビュー  ← 作業中
- [ ] 5. Storage アダプタ（local / remote-rclone）、接続テスト、クレデンシャル暗号化
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

- Phase 3.5（公開準備）を完了。README、LICENSE（MIT）、デプロイ・トラブルシューティング・
  公開前チェックリストを整備し、Ubuntu 22.04.5 VM で17項目を実機検証した
- VM 検証で Quick Start の `SECRET_KEY` 生成、`MEDIA_ROOT` の事前作成、
  `docker compose exec --user` の不足を発見して修正した
- 履歴全体の機密監査を実施し、GitHub `zawa356/sluicery` に private で push 済み。
  public 化は見送った
- `checkpoint/step-03.5` タグを Phase 3.5 完了コミットに付与した
- Phase 4 の最初の実装として、Profile の継承対象フラグ6項目を三状態化する
  Alembic マイグレーションと往復テストを追加した（D-017）
- 予約引数・警告対象だけを認識する最小オプションパーサとガード、および
  `defaults.video.*` / `defaults.music.*` の種別既定を追加した
- `flat` / `custom` レイアウト、subpath テンプレート、NFC・Windows 予約語・
  traversal を扱うユーザー入力由来パスの検証を追加した
- 6層の構造化フィールド解決・自由文字列連結・由来追跡と、discover / download
  のコマンドビルダーを `core/options.py` に実装した
- Phase 9 までの暫定 Storage / Profile / Playlist CRUD、Profile 割当、
  `options preview` を CLI に追加した。削除は実ファイルを操作しない（D-021）
- `ytdlp probe` / `fetch` の暫定固定引数を削除し、discover / download の
  合成ビルダー経由へ置換した。`--print` の進捗抑制補償もビルダーに集約した
- 実機の音楽 fetch で判明した `mutagen` 不足を修正し、yt-dlp venv は公式
  `default` extras 付きで導入するようにした。opus のタグと埋め込み画像を確認済み

## 次にやること

1. `docs/phase4_指示書_改訂版.md` §0 の README・deployment・legal 是正とレビュー役定義
2. Profile の三状態化、6層オプション合成、予約引数ガード、レイアウト・命名を実装
3. 最小 CRUD CLI と `options preview` を追加し、`ytdlp probe` / `fetch` を合成経由へ置換
4. 開発機で実機検証し、独立レビュー・履歴監査後に push 承認を求める

## 未解決・保留

| # | 内容 | 状態 |
|---|---|---|
| 1 | Alembic の autogenerate は SQLite の CHECK 制約比較で偽陽性 diff を出す（基本設計 D-008） | マイグレーション追加時に手で除去 |
| 2 | `target.status=blocked` に対応する `TaskStatus` がない | Phase 6/7 着手時に判断 |
| 3 | compose に init がなく、yt-dlp の孫プロセスが zombie として残る可能性がある | Phase 6 着手時に評価 |
| 4 | generic extractor では `uploader` / `duration` / `upload_date` 等が取得できない場合がある | Phase 4 実機検証で再確認 |
| 5 | GitHub リポジトリの public 化 | 見送り中・判断待ち |
| 6 | Issues / Wiki / Projects の要否 | 未確認 |
| 7 | Dependabot alerts の要否 | 未確認 |
| 8 | README・`docs/deployment.md` の clone URL が `<repo>` のまま | public 化時に差し替え |

## 重要な前提（忘れやすいもの）

- 配信元での削除は絶対にローカルファイルの削除に伝播させない
- `blocked` はリトライ回数を消費しない
- ファイル名の `[<source_id>]` は末尾（拡張子直前）に固定。relink がこれに依存している
- `SECRET_KEY` 未設定時は起動を拒否する
- `SECRET_KEY` はローテーション非対応（鍵紛失・変更時はクレデンシャル再入力。D-004）
- 運用パラメータは `.env` ではなく `setting` テーブル、既定値は `core/settings.py` の `CODE_DEFAULTS`
- 内部状態は `_internal.*` 名前空間に置き `CODE_DEFAULTS` に登録しない
- DB のタイムスタンプは独自の `UTCDateTime` 型を使う
- `MEDIA_ROOT` はホスト側パスであり、コンテナ内では常に `/mnt/media` を使う（D-010）
- リポジトリ層に状態遷移ロジックを書かない
- yt-dlp venv の変更は `app` のみ。worker は `current` symlink 越しに読み取る
- venv をリネームしたら `_relocate_shebangs()` を必ず通す
- yt-dlp の `--print` は暗黙に `--quiet` を付与する。進捗が必要なら `--progress` を明示する
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
- 開発機の compose 環境は起動済み。Phase 3 時点のテスト70件と lint はクリーン
- VM（Ubuntu 22.04.5）には `~/sluicery`、`~/sluicery.bundle`、`~/alt-media`、
  `/mnt/media` が残る。片付けはユーザー判断であり Phase 4 では触らない

## 既知の落とし穴

- SQLite WAL でも書き込みトランザクションは短く保つ
- `docker compose down -v` と `make purge` は volume / Staging を消す
- Alembic autogenerate の CHECK 制約偽陽性をそのままコミットしない
- yt-dlp venv リネーム後のシェバン修正を忘れない
- `--print` による進捗抑制を合成側で補償する
- コンテナに init がなく、孫プロセスの zombie が残る可能性がある
