# AISTATE

> このファイルはセッション間の引き継ぎ用です。
> セッション開始時に最初に読み、セッション終了時に必ず更新してください。

最終更新: 2026-08-13
対応コミット: Phase 9 §2（Deno導入・HTTP 403再分類）完了

## プロジェクト概要

sluiceryはyt-dlpを用いた自己ホスト型のプレイリスト同期サーバー。詳細は
`docs/要件定義.md`。実装判断と実機結果は`docs/基本設計.md`、変更一覧は
`docs/変更履歴.md`を正とする。

## 現在の進捗

- [x] 1. リポジトリ骨格、Dockerfile、compose.yaml、Makefile、`.env.example`、entrypoint
- [x] 2. 設定読み込み、`SECRET_KEY` 検証、DB スキーマ + Alembic マイグレーション
- [x] 3. yt-dlp venv 管理（インストール、バージョン取得）と CLI ラッパ
- [x] 4. オプション合成モデル、ガード、コマンドラインプレビュー
- [x] 5. Storage アダプタ（local / remote-rclone）、接続テスト、クレデンシャル暗号化
- [x] 6. Task キューとワーカー（network / compute の2クラス）
- [x] 7. パイプライン（download → verify → postprocess(空) → publish → index）
- [x] 8. 二相同期（discover / download）、状態遷移
- [ ] 9. 認証、Web UI 骨格（レイアウト、ログイン） ← §2の403対処完了、Part A着手前
- [ ] 10–20. CRUD画面以降

## 直近の作業

- Deno 2.9.5をversioned URL・SHA-256検証付きでruntimeイメージへ同梱した（D-044）
- HTTP 403とボット確認をattemptsを消費しない`blocked`へ再分類し、専用の1時間待機を追加した（D-045）
- 既存ログはJSランタイム欠如警告325件、HTTP 403が70件。D-022のprobe / fetchはDeno導入後に成功した
- 既存403 Target 5件の再試行は成功2、HTTP 403 blocked 2、形式非互換 unavailable 1。大量アクセスはしていない
- 基準線306テストと、403関連43テストが成功した

## 次にやること

1. Part Aの単一ユーザー認証、DBセッション、永続ロックアウトを実装する
2. CSRF、Web UI骨格、ローカル同梱HTMX / CSSを実装する
3. Playlist Cookieの暗号化保存・tmpfs展開・確実な削除・マスクを実装し、少量で403を再評価する
4. セキュリティ重点レビュー①を実施し、`checkpoint/step-09`を付ける

## 未解決・保留

| # | 内容 | 状態 |
|---|---|---|
| 1 | Alembic autogenerateがSQLite CHECK制約で偽陽性diffを出す（D-008） | マイグレーション追加時に手で除去 |
| 2 | GitHubリポジトリのpublic化、Issues / Wiki / Projects / Dependabot | 判断待ち |
| 3 | README / deploymentのclone URLが`<repo>`のまま | public化時に差し替え |
| 4 | ffmpegの`--download-sections` 1秒区間切り出しは`-11` | ffprobe通常検証は健全。D-036 |
| 5 | ローカルコミットとタグのGitHub push | 公開前監査とユーザー承認前は禁止 |
| 6 | Deno導入後も既存5件中2件はHTTP 403 | Part AのCookieサポート後に少量再評価 |
| 7 | `.local/docker-server.env`のSSH認証が拒否される | ローカルDockerで継続。最終実機確認までに要確認 |

## 重要な合意

- 「ローカルのデータを失わない」
- 「不完全なファイルを最終保存先に残さない」
- 「システムに残骸を残さない」
- 「不確実な状態で削除しない」
- 「すべての操作を追跡可能にする」
- discoverの取得結果が空またはエラーの場合、以降の処理を中止する
- delistedへの遷移は Artifact に一切影響しない。ファイルを削除しない
- `blocked` は原因解消後に自動で `pending` に戻す
- `missing` からの自動再取得は既定で無効
- `ignored` はユーザー操作でのみ設定・解除される
- `artifact` は index タスクで作成する
- Stagingはindex後だけ削除し、失敗・中断時は削除しない
- final既存時は期待サイズ一致だけをpublish済み復旧とし、不一致は上書きしない
- payloadへ資格情報・取得元URLを保存しない
- `git push` / `gh repo create` / `gh repo edit`は履歴監査とユーザー承認後だけ許可

## 環境メモ

- 起動: `make up`または`docker compose up -d --build`
- テスト: `make test`、lint: `make lint`
- 同期: `make sync`または`docker compose exec --user "$(id -u):$(id -g)" app python3 -m sluicery.cli sync ...`
- 実機用のSMB / Docker SSH / Playlist URL / Cookieは、ignoredかつmode 600の`.local/`だけにある
- `/data/staging/trailer_1080p.mov`は削除禁止。既存の孤立検出対象は自動削除しない
- `sync.max_targets_per_run`は既定50へ復元済み
- DBマイグレーションheadは`e4a1f7b9c203`

## 既知の落とし穴

- SQLite WALでも書込み競合は起こる。claim、heartbeat、進捗、状態更新のtransactionを短くする
- workerは運用設定を起動時に読むため、設定変更後は該当workerの再起動が必要
- `sync run --all`のdownload Runは投入完了時点で成功になる。メディア取得の成否はTarget / Taskを確認する
- HTTP 403多発時は並列度を上げず停止する。中間ファイルは調査・再開まで削除しない
- Profile自由引数よりPlaylist自由引数が後勝ちになる。検証用`--format`が残るPlaylistを別Profileへ流用しない
- `docker compose down -v`はDBとStagingを消す。通常開発では使わない
