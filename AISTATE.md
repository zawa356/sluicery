# AISTATE

> このファイルはセッション間の引き継ぎ用です。
> セッション開始時に最初に読み、セッション終了時に必ず更新してください。

最終更新: 2026-08-21
対応コミット: Phase 20バックアップ / リストア実装作業中（Phase 19 `b60d7da`から継続）

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
- [x] 13. 整合性、relink、missing方針、手動リンク、差分レポート
- [x] 14. Profile編集からのフォーマット検査、レート制限、URL非保持
- [x] 15. yt-dlp自動更新、強化スモークテスト、安全なロールバック
- [x] 16. retention（dry-run必須・実体CAS・二相監査付き）
- [x] 17. 設定エクスポート / インポート
- [x] 18. フック機構と12イベント発火点
- [x] 19. privileged mount / GPU整合確認
- [x] 20前半. バックアップ / リストア
- [ ] 20後半. 受け入れ条件、全体レビュー、最終整備

## 直近の作業

- `make backup` / `make restore`を実装した。SQLite backup API、config、任意logをmanifestと全file SHA-256付きarchiveへ保存し、SECRET_KEY自体、メディア、Staging、yt-dlp venvは含めない（D-061）
- restoreは上書き確認、現状態のpre-restore backup、3service停止、archive path / type / size / hash、SQLite quick_check、SECRET_KEY指紋の検証後に復元し、migration head確認後に再起動する。指紋不一致は既定で拒否する
- 実DBの読み取りbackupは3対象file・6,533,934 bytesで、archive内SECRET_KEY実値一致0。隔離restoreはStorage 3、Profile 7、Playlist 8、割当19、quick_check=ok、revision=headを確認した
- 別Compose project / volume / port / configで`make restore`をend-to-end検証した。改変前DBへ復元、余分なconfig除去、自動pre-restore backup、3service再起動とapp healthyを確認し、現行projectは変更していない
- backup manifestをSECRET_KEY由来HMACで認証し、改変archiveを拒否する。最終test imageで全494テスト、Ruff、mypy 87 source filesが成功した
- Phase 19の既定無効`compose.privileged.yaml`とCIFS / NFS kernel mount adapterを実装した。app / worker-networkだけへ必要な2 capabilityを付け、fixed sentinelと実効capabilityが揃う場合だけUI / CLI / factoryを有効にする（D-060）
- CIFS資格情報は`/run/sluicery`のmode 600一時ファイルだけに展開し、argvへ載せず直後に削除する。接続先競合、symlink、非空mountpoint、危険な設定値は拒否し、mount後の全操作はlocal adapterへ委譲する
- ローカルDockerで通常Composeの無効化、補助コマンド、非root UIDへのcapability継承、エラー分類を確認した。WSL2のbind元がprivate mountでoverlay Composeを開始できず、外部VMにも接続できないため実CIFS / NFS mountは未検証。全486テスト、Ruff、mypy 86 source filesは成功した
- Phase 18の12イベント発火点と`config/hooks.yaml`購読を実装した。組み込み`event_log`は単一worker・容量1000の非同期queueで順序を保ち、設定不正・DB失敗・queue飽和を本体から隔離する。payloadはイベント別allowlistとURL / 秘密key除外を通す（D-059）
- runtime imageを再buildし、app healthy、両worker稼働、3サービスすべてで12購読、起動ログのHook設定エラー / 未処理例外0を確認した
- Phase 17のYAML設定export / importを実装した。Storage kind別positive schemaを使い、自由入力yt-dlp引数、postprocess、smoketest URL override、秘密を含み得るsource URLは要再入力として省略する。importでは既存資格情報・Cookieを常に消去し、remote Storageとretentionを無効へ戻す（D-058）
- 実環境の読み取り検証では10,675 bytes、Storage 3、Profile 7、Playlist 8、割当19、設定1をexportし、秘密値一致0、禁止フィールド一致0、DB不変だった。skip / overwrite / create previewもDB不変で、ファイル操作は行っていない
- Phase 16のretentionをレビュー③で補強した。DB snapshotに加えてStorage設定と実体SHA-256 / file IDを署名・CASし、local / remoteをno-replace quarantineで再照合する。削除前intentと削除後resultを二相fsyncし、未完了intentは自動移動せず次回previewを停止する（D-057）
- 実環境のretentionは引き続き有効Playlist 0、候補0で、実メディアの削除・移動は行っていない
- Phase 16–18独立レビューの重大・中指摘をすべて回帰試験付きで対応し、最終再レビューの残存指摘は0。commit `ffaac62`から焼いたtest imageで全472テスト、Ruff、mypy 86 source filesが成功した（既知のStarlette TestClient警告1件）
- Phase 15の週次yt-dlp更新を実装した。実ダウンロード、Deno検出、challenge警告、default extras、固定markerのメタデータと実thumbnail埋込みを検査し、専用Stagingを必ず削除する
- 新版失敗時は直前版を未切替のまま同じスモークで検査し、成功時だけ戻す。新旧両失敗は新版を維持し、`run_failed` Hookへ安全なreasonだけを記録する（D-056）
- WebとCLIへ手動更新・ロールバックと履歴表示を追加した。app専用週次jobは引数なしで、`ytdlp.update_cron`を空にすると無効化できる
- 実環境の最新版は現行と同じ`2026.07.04`だった。強化スモークは固定metadata markerと合成M4Aへのthumbnail coverを含む全必須項目に成功し、専用Staging残骸0、最新保存ログのHTTP(S) URL 0、Storage / Artifact変更0だった
- Phase 14のフォーマット検査を実装した。Phase 4のL2〜L4合成とdiscover timeoutを再利用し、利用可能format、selector選択、推定サイズだけを表示する。全Profile共通の既定10秒間隔で制限し、入力URLは画面へ再表示せずログでも一次マスクする
- D-015の公開CC素材で実機検査し、終了コード0、format一覧・選択結果、出力先引数なし、ログへの入力URLなしを確認した。サイズ情報が無い配信元は「—」表示とする
- Phase 14対応後の全425テストが成功した
- Phase 15レビュー②の中4件・軽微2件へ対応し、全437テスト、Ruff、mypy 85 source filesが成功した。重大・中・軽微の残存指摘はない
- Phase 13の整合性コアを実装した。全Artifactのexists確認、不在時だけStorage単位1回の再走査、一意な末尾ID候補のDBパスrelink、missing/復帰を扱う。複数候補・Storageエラー・走査エラー/タイムアウトでは自動選択やmissing判定を行わず、ファイル操作APIを呼ばない（D-054）
- Playlist単位のmissing方針を`leave`（既定）/`redownload`/`ignore`として永続化し、Web UI・CLI・明示Target操作から選べるようにした。自動再取得は既定無効
- missing Targetと孤立ファイルを並べる整合性レポート、DBパスだけを変更する手動リンクと取消を実装した。実ファイル不変をコア/Webテストで固定した（D-055）
- delisted Itemと関連Artifactパスを表示する差分レポートを追加した。Playlistと`TZ`に沿った期間で絞り込め、画面から削除できないことをテストした
- `sluicery integrity check [--storage][--playlist]`、UI手動実行、既存app schedulerの日次integrity jobを実装した。永続job引数は空で、秘密値やパスを保存しない
- Phase 13レビュの重大1件・中4件に対応した。絞り込み時の追跡済み候補除外、走査失敗時の復帰抑止、Adapter期限による実走査停止、Storage I/O前後のDB transaction分離、適用直前のStorage実体・DB更新世代再確認を実装し、回帰テストを追加した
- Phase 13準備として、過去にCookieで成功した既存TargetをCookieなしで1件だけ取得試験した。Denoは検出されたがHTTP 403、生成物0件であり、Cookieが必要な取得対象があると判定した（D-053）。試験用Stagingは削除済みでDBと既存メディアは変更していない
- Phase 8残件は、既に再試行中の1件を重複投入せず、形式非互換で4回失敗済みの1件も追加試行しなかった。残る1件だけを一時Cookieで再試行したが同じHTTP 403でunavailableとなったため停止した。一時Cookieと一時ファイルは削除し、Itemのdelisted状態を復元した
- 現在の実DBはTarget 659件中downloaded 599、blocked 1、failed 1、unavailable 58で、Artifact 599件・約2.54GiB。Phase 8受け入れ条件#16の「downloadedが大半」は満たすが、#14の全件完走は検証制限のままである
- `app`だけでAPSchedulerを起動し、既存SQLiteのSQLAlchemyJobStoreへPlaylist IDと種別だけを永続化した。workerとホストcron / systemdにはschedulerを置かない
- Playlistごとのdiscover / download独立cron、グローバルfallback、`TZ`解釈、永続する±jitter位相、paused除外、ダッシュボードと詳細の次回予定を実装した
- `schedule.download_window`を開始含む・終了含まない時間帯として実装し、日跨ぎと開始終了同時刻（終日）を扱う。時間外の自動downloadはTaskを作らず`skipped` Runへ記録する
- 同一Playlistの手動・自動discover / downloadをSQLiteの短い書込みtransactionで排他し、自動側の競合を`active_sync`理由の`skipped` Runへ残す
- 起動時と60秒ごとにPlaylist設定と永続jobを整合する。設定不変のjobは置換せず期限超過時刻を保持し、`coalesce=true`で停止中の複数回分を復帰直後の1回へ畳む
- レビュー③で負方向jitterの重複候補、生存Runの誤回収、外部CLIとの回収競合、paused発火競合、不正schedule値のCLI保存、起動ログを修正した。重大な残存指摘はない
- 2時間00分28秒の連続実機試験でdiscover / download各24回、全48回の安全なskip、scheduled Task 0、DB lock / scheduler error 0、SQLite整合性正常を確認した。合成データは削除済み
- Phase 13のlocal / SMB隔離検証、レビュー対応、実DBマイグレーション往復を完了した。合成ファイルとDB行は対象限定で片付け、SQLite整合性、app healthy、両worker稼働を確認した
- Phase 13対応後の全419テスト、Ruff、mypy 83 source filesが成功した

