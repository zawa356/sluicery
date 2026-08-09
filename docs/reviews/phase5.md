# Phase 5 独立レビュー

## 総評

Storage 境界・クレデンシャル経路・SMB 限定表記は概ね要件に沿い、実環境の識別情報も文書差分からは
検出しなかった。一方、local publish の失敗時に Staging 元パスを先に失う重大な問題、競合時の
上書き、接続テスト deadline、実機試験 #15 の代替方法などがあり、初回レビュー時点では未完了と判定した。
レビュー役は静的確認だけを行い、編集・テスト・指摘対応は実装役が行った。

## 指摘

### [重大] local publish が最終化前に Staging 元ファイルを移動している

- 該当箇所: `src/sluicery/storage/local.py` の publish、`tests/test_storage_adapters.py`、`docs/基本設計.md` §4.5
- 内容: 同一 filesystem では最初の `os.replace(src, temp)` で Staging 元を消しており、検証・最終化失敗や強制終了時に元パスを失う。remote は成功後まで元を保持するため契約も不一致
- 根拠: 要件定義 §1.4 原則1、§6.2、§8.2、N-3 / Phase 5 指示書 §9.1–9.5、完了条件 #13
- 提案: 元を保持して一時名へ copy / hardlink し、成功後だけ元を削除する。失敗・`BaseException` 時の元保持を試験する

### [中] `overwrite=False` が競合時の上書きを防げない

- 該当箇所: `src/sluicery/storage/local.py`、`src/sluicery/storage/remote_rclone.py` の最終化
- 内容: 最終 `exists()` と rename / moveto の間に別処理が最終名を作ると、既存ファイルを置換しうる
- 根拠: 要件定義 §1.4 原則1、N-3 / Phase 5 指示書 §9.3、完了条件 #14
- 提案: local は原子的 no-replace、remote は rclone 側の no-clobber 手段を使い、競合試験を追加する

### [中] 接続テストの30秒制限が各 subprocess に適用されている

- 該当箇所: `src/sluicery/storage/remote_rclone.py` の `_run()` / `test_connection()`
- 内容: listing、copy、cat、delete と password obscure が各30秒となり、全体が30秒を大幅に超えうる
- 根拠: Phase 5 指示書 §3.3「接続テスト用の全体タイムアウト」、§8
- 提案: 開始時に単一 deadline を作り、各段階へ残り時間を渡す

### [中] 実機試験 #15 の閉鎖ポート試験は指定手順と同等ではない

- 該当箇所: 初回レビュー時の `docs/基本設計.md`、`docs/deployment.md`、`docs/変更履歴.md`、`AISTATE.md`
- 内容: 初回接続拒否では、確立済み SMB 転送中の接続喪失時の分類・一時名・元保持を確認できない
- 根拠: Phase 5 指示書 §16.3 #15、完了条件 #19
- 提案: サーバーを停止できない場合は、転送開始後のクライアント通信遮断等で established connection の喪失を再現する

### [中] 実機検証後の最終監査とレビュー記録が未完了

- 該当箇所: 初回レビュー時の `AISTATE.md`、`docs/reviews/`
- 内容: 実機後監査と `docs/reviews/phase5.md` が未完了のまま進捗 #5 を完了表示していた
- 根拠: Phase 5 指示書 §16.2、完了条件 #23 / `CLAUDE.md`
- 提案: 指摘対応、全件試験、履歴監査、gitleaks、本記録の保存後に完了扱いとする

### [軽微] 読み戻し内容不一致時に分類が `ok` になる

- 該当箇所: `src/sluicery/storage/remote_rclone.py` の接続テスト read-back 判定
- 内容: `rclone cat` が終了コード0でも内容不一致なら stage は失敗だが、成功 result を再利用して `ok/ok` になる
- 根拠: 要件定義 §6.4 / Phase 5 指示書 §8.1–8.2、完了条件 #10
- 提案: `failed/content_mismatch` を明示し、回帰試験を追加する

### [軽微] BaseRunner 抽出で YtdlpRunner の stdin 挙動が変わっている

- 該当箇所: `src/sluicery/runner/base.py`、`src/sluicery/downloader/ytdlp.py`
- 内容: 抽出前は stdin 継承、抽出後は `DEVNULL` になっていた。フレーミング、ロケール、進捗、分類、タイムアウトは維持されている
- 根拠: Phase 5 指示書 §2.4、§21
- 提案: 基底側で stdin 方針を選べるようにし、yt-dlp は従来の継承を回帰試験する

## 観点別の確認結果

- 要件との齟齬: 初回レビューでは local 元保持、全体 deadline、実機 #15、競合上書き拒否に齟齬あり
- 設計原則違反: local 失敗後の Staging 元パス喪失が原則1と中断再開性に反していた
- 前フェーズの前提の破壊: yt-dlp の主要挙動は維持。stdin だけ未記録の差分があった
- ドキュメントと実装の乖離: local の元削除時点と #15 の「同等試験」表現に乖離があった
- ドキュメント更新漏れ: 指定文書は更新済み。本レビュー保存と実機後監査が残っていた
- 完了条件の未達: 初回時点で #13、#14、#19、#23 が未達または要修正
- 用語のドリフト: 重大なドリフトなし
- コミット粒度: リファクタ、Runner、共通型、各 adapter、CLI、実機修正、文書が概ね意味単位
- 未記録の設計判断: D-024〜D-028 は記録済み。閉鎖ポートの同等性判断だけ不適切だった
- クレデンシャル: 平文は stdin、暗号化DB、プロセス内メモリに限定。実値の引数・ログ・文書混入なし
- protocol 表記: 実装・検証済みは SMB のみ。その他と mount を対応済みとしていない
- 実環境識別情報: 文書はプレースホルダだけで、実ホスト/IP/share/user/password なし

## 対応

全指摘へ対応した。

- [重大] local 元保持: Staging 元を保持したまま hardlink、mount 境界・非対応時は copy で一時名を作る。検証と最終化成功後だけ元を削除する。通常失敗と `KeyboardInterrupt` の双方で元保持・最終名不在を試験した
- [中] 競合上書き: local は Linux `renameat2(RENAME_NOREPLACE)`、remote は `rclone moveto --ignore-existing` と一時名消滅確認を使用する。最終確認後に競合ファイルが現れる回帰試験を両方へ追加した
- [中] 全体 timeout: password obscure を含む単一 deadline から各 subprocess の残り時間を計算する。疑似 clock で 28→20→10→1秒と減ることを試験した
- [中] 実機 #15: 8 GiB 転送で rclone 稼働を確認後、app コンテナだけを compose ネットワークから一時切断した。`unreachable/timeout`、最終名なし、Staging 元保持、一時名報告、rclone 残留0を確認し、直後に再接続した。サーバー自体は停止していないが、確立済み接続喪失を安全に再現した
- [中] 監査・記録: 本ファイルを追加。最終履歴監査と gitleaks はレビュー対応コミット後に実施する
- [軽微] 内容不一致: `failed/content_mismatch` を返す回帰試験を追加した
- [軽微] yt-dlp stdin: `BaseRunner` に明示的な継承指定を追加し、`YtdlpRunner` は抽出前と同じ stdin 継承へ戻した

対応後の検証は `make test` 210件、Ruff、mypy が全件成功。新コードで SMB の正常 publish・既定の
上書き拒否、local の cross-mount publish・上書き拒否を再実行し、生成物と資格情報入り試験レコードを
削除した。レビュー役の禁止事項に従い、対応と再検証は実装役が行った。
