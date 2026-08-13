# AISTATE

> このファイルはセッション間の引き継ぎ用です。
> セッション開始時に最初に読み、セッション終了時に必ず更新してください。

最終更新: 2026-08-13
対応コミット: Phase 11 Part C（Run履歴・進捗・ログ・キャンセル）完了

## プロジェクト概要

sluiceryはyt-dlpを用いた自己ホスト型のプレイリスト同期サーバー。詳細は
`docs/要件定義.md`。実装判断と実機結果は`docs/基本設計.md`、変更一覧は
`docs/変更履歴.md`を正とする。

## 現在の進捗

- [x] 1–8. 基盤、設定・DB、yt-dlp、オプション、Storage、Task、パイプライン、二相同期
- [x] 9. 単一管理者認証、CSRF、Web UI骨格、Playlist Cookie
- [x] 10. ダッシュボード、Playlist / Profile / Storage CRUD、運用設定画面
- [x] 11. Run履歴、HTMX進捗、マスク済みログ、Task / Runキャンセル
- [ ] 12. app専用スケジューラ、分離cron、時間帯、ジッター、整合、misfire
- [ ] 13–20. 整合性、後処理、yt-dlp更新、通知、バックアップ、フック、mount、仕上げ

## 直近の作業

- Run一覧・詳細へ状態フィルタ、50件ページネーション、Task状態内訳を追加し、download Runの「投入結果」と実際の取得状態を分離表示した
- `payload_json.progress`の種別・対象・進捗率・速度・ETAを3秒間隔で更新し、running Taskが無い部分レスポンスからポーリング属性を除去した
- 外部CLIログをRun単位へ行ストリームで集約し、DBの`run.log_path`を起点に`DATA_DIR`内の通常ファイルだけを末尾表示・マスク済み全文ダウンロードできるようにした
- 待機中Taskの即時キャンセル、実行中Taskの協調キャンセル、Run一括キャンセルを確認ダイアログ付きで既存機構へ接続した。cancelled Runは遅延完了で上書きされない
- レビュー②でStorage資格情報の平文再表示、一覧のN+1、ログ一括読込みを修正した。remote資格情報3項目はwrite-only、一覧は集約クエリ、ログは上限付き・行単位となった
- Part C完了確認で全366テスト、Ruff、mypyが成功した

## 次にやること

1. appサービスだけでAPSchedulerと既存SQLite上のSQLAlchemyJobStoreを起動する
2. Playlistごとのdiscover / download独立cron、`TZ`解釈、±ジッター、paused除外、次回予定表示を実装する
3. download実行可能時間帯を日跨ぎ対応で実装し、時間外にはTaskを投入しない
4. 起動・設定変更時のジョブ整合、直近1回だけのmisfire、削除Playlistジョブ除去を実装する
5. 同一Playlistの手動・自動実行を排他し、スケジュール側のスキップをRunへ記録する
6. 実機検証、レビュー③、全文書更新後に`checkpoint/step-12`を付ける

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

- ローカルデータ、不完全な最終ファイル、残骸、不確実な削除を避け、全操作を追跡可能にする
- 空またはエラーのdiscoverはドメインDBを変更せず、delistedはArtifactや実ファイルを変更しない
- `blocked`は原因解消後に自動復帰し、`missing`の自動再取得は既定無効、`ignored`は手動だけで変更する
- Artifactはindexだけで作り、Stagingはindex後だけ削除する。失敗・中断時は保持する
- 既存最終名は期待サイズ一致だけをpublish済み復旧とし、不一致は上書きしない
- payload、ログ、UIへ資格情報・取得元URL・Cookie値や一時パスを出さない
- 認証はホワイトリスト方式、CSRFはGET以外の全エンドポイントへ共通適用する
- CookieはPlaylist単位のオプトインで、書き戻しを保存せず平文はtmpfsへだけ展開する
- Web UIとCLIは併存し、CLIを自動化・デバッグ用の恒久機能として維持する
- スケジューラはappだけで動かし、workerやホストcron / systemdへ置かない
- `git push` / `gh repo create` / `gh repo edit`は履歴監査とユーザー承認後だけ許可する

## 環境メモ

- 起動: `make up`または`docker compose up -d --build`
- テスト: `make test`、lint: `make lint`
- 同期: `make sync`またはappコンテナ内の`python3 -m sluicery.cli sync ...`
- 実機資格情報、Playlist URL、Cookieはignoredかつmode 600の`.local/`だけに置く。文書・コミットへ記載しない
- `/data/staging/trailer_1080p.mov`は削除禁止。孤立検出対象を自動削除しない
- `sync.max_targets_per_run`は既定50、DBマイグレーションheadは`b8c9d0e1f2a3`
- 検証PlaylistのCookieは現在有効。平文ファイルは`.local`にだけ存在する

## 既知の落とし穴

- SQLite WALでも書込み競合は起こる。scheduler、claim、heartbeat、進捗、状態更新のtransactionを短くする
- workerは運用設定を起動時に読むため、設定変更後は該当workerの再起動が必要
- download Runは投入完了時点で成功になる。メディア取得の成否はTarget / Taskを確認する
- HTTP 403多発時は並列度を上げず停止する。Cookieを使う場合もPlaylist単位・少数で試す
- Profile自由引数よりPlaylist自由引数が後勝ちになる。検証用`--format`が残るPlaylistを別Profileへ流用しない
- `docker compose down -v`はDBとStagingを消す。通常開発では使わない