## 次にやること

1. Phase 20 feature commitを確定し、隔離cloneでpurge → clean rebuildを検証する
2. 要件定義§19の受け入れ条件26項目、公開前監査、レビュー④を実施する
3. `checkpoint/step-18`以降もpushせず、最終履歴監査とユーザー承認を待つ

## 未解決・保留

| # | 内容 | 状態 |
|---|---|---|
| 1 | Alembic autogenerateがSQLite CHECK制約で偽陽性diffを出す（D-008） | マイグレーション追加時に手で除去 |
| 2 | GitHubリポジトリのpublic化、Issues / Wiki / Projects / Dependabot | 判断待ち |
| 3 | README / deploymentのclone URLが`<repo>`のまま | public化時に差し替え |
| 4 | ffmpegの`--download-sections` 1秒区間切り出しは`-11` | ffprobe通常検証は健全。D-036 |
| 5 | ローカルコミットとタグのGitHub push | 公開前監査とユーザー承認前は禁止 |
| 6 | 外部検証VMへのSSH認証が拒否される | ローカルDockerで継続。次の外部実機確認までに要確認 |
| 7 | mount Storageの実CIFS / NFS検証 | 外部VM接続不可かつWSL2 bind元がprivate mount。実装・unit・capability継承まで完了 |

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
- retentionは既定無効で、有効化と実行の両方にdry-runを必須とする。実メディの削除検証はユーザー判断なしに行わない
- `git push` / `gh repo create` / `gh repo edit`は履歴監査とユーザー承認後だけ許可する

## 環境メモ

- 起動: `make up`または`docker compose up -d --build`
- テスト: `make test`、lint: `make lint`
- 同期: `make sync`またはappコンテナ内の`python3 -m sluicery.cli sync ...`
- 実機資格情報、Playlist URL、Cookieはignoredかつmode 600の`.local/`だけに置く。文書・コミットへ記載しない
- `/data/staging/trailer_1080p.mov`は削除禁止。孤立検出対象を自動削除しない
- `sync.max_targets_per_run`は既定50、DBマイグレーションheadは`f2a3b4c5d6e7`
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
