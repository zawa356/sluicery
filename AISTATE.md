# AISTATE

> このファイルはセッション間の引き継ぎ用です。
> セッション開始時に最初に読み、セッション終了時に必ず更新してください。

最終更新: 2026-08-13
対応コミット: Phase 7レビュー対応完了（checkpointタグ前）

## プロジェクト概要

sluicery はyt-dlpを用いた自己ホスト型のプレイリスト同期サーバー。詳細は
`docs/要件定義.md`。実装判断と実機結果は`docs/基本設計.md`、変更一覧は
`docs/変更履歴.md`を正とする。

## 現在の進捗

- [x] 1–6. 基盤、設定・DB、yt-dlp、オプション、Storage、Taskキュー
- [x] 7. `download → verify → postprocess → publish → index`パイプライン
- [ ] 8. 二相同期（discover / download）、状態遷移（次の作業）
- [ ] 9–20. Web UI以降

Phase 7は実装、実機検証17項目、独立レビューと全指摘対応まで完了した。
`checkpoint/step-07`タグを付けてからPhase 8へ進む。

## Phase 7で実装したもの

- Target単位の5 Taskを共通`work_id`と依存関係付きで一括投入する`tasks/pipeline.py`
- worker所有権を保った結果payload受け渡しと、実行時だけの`_execution`コンテキスト
- 実download、ffprobe verify、空postprocess、local / remote publish、indexハンドラ
- Artifactのindex限定・冪等確定、Target downloaded、index後だけのStaging削除
- publish済み最終名の同サイズ復旧、Storage blockedのattempts不変な自動再開
- `target_downloaded` / `artifact_published`のevent_log Hook発火点
- 読み取り専用の`staging orphans`検出CLI
- `pipeline.verify_timeout_sec`、`sync.max_targets_per_run`、
  `sync.delete_staging_after_index`の運用設定
- Taskキャンセルの後続チェーン伝播と、downloadへの明示的な`--continue`

## Phase 7検証結果

- D-015直リンクでlocalとSMBの5段パイプラインがattempts 0で完走
- SMB到達不能でblocked / attempts 0、復旧後に同じTaskが自動再開して完走
- 存在しない動画はunavailable、後続4 Task cancelled
- download中断時は`.part`を保持しattempts 0のpending、再起動後に完走
- 破損ファイルのverify失敗時にStagingを保持
- Blender公式PeerTube素材からOpus、タグ、`metadata_block_picture`を生成
- 孤立ファイルは4件を検出し、自動削除なし。Phase 3のMOV以外にPhase 4由来3件が残っていた
- レビュー対応後の`make test` 272件、Ruff、mypy成功

詳細な17項目は`docs/基本設計.md`の「Phase 7 開発機・SMB実機検証」を参照。

## 次にやること

1. `checkpoint/step-07`を付与
2. Phase 8のdiscover Task、Item upsert・空振り判定・delisted / 再登場を実装
3. download選択・投入上限・Storage事前確認・Run統計・`sync` CLI / dry-runを実装
4. `.local/test_playlists.txt`の全URLを実測し、可能な範囲でPart B 19項目を検証

## 未解決・保留

| # | 内容 | 状態 |
|---|---|---|
| 1 | Alembic autogenerateがSQLite CHECK制約で偽陽性diffを出す（D-008） | マイグレーション追加時に手で除去 |
| 4 | GitHubリポジトリのpublic化 | 見送り中・判断待ち |
| 5 | Issues / Wiki / Projectsの要否 | 未確認 |
| 6 | Dependabot alertsの要否 | 未確認 |
| 7 | README / deploymentのclone URLが`<repo>`のまま | public化時に差し替え |
| 8 | ffmpegの`--download-sections` 1秒区間切り出しは`-11` | ffprobe通常検証は健全。D-036 |
| 9 | ローカルコミットとタグのGitHub push | 公開前監査とユーザー承認前は禁止 |
| 10 | D-022 YouTube素材が検証時にHTTP 403 | Blender公式PeerTube素材で代替。サイト負荷を避け反復しない |

## 重要な前提

- 配信元での削除をローカルファイル削除へ伝播させない
- 空のdiscover結果ではdelisted判定を行わない
- `blocked`はTask / Targetの試行回数を消費しない
- shutdownはTaskをattempts不変のpendingへ戻し、Stagingを保持する
- stale回収だけはattemptsを1増やす
- download失敗、verify失敗、publish失敗ではStagingを削除しない
- Storage publishは一時名で最終化し、既定で上書きしない
- publish後もStaging元を保持し、Artifact / Target確定後のindexだけが削除する
- final既存時は期待サイズ一致だけをpublish済み復旧とし、不一致は停止する
- Artifactはindexだけが作成し、現バージョンでは`checksum=null`
- payloadへ資格情報・取得元URLを保存しない
- `[<source_id>]`は拡張子直前に固定し、relinkが依存する
- 運用パラメータはsettingテーブル、コード既定は`core/settings.py`
- `MEDIA_ROOT`はホスト側パス、コンテナ内境界は`/mnt/media`
- yt-dlp / rclone / ffmpegと子孫はプロセスグループ単位で終了する
- `git push` / `gh repo create` / `gh repo edit`は履歴監査とユーザー承認後だけ許可

## 環境メモ

- 起動: `make up`または`docker compose up -d --build`
- テスト: `make test`、lint: `make lint`
- CLI: `docker compose exec app python3 -m sluicery.cli ...`
- 実機用のSMB / Docker SSH / Playlist URLは、ignoredかつmode 600の`.local/*.env`と
  `.local/test_playlists.txt`だけにある。値を文書・コミット・通常ログへ出さない
- Phase 7検証用Storage / Playlist / Profile / Item / Target / Task / Artifactは開発DBに残っている
- `/data/staging/trailer_1080p.mov`とPhase 4由来3ファイルは孤立として検出されるが削除しない
- `worker.blocked_retry_sec`と`download.limit_rate`は検証後にコード既定へ戻した
- DBマイグレーションheadは`e4a1f7b9c203`

## 既知の落とし穴

- SQLite WALでも書込み競合は起こる。claim、heartbeat、進捗、状態更新のtransactionを短くする
- workerは運用設定を起動時に読むため、設定変更後は該当workerの再起動が必要
- Profile自由引数よりPlaylist自由引数が後勝ちになる。検証用`--format`が残ったPlaylistを
  別Profileの実測へ流用しない
- `docker compose down -v`はDBとStagingを消す。通常開発では使わない
- Docker / WSLでは異なるmountが同じ`st_dev`を返し得るため、local publishは実際の
  `EXDEV`でcopyへフォールバックする
