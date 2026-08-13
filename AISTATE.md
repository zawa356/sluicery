# AISTATE

> このファイルはセッション間の引き継ぎ用です。
> セッション開始時に最初に読み、セッション終了時に必ず更新してください。

最終更新: 2026-08-13
対応コミット: Phase 10 Part B（ダッシュボード・Web CRUD・設定画面）完了

## プロジェクト概要

sluiceryはyt-dlpを用いた自己ホスト型のプレイリスト同期サーバー。詳細は
`docs/要件定義.md`。実装判断と実機結果は`docs/基本設計.md`、変更一覧は
`docs/変更履歴.md`を正とする。

## 現在の進捗

- [x] 1. リポジトリ骨格、Dockerfile、compose.yaml、Makefile、`.env.example`、entrypoint
- [x] 2. 設定読み込み、`SECRET_KEY`検証、DBスキーマ + Alembicマイグレーション
- [x] 3. yt-dlp venv管理（インストール、バージョン取得）とCLIラッパ
- [x] 4. オプション合成モデル、ガード、コマンドラインプレビュー
- [x] 5. Storageアダプタ（local / remote-rclone）、接続テスト、クレデンシャル暗号化
- [x] 6. Taskキューとワーカー（network / computeの2クラス）
- [x] 7. パイプライン（download → verify → postprocess(空) → publish → index）
- [x] 8. 二相同期（discover / download）、状態遷移
- [x] 9. 単一管理者認証、CSRF、Web UI骨格、Playlist Cookie
- [x] 10. ダッシュボード、Playlist / Profile / Storage CRUD、運用設定画面
- [ ] 11–20. Run履歴・進捗・ログ・キャンセル以降

## 直近の作業

- ダッシュボードに直近Run、yt-dlp状態、Staging使用率、Storage到達性、failed / missing / delisted件数を追加した。次回予定はPart Dまで未実装と明示している
- Playlistの作成・編集・無効化／一時停止、手動discover / download、Profile割当、個別retry / ignore、50件ページネーション、検索・状態フィルタを追加した
- Playlist削除はItem保持と関連DBレコード削除を明示選択させ、いずれもメディアファイルを操作しない。Run参照があればDB削除も拒否する
- Profileの三状態UI、参照Playlist、由来層付きマスク済みコマンドラインプレビューを追加した。フォーマット検査はPhase 14予定として無効表示する
- Storageのlocal / remote SMB CRUD、write-only暗号化クレデンシャル、4段階接続テスト、空き容量・参照表示を追加した。削除でメディアを操作しない
- 運用設定画面にコード既定／DB上書きの由来、型・範囲・相互しきい値検証、既定値復帰、パスワード変更導線を追加した。`_internal.*`は表示・更新できない
- Part B完了確認で全357テスト、Ruff、mypyが成功。本番イメージを再構築し、3サービス稼働、healthz、認証リダイレクト、DB head一致を確認した

## 次にやること

1. Part CのRun履歴一覧・詳細・状態フィルタ・ページネーションを実装する
2. `payload_json.progress`を読むHTMXポーリングを追加し、実行中Taskが無ければ停止する
3. DBの`run.log_path`だけを起点とするDATA_DIR境界内のマスク済みログ表示・ダウンロードを実装する
4. Task / Runキャンセルを既存機構へ接続し、レビュー②後に`checkpoint/step-11`を付ける
5. Part Dのapp専用APScheduler、独立cron、TZ、時間帯、ジッター、整合、misfireを実装する

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
- delistedへの遷移はArtifactに一切影響しない。ファイルを削除しない
- `blocked`は原因解消後に自動で`pending`へ戻す
- `missing`からの自動再取得は既定で無効
- `ignored`はユーザー操作でのみ設定・解除される
- `artifact`はindex Taskで作成する
- Stagingはindex後だけ削除し、失敗・中断時は削除しない
- final既存時は期待サイズ一致だけをpublish済み復旧とし、不一致は上書きしない
- payloadへ資格情報・取得元URL・Cookieを保存しない
- 認証はホワイトリスト方式、CSRFはGET以外の全エンドポイントへ共通適用
- CookieはPlaylist単位のオプトイン。書き戻しを保存せず、平文はtmpfsへだけ展開する
- Web UIとCLIは併存し、CLIを自動化・デバッグ用の恒久機能として維持する
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
