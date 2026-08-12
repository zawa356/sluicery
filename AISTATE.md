# AISTATE

> このファイルはセッション間の引き継ぎ用です。
> セッション開始時に最初に読み、セッション終了時に必ず更新してください。

最終更新: 2026-08-12 23:08
対応コミット: 381eab0 fix: Task状態遷移の所有権競合を解消

## プロジェクト概要

sluicery は yt-dlp を用いた自己ホスト型のプレイリスト同期サーバー。
詳細は `docs/要件定義.md`。

## 現在の進捗

要件定義 §20 の実装順序に対する現在地。

- [x] 1. リポジトリ骨格、Dockerfile、compose.yaml、Makefile、`.env.example`、entrypoint
- [x] 2. 設定読み込み、`SECRET_KEY` 検証、DB スキーマ + Alembic マイグレーション
- [x] 3. yt-dlp venv 管理（インストール、バージョン取得）と CLI ラッパ
- [x] 4. オプション合成モデル、ガード、コマンドラインプレビュー
- [x] 5. Storage アダプタ（local / remote-rclone）、接続テスト、クレデンシャル暗号化
- [x] 6. Task キューとワーカー（network / compute の2クラス）
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

- `TaskStatus` に `blocked` / `unavailable`、taskへ `available_at` / `blocked_until` /
  `blocked_reason` / `heartbeat_at` / `worker_id` / `cancel_requested` を追加した
- 既存の単一 `UPDATE ... RETURNING` claimを、時刻・依存成功・worker所有権対応へ拡張した
- network / compute worker、poll jitter、指数backoff、graceful shutdown、heartbeat、app stale回収、
  Task / Run cancel、依存失敗の後続cancel、所有権付き進捗スロットリングを実装した
- `worker.enable_test_tasks=false` で既定無効のダミーTask 6種と暫定Task CLIを追加した
- 全サービスへ `init: true`、workerへ `stop_grace_period: 30s`、build targetへruntimeを設定した
- Phase 6実機検証20項目と実DB migration往復を完了。レビュー対応後の`make test` 240件、Ruff、mypy成功
- 独立レビューで失敗再試行、cancel / retryとclaimの所有権競合を検出し、状態・所有権条件付き単一UPDATEと競合回帰試験で解消した
- `docs/reviews/phase6.md` に指摘と対応を記録し、ローカルの`checkpoint/step-06`を付与した
- 検証用Task 24件とworker設定上書きは削除済み。Stagingの既存ファイルは変更していない

## 次にやること

1. Phase 7指示書を読み、download → verify → postprocess(空) → publish → index の境界と既存Task契約を棚卸しする
2. Phase 7で実ハンドラを登録し、Staging保持・原子的publish・依存失敗伝播を実パイプラインへ接続する
3. Phase 7着手時にffmpeg静的ビルドのSIGSEGV（未解決 #8）を再評価する

## 未解決・保留

| # | 内容 | 状態 |
|---|---|---|
| 1 | Alembic の autogenerate は SQLite の CHECK 制約比較で偽陽性 diff を出す（D-008） | マイグレーション追加時に手で除去 |
| 4 | GitHub リポジトリの public 化 | 見送り中・判断待ち |
| 5 | Issues / Wiki / Projects の要否 | 未確認 |
| 6 | Dependabot alerts の要否 | 未確認 |
| 7 | README・`docs/deployment.md` の clone URL が `<repo>` のまま | public 化時に差し替え |
| 8 | 1秒区間を `--download-sections` で切り出す追加試験は ffmpeg `-11`（SIGSEGV）で失敗した。通常 fetch は成功 | Phase 7のverify着手時に静的ビルドの健全性を再評価 |
| 9 | Phase 6のローカルコミットとタグのGitHub push | 公開前監査とユーザー承認前は実行しない |

未解決 #2（Task側blocked）と #3（compose init）はPhase 6のD-029 / D-030で解消済み。

## 重要な前提（忘れやすいもの）

- 配信元での削除は絶対にローカルファイルの削除に伝播させない
- `blocked` はリトライ回数を消費しない
- shutdownはTaskをattempts不変の`pending`へ戻し、Stagingの中間ファイルを消さない
- stale回収はTaskを`pending`へ戻し、無限再実行防止のためattemptsを1増やす
- 進捗更新は短い単一UPDATEとし、最終状態は必ず保存する
- ファイル名の `[<source_id>]` は末尾（拡張子直前）に固定。relink がこれに依存している
- `SECRET_KEY` 未設定時は起動を拒否する。ローテーションには対応しない（D-004）
- 運用パラメータは`.env`ではなくsettingテーブル、既定値は`core/settings.py`の`CODE_DEFAULTS`
- `MEDIA_ROOT` はホスト側パスで、コンテナ内では常に `/mnt/media` を使う（D-010）
- yt-dlp / rclone とその子はプロセスグループ単位で終了させる
- Storage publishは一時名で検証後に最終化し、既定で上書きしない
- `git push` / `gh repo create` / `gh repo edit` は履歴監査とユーザー承認後のみ許可

## 環境メモ

- 起動: `make up` または `docker compose up -d --build`
- テスト: `make test`。lint: `make lint`
- DB: `data/sluicery.db`（named volume内）。current/head=`c7d94b31a6e2`
- CLI: `docker compose exec app python3 -m sluicery.cli ...`
- 開発機の3サービスはruntimeイメージで稼働中。app healthy、yt-dlp 2026.07.04 ready
- worker IDは `service:container:pid:nonce`。再起動ごとにnonceが変わる
- Phase 6検証後、`worker.*` は全てコード既定値、`worker.enable_test_tasks=false`
- `/data/staging/trailer_1080p.mov` はPhase 3由来の既存ファイル。Phase 6では変更・削除していない

## 既知の落とし穴

- SQLite WALでも書込み競合は起こる。claim、heartbeat、進捗、状態更新のtransactionを短く保つ
- heartbeatのstale閾値はheartbeat間隔の3倍以上にする。既定は30秒 / 180秒
- `docker compose down -v` はvolumeとStagingを消す。開発中は使わない
- 検証用Taskを有効化した場合、workerは設定を起動時に読むため再起動が必要
- Docker/WSLでは異なるmountが同じ`st_dev`を返し得る。local publishは実際のEXDEVでcopyへ切替える
