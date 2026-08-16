# AISTATE

> このファイルはセッション間の引き継ぎ用です。
> セッション開始時に最初に読み、セッション終了時に必ず更新してください。

最終更新: 2026-08-16
対応コミット: Phase 13実装中

## プロジェクト概要

sluiceryはyt-dlpを用いた自己ホスト型のプレイリスト同期サーバー。詳細は
`docs/要件定義.md`。実装判断と実機結果は`docs/基本設計.md`、変更一覧は
`docs/変更履歴.md`を正とする。

## 現在の進捗

- [x] 1–8. 基盤、設定・DB、yt-dlp、オプション、Storage、Task、パイプライン、二相同期
- [x] 9. 単一管理者認証、CSRF、Web UI骨格、Playlist Cookie
- [x] 10. ダッシュボード、Playlist / Profile / Storage CRUD、運用設定画面
- [x] 11. Run履歴、HTMX進捗、マスク済みログ、Task / Runキャンセル
- [x] 12. app専用スケジューラ、分離cron、時間帯、ジッター、整合、misfire
- [ ] 13–20. 整合性、フォーマット検査、yt-dlp更新、retention、設定移行、フック、mount、仕上げ

## 直近の作業

- Phase 13の整合性コアを実装した。全Artifactのexists確認、不在時だけStorage単位1回の再走査、一意な末尾ID候補のDBパスrelink、missing/復帰を扱う。複数候補・Storageエラー・走査エラー/タイムアウトでは自動選択やmissing判定を行わず、ファイル操作APIを呼ばない（D-054）
- Playlist単位のmissing方針を`leave`（既定）/`redownload`/`ignore`として永続化し、Web UI・CLI・明示Target操作から選べるようにした。自動再取得は既定無効
- missing Targetと孤立ファイルを並べる整合性レポート、DBパスだけを変更する手動リンクと取消を実装した。実ファイル不変をコア/Webテストで固定した（D-055）
- delisted Itemと関連Artifactパスを表示する差分レポートを追加した。Playlistと`TZ`に沿った期間で絞り込め、画面から削除できないことをテストした
- Phase 13準備として、過去にCookieで成功した既存TargetをCookieなしで1件だけ取得試験した。Denoは検出されたがHTTP 403、生成物0件であり、Cookieが必要な取得対象があると判定した（D-053）。試験用Stagingは削除済みでDBと既存メディアは変更していない
- 現在の実DBはTarget 659件中downloaded 599、blocked 1、failed 2、unavailable 57で、Artifact 599件・約2.54GiB。Phase 8受け入れ条件#16の「downloadedが大半」は満たすが、全件完走ではない
- `app`だけでAPSchedulerを起動し、既存SQLiteのSQLAlchemyJobStoreへPlaylist IDと種別だけを永続化した。workerとホストcron / systemdにはschedulerを置かない
- Playlistごとのdiscover / download独立cron、グローバルfallback、`TZ`解釈、永続する±jitter位相、paused除外、ダッシュボードと詳細の次回予定を実装した
- `schedule.download_window`を開始含む・終了含まない時間帯として実装し、日跨ぎと開始終了同時刻（終日）を扱う。時間外の自動downloadはTaskを作らず`skipped` Runへ記録する
- 同一Playlistの手動・自動discover / downloadをSQLiteの短い書込みtransactionで排他し、自動側の競合を`active_sync`理由の`skipped` Runへ残す
- 起動時と60秒ごとにPlaylist設定と永続jobを整合する。設定不変のjobは置換せず期限超過時刻を保持し、`coalesce=true`で停止中の複数回分を復帰直後の1回へ畳む
- レビュー③で負方向jitterの重複候補、生存Runの誤回収、外部CLIとの回収競合、paused発火競合、不正schedule値のCLI保存、起動ログを修正した。重大な残存指摘はない
- 2時間00分28秒の連続実機試験でdiscover / download各24回、全48回の安全なskip、scheduled Task 0、DB lock / scheduler error 0、SQLite整合性正常を確認した。合成データは削除済み
- `RunStatus.skipped`マイグレーションのupgrade / downgrade / upgradeを実DBで確認した。全389テスト、Ruff、mypy 82ファイルが成功した

## 次にやること

1. integrity checkのCLI・UI・日次cronを既存app schedulerへ追加し、job引数へ秘密値やパスを保存しない
2. Phase 8残存3件（blocked 1、failed 2）は原因別に少量再試行し、同種エラー多発時は停止する

## 未解決・保留

| # | 内容 | 状態 |
|---|---|---|
| 1 | Alembic autogenerateがSQLite CHECK制約で偽陽性diffを出す（D-008） | マイグレーション追加時に手で除去 |
| 2 | GitHubリポジトリのpublic化、Issues / Wiki / Projects / Dependabot | 判断待ち |
| 3 | README / deploymentのclone URLが`<repo>`のまま | public化時に差し替え |
| 4 | ffmpegの`--download-sections` 1秒区間切り出しは`-11` | ffprobe通常検証は健全。D-036 |
| 5 | ローカルコミットとタグのGitHub push | 公開前監査とユーザー承認前は禁止 |
| 6 | 外部検証VMへのSSH認証が拒否される | ローカルDockerで継続。次の外部実機確認までに要確認 |

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
- schedulerはappだけで動かし、workerやホストcron / systemdへ置かない
- 自動syncの時間帯外・活動中競合はTaskを作らず`skipped` Runへ理由を記録する
- `git push` / `gh repo create` / `gh repo edit`は履歴監査とユーザー承認後だけ許可する

## 環境メモ

- 起動: `make up`または`docker compose up -d --build`
- テスト: `make test`、lint: `make lint`
- 同期: `make sync`またはappコンテナ内の`python3 -m sluicery.cli sync ...`
- 実機資格情報、Playlist URL、Cookieはignoredかつmode 600の`.local/`だけに置く。文書・コミットへ記載しない
- `/data/staging/trailer_1080p.mov`は削除禁止。孤立検出対象を自動削除しない
- `sync.max_targets_per_run`は既定50、DBマイグレーションheadは`d0e1f2a3b4c5`
- schedulerの設定反映周期は60秒、Playlist jobのmisfire猶予は24時間、既定jitterは±5分

## 既知の落とし穴

- SQLite WALでも書込み競合は起こり得る。scheduler、claim、heartbeat、進捗、状態更新のtransactionを短くする
- scheduler起動時に設定不変の永続jobを置換すると、期限超過`next_run_time`が失われmisfireが発火しない
- jitterは実発火時刻へ都度乱数を加えず、jobごとの位相として永続化する。負方向でも前回時刻をcron基準へ戻して次回を計算する
- Taskなしdownload RunはStorage事前確認中または外部CLI実行中の可能性があるため、起動時も24時間未満は孤児回収しない
- workerは運用設定を起動時に読むため、worker系設定変更後は該当workerの再起動が必要。scheduler系設定は60秒以内に反映される
- download Runは投入完了時点で成功になる。メディア取得の成否はTarget / Taskを確認する
- HTTP 403多発時は並列度を上げず停止する。Cookieを使う場合もPlaylist単位・少数で試す
- Profile自由引数よりPlaylist自由引数が後勝ちになる。検証用`--format`が残るPlaylistを別Profileへ流用しない
- `docker compose down -v`はDBとStagingを消す。通常開発では使わない
