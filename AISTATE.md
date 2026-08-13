# AISTATE

> このファイルはセッション間の引き継ぎ用です。
> セッション開始時に最初に読み、セッション終了時に必ず更新してください。

最終更新: 2026-08-14
対応コミット: Phase 12実装・実機検証完了（レビュー③前）

## プロジェクト概要

sluiceryはyt-dlpを用いた自己ホスト型のプレイリスト同期サーバー。詳細は
`docs/要件定義.md`。実装判断と実機結果は`docs/基本設計.md`、変更一覧は
`docs/変更履歴.md`を正とする。

## 現在の進捗

- [x] 1–8. 基盤、設定・DB、yt-dlp、オプション、Storage、Task、パイプライン、二相同期
- [x] 9. 単一管理者認証、CSRF、Web UI骨格、Playlist Cookie
- [x] 10. ダッシュボード、Playlist / Profile / Storage CRUD、運用設定画面
- [x] 11. Run履歴、HTMX進捗、マスク済みログ、Task / Runキャンセル
- [ ] 12. app専用スケジューラ、分離cron、時間帯、ジッター、整合、misfire（実装・実機検証済み、レビュー③待ち）
- [ ] 13–20. 整合性、後処理、yt-dlp更新、通知、バックアップ、フック、mount、仕上げ

## 直近の作業

- `app`だけでAPSchedulerを起動し、既存SQLiteのSQLAlchemyJobStoreへPlaylist IDと種別だけを永続化した。workerとホストcron / systemdにはschedulerを置かない
- Playlistごとのdiscover / download独立cron、グローバルfallback、`TZ`解釈、±jitter、paused除外、ダッシュボードと詳細の次回予定を実装した
- `schedule.download_window`を開始含む・終了含まない時間帯として実装し、日跨ぎと開始終了同時刻（終日）を扱う。時間外の自動downloadはTaskを作らず`skipped` Runへ記録する
- 同一Playlistの手動・自動discover / downloadをSQLiteの短い書込みtransactionで排他し、自動側の競合を`active_sync`理由の`skipped` Runへ残す
- 起動時と60秒ごとにPlaylist設定と永続jobを整合する。設定不変のjobは置換せず期限超過時刻を保持し、`coalesce=true`で停止中の複数回分を復帰直後の1回へ畳む
- 実機で独立設定反映、一時停止・削除job除去、時間帯外、手動競合、misfire、app専用起動、DB lock非発生を確認した。合成データと一時設定は削除済み
- `RunStatus.skipped`マイグレーションのupgrade / downgrade / upgradeを実DBで確認した。scheduler focused test 11件、Ruff、mypyは成功済み

## 次にやること

1. `docs/phase9-12_指示書.md` §18のレビュー③を実施し、`docs/reviews/phase12.md`へ記録する
2. レビュー指摘をコード・テスト・文書へ反映し、`docs: レビュー③の指摘への対応`でコミットする
3. 全pytest、Ruff、mypy、実サービス、DB head、履歴・秘密情報非混入を最終確認する
4. Phase 12完了状態へ本ファイルを更新し、`checkpoint/step-12`タグを付ける（pushしない）
5. 次フェーズは要件定義 §20のPhase 13（整合性チェック・relink・missing管理）から着手する

## 未解決・保留

| # | 内容 | 状態 |
|---|---|---|
| 1 | Alembic autogenerateがSQLite CHECK制約で偽陽性diffを出す（D-008） | マイグレーション追加時に手で除去 |
| 2 | GitHubリポジトリのpublic化、Issues / Wiki / Projects / Dependabot | 判断待ち |
| 3 | README / deploymentのclone URLが`<repo>`のまま | public化時に差し替え |
| 4 | ffmpegの`--download-sections` 1秒区間切り出しは`-11` | ffprobe通常検証は健全。D-036 |
| 5 | ローカルコミットとタグのGitHub push | 公開前監査とユーザー承認前は禁止 |
| 6 | `.local/docker-server.env`のSSH認証が拒否される | ローカルDockerで継続。最終実機確認までに要確認 |
| 7 | Phase 12の数時間連続稼働 | 短時間の連続発火ではDB lockなし。長時間観測は継続課題 |

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
- workerは運用設定を起動時に読むため、worker系設定変更後は該当workerの再起動が必要。scheduler系設定は60秒以内に反映される
- download Runは投入完了時点で成功になる。メディア取得の成否はTarget / Taskを確認する
- HTTP 403多発時は並列度を上げず停止する。Cookieを使う場合もPlaylist単位・少数で試す
- Profile自由引数よりPlaylist自由引数が後勝ちになる。検証用`--format`が残るPlaylistを別Profileへ流用しない
- `docker compose down -v`はDBとStagingを消す。通常開発では使わない
