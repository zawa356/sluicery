# Phase 13 独立レビュー（整合性・relink）

## 総評

整合性チェック、relink、missing方針、手動リンク、孤立ファイル、差分レポート、日次jobを、
設計原則1とPhase 13–20指示書 §8の観点で静的レビューした。初回の重大1件・中3件、
再確認時の中1件・軽微1件はすべて回帰試験付きで対応済みである。重大・中の残存指摘はない。

## 指摘

### [重大・対応済み] Playlist絞り込み時に別Playlistの追跡済みパスへrelinkできた

- 該当箇所: `src/sluicery/core/integrity.py`
- 内容: 候補除外用の追跡パスもPlaylistで絞っていたため、同一Storage上の別Playlistが追跡する同じsource IDの実体を一意候補と誤認できた
- 対応: 点検対象Artifactと候補所有権の集合を分離し、対象Storageの全Artifactパスを常に候補から除外する
- 確認: Playlist絞り込み中も別Playlistの追跡パスを選ばず、missingとして報告する回帰試験を追加した

### [中・対応済み] 走査失敗時にTargetを誤ってdownloadedへ戻し得た

- 該当箇所: `check_integrity()`のTarget状態集約
- 内容: 同じTargetの一部Artifactが存在し、別Artifactを含むStorage走査が失敗した場合、確認済みArtifactだけで復帰判定できた
- 対応: Storage走査に失敗したArtifactが1件でも残るTargetには、missing確定だけでなく復帰判定も適用しない
- 確認: 複数Artifact Targetの部分確認と走査失敗を組み合わせ、状態と`missing_since`が不変であることを固定した

### [中・対応済み] タイムアウト後もremote走査プロセスが継続し得た

- 該当箇所: `StorageAdapter.list_recursive()`とremote rclone実装
- 内容: daemon thread側の待ちだけを打ち切る方式では、呼び出し元へtimeoutを返した後もrcloneが実行を続けた
- 対応: Adapter契約へ期限を渡し、localは再帰走査中に協調確認、remoteはRunnerの絶対・無出力timeoutでプロセスグループを停止する
- 確認: 期限がlocal / remote実装からRunnerへ伝わる回帰試験を追加した

### [中・対応済み] Storage I/O中にDB transactionを保持していた

- 該当箇所: `check_integrity()` / `list_orphan_files()`
- 内容: SMB全走査を含む読み取り中にtransactionを保持し、workerやschedulerの書込みと長時間競合し得た
- 対応: 短いread transactionで点検スナップショットを作成して終了し、Storage I/O後に短い適用transactionを開始する
- 確認: Adapter呼出し時にsessionがtransaction中でないことを回帰試験で確認した

### [中・対応済み] 走査中のworker完了へ古いmissing判定を適用し得た

- 該当箇所: `check_integrity()`の判定適用境界
- 内容: transaction分離後、走査中にdownloadが完了してArtifact / Targetが更新された場合、古いスナップショットでmissingへ戻し得た
- 対応: 適用前に元パス・候補パスの実在を再確認し、その後Artifact / Targetの`updated_at`、状態、記録パス、候補所有権を比較して一致した判定だけを適用する
- 確認: 走査中のTarget更新、Artifact更新、候補取得、元ファイル復帰の各競合を回帰試験へ追加した

### [軽微・対応済み] 競合で適用しなかった判定を件数へ含めていた

- 該当箇所: `IntegrityReport`の集計
- 内容: CAS再確認で安全に見送った判定も、初期判定の件数とissueへ残った
- 対応: 実際に適用した最終判定だけから件数とissueを集計する
- 確認: 競合回帰試験でreport件数も0になることを確認した

## 観点別の確認結果

- ファイル操作: 自動整合と手動リンクが使うStorage APIは`exists`と`list_recursive`だけで、削除・移動・publish経路を持たない
- エラー安全性: 到達不能、走査エラー、timeoutはmissing確定にもTarget復帰にも使わない
- 候補の曖昧性: 複数候補、追跡済み候補、複数Artifactが共有する1候補は自動選択しない
- 性能: 不在があるStorageだけを1実行1回再走査し、結果を実行内だけで共有する
- 手動リンク: ArtifactのDBパスとTarget状態だけを変更し、直前パスを保存して取消可能にする
- 差分レポート: delisted ItemとArtifactパスの読み取り専用表示で、削除操作を公開しない
- 再取得: Playlist既定は`leave`。`redownload`を明示した場合だけpendingへ戻す
- scheduler: app専用の引数なし日次jobで、Storageパスや資格情報をjobstoreへ保存しない

## 対応後の再確認

- focused test 70件成功、Ruff成功、mypy 83 source files成功
- 最終の全419テスト成功、Ruff成功、mypy 83 source files成功
- local / SMBの隔離した合成ファイルで、移動・改名relink、missing、復帰、IDなし孤立、手動リンク・取消、複数候補の非選択を確認した
- 実DBでAlembic downgrade / upgradeを往復し、current / headが`f2a3b4c5d6e7`で一致した
- 日次jobはID `sluicery:maintenance:integrity`、引数・キーワード引数なし、既定cron `0 3 * * *`であることを実jobstoreで確認した
- 検証用DB行とlocal / SMBファイルは対象を限定して片付け、SQLite `integrity_check=ok`、app healthy、両worker稼働を確認した

独立レビュー担当は静的確認のみを行い、上記テストと実機確認は指摘対応後に実装担当が実施した。
