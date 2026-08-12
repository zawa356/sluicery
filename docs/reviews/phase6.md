# Phase 6 独立レビュー

## 総評

Phase 6 の主要要件は概ね実装され、Staging 非削除、`blocked` の試行回数維持、進捗更新、claim、
ダミーTask隔離、stale回収、実機検証はいずれも確認できた。一方、初回レビューでは所有権付き
状態遷移とキャンセルに競合窓があり、Phase 7 の実処理を載せる前に修正が必要と判定した。
レビュー役は静的確認だけを行い、編集・テスト・指摘対応は実装役が行った。

## 指摘

### [重大] 一時失敗の確定処理が旧workerから新所有者を上書きできる

- 該当箇所: 初回レビュー時の `src/sluicery/db/repositories/task.py` の `mark_failed_for_retry()`
- 内容: `status=running AND worker_id=...` でSELECTした後、ORMオブジェクトを主キーだけでUPDATEしていた。SELECT後にstale回収と別workerの再claimが入ると、旧workerが新所有者のrunning Taskをpending / unavailableへ戻し、所有情報も消去できる
- 根拠: Phase 6 指示書 §4.4、§7.3、§10.4 / 要件定義 N-3、N-7 / 基本設計 §4.7
- 提案: attempts増加、上限判定、状態・時刻・所有情報更新を、`id + status=running + worker_id` 条件の単一UPDATEで行う。stale回収・再claim後に旧workerが失敗結果を返す競合テストを追加する

### [中] cancel/retry のread-modify-write競合とcancel要求の取り残しがある

- 該当箇所: 初回レビュー時の `src/sluicery/db/repositories/task.py` の `mark_blocked()` / `request_cancel()` / `retry()`
- 内容: cancelとretryは状態の読取り後にORM更新するため、その間のclaimを排除できなかった。また最後のheartbeat後にcancel要求が立ち、handlerがfailed / blockedを返すと、cancel_requestedが残って再claim不能になる経路があった
- 根拠: Phase 6 指示書 §4.4、§9.1–9.2 / 要件定義 §10.3、N-3、N-7
- 提案: cancel/retryも状態条件付き単一UPDATEとrowcount判定にする。retryable / blocked遷移時はcancel要求を優先してcancelledへ確定し、claimとの競合と最終heartbeat直後のcancelを試験する

### [中] Phase 6 の完了条件はまだ満たされていない

- 該当箇所: 初回レビュー時の `docs/phase6_指示書.md` §17、`AISTATE.md`、`docs/reviews/`
- 内容: 実機検証20項目、236件のテスト、Ruff、mypy、マイグレーション往復は記録済みだったが、本レビューの保存、上記指摘対応、文書コミット、`checkpoint/step-06` が未完了だった
- 根拠: Phase 6 指示書 §16、§17.23–24 / CLAUDE.md §8.3
- 提案: 本レビューを保存し、指摘対応と再検証結果を記録・コミットした後、完了状態でタグを付ける

### [軽微] 実装・テスト・文書のコミット粒度が計画より粗い

- 該当箇所: `258c323`、`ed7c262`、`4da571c`、`c8bec30`
- 内容: repositoryの状態遷移、workerのshutdown / heartbeat / stale回収などが各1コミットにまとまり、テストと対応文書も後続コミットへ分離された。Phase 6 指示書の14段階計画と、対応文書を実装コミットへ含めるルールより粗い
- 根拠: CLAUDE.md §2.1、§4.2 / Phase 6 指示書 §16
- 提案: 履歴書換えは行わず逸脱を記録する。以後は状態遷移、shutdown、heartbeat、stale回収、進捗を意味単位で分け、対応テスト・文書を同じコミットに含める

## 観点別の確認結果

- 要件との齟齬: 初回レビューでは所有権付き失敗遷移とキャンセル競合に齟齬あり。`blocked` のattempts非消費、worker class分離、backoff、依存失敗伝播は整合している
- 設計原則違反: Phase 6コードにStaging削除処理はなく、既存ファイルを保持した実機記録もある。初回の所有権競合は将来の二重実行につながりうるため修正対象とした
- 前フェーズの前提の破壊: yt-dlp ready待機、UTC日時、Runnerのプロセスグループ終了、degraded起動は維持されている。起動時の一括failed化は残らず、閾値付きstale回収へ統合されている
- ドキュメントと実装の乖離: 初回は基本設計 §4.7 の所有権保証が失敗再試行経路で成立していなかった。対応後は一致している
- ドキュメント更新漏れ: 初回は本レビュー文書だけ未作成。指定文書は対応後に全て更新した
- 完了条件の未達: 初回はレビュー保存、指摘対応、文書コミット、step-06タグが未完了。対応と再検証後に完了した
- 用語のドリフト: `Task`、`blocked`、`pending`、`Staging`、`worker_class` 等は要件定義と整合し、顕著なドリフトはない
- コミット粒度: 上記軽微指摘のとおり。安全のため既存履歴は書き換えず、今後のフェーズへ申し送る
- 未記録の設計判断: D-029〜D-035で主要判断は記録済み。worker IDのnonceとruntime build target修正も実機検証節・変更履歴・AISTATEに記録済み

## 対応

全指摘へ対応または理由を記録した。

- [重大] `mark_failed_for_retry()` を所有権条件付き単一UPDATEへ変更した。SQLのCASE式でcancel優先、attempts増加、上限判定、状態・時刻・所有情報の更新を一文で確定し、stale回収・再claim後の旧worker更新を拒否する回帰試験を追加した
- [中] `request_cancel()` と `retry()` を状態条件付き単一UPDATEへ変更した。`mark_failed_for_retry()` と `mark_blocked()` は、最後のheartbeat後に立ったcancel要求をcancelledとして優先確定する。cancel / retryとclaimを同時実行する競合試験、およびcancel取り残しの回帰試験を追加した
- [中] 本レビューと対応内容を保存し、全件再検証後に完了状態をAISTATEへ反映して`checkpoint/step-06`を付けた
- [軽微] 既存コミットは書き換えず、本記録をPhase 7以降のコミット粒度改善に利用する

対応後の検証は `make test` 240件、Ruff、mypyが全件成功した。実機検証20項目と実DBの
upgrade → downgrade → upgradeは初回レビュー前に完了済みで、検証Task・設定上書きも削除済み。
対応コードをruntimeイメージへ再ビルドした後も、3サービスの正常稼働、DB head、検証用Taskの
既定無効、実行中Taskなしを確認した。
