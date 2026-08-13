# Phase 10–11 独立レビュー（Web UI・ログ・キャンセル）

## 総評

ダッシュボード、Playlist / Profile / Storage / 設定、Run履歴、進捗、ログ、キャンセルを
要件・Phase 9–12指示書と照合した。レビューで検出した中指摘1件と軽微指摘2件は、いずれも
回帰試験付きで対応済みである。重大な残存指摘はない。

## 指摘

### [中・対応済み] remote Storageのuser / domainが編集画面へ復号表示された

- 該当箇所: `src/sluicery/web/app.py`、`web/templates/storages/form.html`
- 内容: passwordはwrite-onlyだったが、同じ暗号化辞書のuser / domainを編集フォームへ復号して埋め戻していた。検証エラー時にも入力値を応答へ含めていた
- 根拠: Phase 9–12指示書 §14「クレデンシャルがUIに平文で表示される経路がないか」
- 提案: remoteクレデンシャル3項目を一組のwrite-only値として扱い、空欄は既存値の維持、変更時はuserとpasswordの再入力を必須にする
- 対応: 編集時と検証エラー時のuser / domain / passwordを常に空欄にし、設定済みかだけを表示した。既存値の維持と平文非表示を回帰試験で確認した

### [軽微・対応済み] Playlist / Profile一覧にN+1集計があった

- 該当箇所: `src/sluicery/web/app.py` の一覧ルート
- 内容: PlaylistごとのItem件数とProfileごとの参照件数を行ごとの追加SELECTで取得していた
- 根拠: Phase 9–12指示書 §14「N+1クエリや、大量データでの性能問題」
- 提案: `OUTER JOIN`と`GROUP BY`による各1クエリへ集約する
- 対応: 両一覧を集約クエリへ変更した。Item / Target詳細は50件ページネーションを維持する

### [軽微・対応済み] Runログの集約と全文ダウンロードがファイル全体をメモリへ保持した

- 該当箇所: `src/sluicery/tasks/worker.py`、`src/sluicery/web/app.py`
- 内容: 外部CLIログの集約と全文ダウンロードで`read_text()`を使い、ログの大きさに比例してメモリを消費した
- 根拠: Phase 9–12指示書 §13.1 / §14「大量データでの性能問題」
- 提案: 一行ずつ二次マスクしてストリーミングし、DBへ残すexcerptだけを上限付きにする
- 対応: 集約とダウンロードを行単位ストリームへ変更し、excerptを末尾4000文字に制限した。ブラウザ表示は従来どおり末尾N行だけを読む

## 観点別の確認結果

- ログのパストラバーサル: URLやフォーム値からパスを組み立てない。DBの`run.log_path`を`resolve(strict=True)`し、`DATA_DIR`内の通常ファイルだけを許可する
- ログの秘密情報: Runnerで一次マスクし、worker集約・ブラウザ表示・全文ダウンロードでも二次マスクする
- UIのクレデンシャル: Cookieとremote Storage資格情報はwrite-only。暗号化値、生値、エラー入力を画面へ戻さない
- 破壊的操作: Playlist / Profile / Storage削除、Task / Runキャンセルは確認ダイアログを持つ
- Playlist削除: DBレコードだけを対象とし、保持・関連DB削除のどちらもメディアファイルを操作しない。既存マーカーを残す試験がある
- Profile三状態: 継承 / 有効 / 無効を別値で表示・保存し、コマンドラインプレビューに由来層を表示する
- 大量データ: Item / Target / Run / Task一覧を50件でページ分割し、一覧の関連件数は集約クエリで取得する
- CSRF: GET以外の全APIRouteへ共通依存を適用し、ルート集合を構造的に検査する試験が新規ルートにも通る
- Runの意味: download Runを「投入結果」と明記し、実際の取得状態を関連Taskの状態内訳として分離表示する
- 前フェーズの前提: delisted・DB削除・キャンセルのいずれもArtifactやメディアを削除せず、Staging保持契約を維持する

## 対応後の再確認

- 全366テスト成功
- Ruff成功、mypy 82 source files成功
- ログ境界外・通常ファイル以外の拒否、二次マスク、HTMXポーリング停止、Task / Runキャンセルを回帰試験で確認
- remote Storageの保存済みuser / domain / passwordと、検証エラー時の入力値がHTMLへ現れないことを確認
