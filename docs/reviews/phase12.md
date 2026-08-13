# Phase 12 独立レビュー（スケジューラ）

## 総評

app専用APScheduler、永続job、分離cron、`TZ`、時間帯、jitter、misfire、設定整合、手動syncとの
排他を要件・Phase 9–12指示書と照合した。レビューで検出した中指摘5件と軽微指摘1件は、いずれも
回帰試験または実サービス確認付きで対応済みである。重大な残存指摘はない。

## 指摘

### [中・対応済み] 負方向jitterが同じcron基準時刻を再発火し得た

- 該当箇所: `src/sluicery/scheduler/__init__.py`の`SymmetricCronTrigger`
- 内容: APSchedulerは実際にずらした発火時刻を`previous_fire_time`として次回計算へ戻す。単純に負の乱数を加える実装では前回値が本来のcron時刻より前になり、同じcron時刻を再び候補にして重複発火し得た
- 根拠: Phase 9–12指示書 §16.4 / §17。自動実行の多重化を避ける必要がある
- 対応: job登録時に±範囲のランダムな位相を一度決め、前回実時刻をcron基準時刻へ戻してから次回を計算するversion 2 triggerへ変更した。時刻加減算はUTC上で行い、夏時間のある`TZ`でも実秒差を維持する。旧versionは起動時整合で再登録する
- 確認: 最大負jitterで06:00を05:55にずらした後、次回が同じ06:00ではなく11:55へ進む回帰試験を追加した。実jobstoreの全triggerがversion 2となり、各`next_run_time`から次回が必ず前進することを確認した

### [中・対応済み] 定期整合が正当に実行中のTaskなしRunを孤児回収し得た

- 該当箇所: `SchedulerService.reconcile()` / `recover_orphan_runs()`
- 内容: 60秒ごとの整合でもRun回収していたため、Storage事前確認などがworker stale閾値を超え、まだTaskを作っていないdownload Runをfailedへ変え得た。その間だけ同一Playlist排他が外れ、別syncが開始できる
- 根拠: Phase 9–12指示書 §16.8 / §17。起動時の異常残骸回収と、生存処理の定期判定を混同しない
- 対応: Run回収をschedulerがpausedの起動時だけ行い、60秒整合はjob設定の反映と除去だけに限定した
- 確認: 起動後に古いTaskなしRunを作って`reconcile()`しても`running`を維持する回帰試験を追加した

### [中・対応済み] app再起動と外部CLIのStorage事前確認が競合し得た

- 該当箇所: `SchedulerService.recover_orphan_runs()`
- 内容: scheduler開始前の起動時回収であっても、別プロセスの恒久CLIがdownloadを実行中なら、TaskなしRunは正当であり得る
- 根拠: Phase 9–12指示書 §10のCLI / UI併存と§17の手動・自動共存
- 対応: Taskなしdownload Runは安全側に24時間保留し、それを超えた起動時だけ孤児としてfailedへ確定する。discoverはRunとTaskを同一transactionで作るため従来のstale閾値で回収する。保留中でもUIから明示キャンセルできる
- 確認: 10分前のTaskなしdownloadを維持し、25時間前だけを回収する試験を追加した

### [中・対応済み] paused変更と時間帯外発火の競合でskipped Runが残り得た

- 該当箇所: `SchedulerService.execute_job()` / `_record_skipped()`
- 内容: job除去まで最大60秒あるため、paused / disabledへ変更した直後に時間帯外jobが発火すると、対象外Playlistへ`skipped`履歴だけを作成できた
- 根拠: Phase 9–12指示書 §16.5。一時停止中はスケジュール対象外である
- 対応: 発火入口とskip記録直前の両方でPlaylistの存在、enabled、pausedを確認する
- 確認: paused Playlistの時間帯外downloadがRun / Taskを一切作らない回帰試験を追加した

### [軽微・対応済み] appでscheduler開始を直接確認できる起動ログが無かった

- 該当箇所: `src/sluicery/cli.py`の`_run_web()`
- 内容: loggerのINFO設定より先にschedulerを開始するため、構造上はapp専用でも通常のcomposeログから開始を判別しにくかった
- 対応: timezoneとapp-onlyだけを含む起動メッセージを標準出力へ追加した。秘密値や環境識別値は出力しない
- 確認: appログに1件、両workerログに0件であることをruntime再build後に確認した

### [中・対応済み] CLIから不正なschedule値を保存できた

- 該当箇所: `src/sluicery/core/settings.py`の`set_override()`
- 内容: Web UIはcron、window、非負jitterを検証していたが、恒久CLIは型変換だけで保存した。不正cronは該当jobを失わせ、負jitterは60秒整合全体を失敗させ、不正windowは発火ごとに例外となる
- 根拠: Phase 9–12指示書 §8.6 / §10 / §16。UIとCLIが同じDBを操作しても安全境界は一致する必要がある
- 対応: core設定保存境界で両cronをAPScheduler parser、windowを実行時と同じparser、jitterを非負条件で検証する。UIの入力時表示用検証も維持する
- 確認: 4種類の不正値を拒否し、上書き行を残さない回帰試験を追加した

## 観点別の確認結果

- app専用性: `SchedulerService`の生成は`sluicery web`のhead一致経路だけ。worker CLI、compose、entrypointに生成経路はない
- タイムゾーン: `SchedulerService.timezone`の1つの`ZoneInfo`をcron、download window、次回表示に共用し、DB timestampはUTCを維持する
- 実行可能時間帯: 開始を含み終了を含まない。日中、日跨ぎ、開始終了同時刻の終日を検査し、discoverと手動downloadには適用しない
- misfire: 設定不変のjobを置換せず期限超過時刻を保持し、24時間猶予、`coalesce=true`、`max_instances=1`で停止中の複数回分を1回へ畳む
- job整合: 起動時と60秒ごとにrunnable Playlistの2 jobへ整合し、削除、disabled、pausedのjobを除去する。設定不正なjobは登録しない
- 手動実行との排他: `BEGIN IMMEDIATE`後に活動中Run / discover Task / Target pipeline Taskを調べ、同一Playlistの開始を直列化する。自動側だけ`skipped` Runへ理由を残す
- 秘密情報: jobstoreの引数はPlaylist IDと種別だけ。URL、Cookie、Storage資格情報を保存しない
- ホスト依存: crontab、systemd timer、ホスト側scheduler設定を追加していない
- 前フェーズの前提: skippedと整合処理はTask未作成で、Artifact、Staging、最終メディアを操作しない

## 対応後の再確認

- 全389テスト成功
- Ruff成功、mypy 82 source files成功
- scheduler focused test 14件成功。対称jitterの両方向・単調性、永続job、`TZ`、paused、時間帯、排他、job除去、起動時回収、期限超過保持、実coalesceを確認
- runtime再build後、app healthy、両worker稼働、DB current / head一致、永続trigger全件version 2、jitter範囲内、前後両方向への分散、次回時刻の単調増加を確認
- appのscheduler起動ログ1件、worker側0件、SQLite lockログ0件を確認
- 2時間00分28秒の連続監視で、5分cronのdiscover / downloadが各24回発火した。全48 Runが`active_sync`理由の`skipped`、scheduled Task 0、health失敗0、サービス脱落0、SQLite quick / integrity check正常、DB lock 0、scheduler error 0だった
- 終了時メモリはapp約75MiB、両worker約57MiB、PIDは10 / 2 / 2で安定していた。合成Playlist / Run / Taskはapp停止中に対象指定で削除し、実jobだけの状態へ復元した
