# Phase 7 独立レビュー

## 総評

5段パイプライン、Staging保持、blockedのattempts不変、Artifactのindex限定確定、local / SMB実機検証は
概ね要件と整合している。一方、終端TaskとTargetの状態同期に重大な欠落があり、Phase 8の対象選択で
永続的に取り残されるため、Part Bへ進む前に修正が必要と判定した。レビュー時点のテストは264件成功。

## 指摘

### [重大] 終端TaskがTargetを実行中またはfailedのまま取り残す

- 該当箇所: `src/sluicery/tasks/worker.py:275`、`src/sluicery/db/repositories/task.py:160`、`src/sluicery/db/repositories/task.py:210`
- 内容: handlerが`FAILED`を返してTaskの最大試行回数へ達するとTaskは`unavailable`になるが、Targetはhandlerが直前に設定した`failed`のままになる。handlerが直接`UNAVAILABLE`を返すpostprocess非対応経路、実行中Taskのcancel、待機中Taskの直接cancel、stale回収の上限到達もTargetを確定しない。特にcancel後はTargetが`downloading` / `processing`のまま後続Taskだけcancelledとなり、Phase 8のdownload対象（pending / failed）から永久に外れる
- 根拠: 要件定義 §7.2、§8.1–8.2、§10.3 / Phase 7-8指示書 §8.3、§10、完了条件13
- 提案: Task終端の所有権付き更新が成功した場合だけ、target参照TaskのTargetを条件付き単一UPDATEで同期する。再試行上限・直接unavailable・stale上限はTarget unavailable、cancelは再試行可能なfailedへ戻す。handler例外によるfailedもactive状態のTargetをfailedへ落とし、既にhandlerが更新したfailedを二重加算しない。実worker、待機cancel、staleの回帰試験を追加する

### [中] Artifactのformat_idへffprobeのstream indexを記録している

- 該当箇所: `src/sluicery/tasks/handlers/verify.py:104`
- 内容: ffprobeの`streams[].index`はコンテナ内stream番号であり、yt-dlpのformat IDではない。現在のArtifactには動画・音声とも先頭streamの`"0"`がformat_idとして入り、利用者が選択した形式を表す値として誤っている
- 根拠: 要件定義 §7.1 artifact / Phase 7-8指示書 §8.2 / 設計原則5（追跡可能性）
- 提案: downloadのafter_move出力でyt-dlpの`format_id`を構造化して受け渡し、verifyはその値を保持する。少なくともstream indexをformat_idとして生成しない。単一形式と結合形式のpayload試験を追加する

### [軽微] publish復旧の不一致停止とindex cleanup例外の回帰試験がない

- 該当箇所: `tests/test_publish_handler.py:138`、`tests/test_index_handler.py:112`
- 内容: 同サイズ既存ファイルの成功試験はあるが、サイズ不一致時に上書きせずfailedとなる安全側分岐を試験していない。またcleanup失敗試験という名前のテストは`delete_staging=False`を確認するだけで、`unlink()`のOSErrorを通していない
- 根拠: Phase 7-8指示書 §6.4、ユーザー承認済みpublish復旧方針 / CLAUDE.md §3.1
- 提案: 不一致既存サイズでadapter.publish未呼出し・Staging保持・Target failedを確認し、unlinkを失敗させてもArtifact / Target / Task成功を覆さない試験を追加する

### [中] Phase 7の完了条件はレビュー時点では未達

- 該当箇所: Phase 7-8指示書 §10、§20、`AISTATE.md`
- 内容: 実装と実機検証17項目は記録済みだが、本レビュー指摘の対応、対応後の全件再検証、`checkpoint/step-07`が未完了
- 根拠: Phase 7-8指示書 §0.3、§10、§19、§20
- 提案: 指摘を修正し、test / lint、秘密情報混入検査、runtime動作確認後にレビュー対応を追記してタグを付ける

## 観点別の確認結果

- 要件との齟齬: 終端TaskとTarget状態同期、format_idの意味に上記齟齬あり。5段依存、分類、duration非判定、空postprocess、Artifact確定時点は整合
- 設計原則違反: Stagingの削除はindex内の対象ファイルだけで、download / verify / publish失敗時の削除経路はない。publishは一時名とno-replaceを維持。Target取り残しは追跡可能性と再開性に反する
- 前フェーズの前提の破壊: Task所有権条件、blocked attempts不変、shutdown pending、stale attempts加算、Runnerのプロセスグループ終了は維持。キャンセル後続伝播の不足はレビュー前に回帰試験付きで修正済み
- ドキュメントと実装の乖離: format_id以外の主要責務は基本設計と一致。`staging orphans`は実在し、自動削除APIを持たない
- ドキュメント更新漏れ: README、基本設計、変更履歴、AISTATE、troubleshootingは更新済み。本レビューの対応結果とstep-07のみ未完了
- 完了条件の未達: 指摘対応、対応後再検証、タグが残る。実機17項目とレビュー前test / lintは完了
- 用語のドリフト: Task / Target / Item / Artifact / Staging / blocked / unavailableは要件定義と整合。顕著なドリフトなし
- コミット粒度: 機能別コミットは概ね指示書計画に沿う。レビュー前に見つけたpublish復旧、continue、cancel伝播は独立修正コミットでテスト・変更履歴を同梱している
- 未記録の設計判断: D-036〜D-040にffmpeg、状態写像、duration、publish復旧、Artifact確定を記録済み。ユーザー指定URLやSMB資格情報は記録していない

## 対応

全指摘へ対応した。

- [重大] `core/target_state.py`を追加し、所有権付きTask更新が成功した後だけTargetを条件付き単一UPDATEで同期するようにした。再試行上限、直接unavailable、stale上限、実行中／待機中cancel、handler例外を回帰試験で固定した。handlerが既にfailedとretry_countを更新した場合は二重加算しない
- [中] after_moveでfile pathとyt-dlpのformat IDを別の構造化フレームとして取得し、download → verify → indexへ伝播するよう変更した。ffprobe stream indexの代入を削除し、実パイプラインでformat_id=`quicktime`を確認した
- [軽微] 既存最終名のサイズ不一致時にpublishを呼ばずfailed / Staging保持となる試験と、index後の`unlink()`がOSErrorでもArtifact / Target確定を覆さない試験を追加した
- [中] 全指摘対応後にruntime全サービスを再ビルドし、`make test` 272件、Ruff、mypyが成功した。秘密情報混入検査と稼働状態を確認してから`checkpoint/step-07`を付ける
