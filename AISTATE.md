# AISTATE

> このファイルはセッション間の引き継ぎ用です。
> セッション開始時に最初に読み、セッション終了時に必ず更新してください。

最終更新: 2026-08-13
対応コミット: Phase 9 Part A（認証・Web UI骨格・Cookie）レビュー①完了

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
- [x] 9. 単一管理者認証、CSRF、Web UI骨格、Playlist Cookie
- [ ] 10–20. CRUD画面以降

## 直近の作業

- argon2の初期管理者、ハッシュ化DBセッション、固定30日期限、5回・15分のDBロック、ログイン時ID再生成、パスワード変更時の全セッション失効を実装した（D-046）
- 全GET以外を共通依存で検証するセッション結合CSRFと、公開パスだけを列挙するホワイトリスト認証を実装した
- Jinja2共通レイアウト、7グループナビゲーション、フラッシュ、403 / 404 / 500、ローカルCSSとHTMX 2.0.10を同梱した
- Playlist CookieをEncryptedJSONへwrite-only保存し、実行時だけtmpfsへ600で展開、finally削除する。yt-dlp書き戻しはDBへ反映しない（D-047）
- Cookie有効のD-022単一動画と既存403 Target 5件を再検証し、全件成功。ログ・DB生値・Task payloadへの平文混入とtmpfs残存は0
- セキュリティレビュー①の中2件・軽微1件を対応し、全340テストとRuff / mypyが成功した

## 次にやること

1. Part Bのダッシュボードを実装する
2. Playlist CRUDと詳細のページネーション・検索・状態フィルタ・Profile割当を実装する
3. Profile CRUD（三状態UI・参照Playlist・プレビュー）を実装する
4. Storage CRUD・4段階接続テストと設定画面を実装し、`checkpoint/step-10`を付ける

## 未解決・保留

| # | 内容 | 状態 |
|---|---|---|
| 1 | Alembic autogenerateがSQLite CHECK制約で偽陽性diffを出す（D-008） | マイグレーション追加時に手で除去 |
| 2 | GitHubリポジトリのpublic化、Issues / Wiki / Projects / Dependabot | 判断待ち |
| 3 | README / deploymentのclone URLが`<repo>`のまま | public化時に差し替え |
| 4 | ffmpegの`--download-sections` 1秒区間切り出しは`-11` | ffprobe通常検証は健全。D-036 |
| 5 | ローカルコミットとタグのGitHub push | 公開前監査とユーザー承認前は禁止 |
| 6 | `.local/docker-server.env`のSSH認証が拒否される | ローカルDockerで継続。最終実機確認までに要確認 |

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
- payloadへ資格情報・取得元URL・Cookieを保存しない
- 認証はホワイトリスト方式、CSRFはGET以外の全エンドポイントへ共通適用
- CookieはPlaylist単位のオプトイン。書き戻しを保存せず、平文はtmpfsへだけ展開する
- `git push` / `gh repo create` / `gh repo edit`は履歴監査とユーザー承認後だけ許可

## 環境メモ

- 起動: `make up`または`docker compose up -d --build`
- テスト: `make test`、lint: `make lint`
- 同期: `make sync`または`docker compose exec --user "$(id -u):$(id -g)" app python3 -m sluicery.cli sync ...`
- 実機用のSMB / Docker SSH / Playlist URL / Cookieは、ignoredかつmode 600の`.local/`だけにある
- `/data/staging/trailer_1080p.mov`は削除禁止。既存の孤立検出対象は自動削除しない
- `sync.max_targets_per_run`は既定50へ復元済み
- DBマイグレーションheadは`b8c9d0e1f2a3`
- runtimeはapp healthy、worker-network / worker-compute稼働中
- 検証PlaylistのCookieは現在有効。平文ファイルは`.local`にだけ存在する

## 既知の落とし穴

- SQLite WALでも書込み競合は起こる。claim、heartbeat、進捗、状態更新のtransactionを短くする
- workerは運用設定を起動時に読むため、設定変更後は該当workerの再起動が必要
- `sync run --all`のdownload Runは投入完了時点で成功になる。メディア取得の成否はTarget / Taskを確認する
- HTTP 403多発時は並列度を上げず停止する。Cookieを使う場合もPlaylist単位・少数で試す
- Profile自由引数よりPlaylist自由引数が後勝ちになる。検証用`--format`が残るPlaylistを別Profileへ流用しない
- `docker compose down -v`はDBとStagingを消す。通常開発では使わない
