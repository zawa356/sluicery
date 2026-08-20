# Phase 16–18 独立レビュー（retention・設定移行・フック）

## 総評

Phase 16のretention、Phase 17の設定エクスポート / インポート、Phase 18のフック機構を、
指示書 §15の削除安全性・秘密非混入・副作用分離を重点に静的レビューした。初回および
修正後の再レビューで検出した重大・中の指摘はすべて回帰試験付きで対応し、最終差分に
残存する重大・中の指摘はない。

## retentionの指摘と対応

### [重大・対応済み] dry-run後に同じpathの別実体を削除できた

- 内容: 当初はDB snapshotだけを再検証していたため、同じ相対pathへのファイル差替え、
  symlink、Storage設定の変更により、確認画面と異なる実体を削除し得た
- 対応: execute用dry-runで実体のsize・mtime・SHA-256・file IDを取得し、Artifact、Target、
  Storage更新時刻とStorage設定fingerprintを署名対象へ追加した。開始時・各削除直前・
  削除後にCASし、検証済みのdetached Storage snapshotからAdapterを作る
- 対応: localはsymlinkを拒否し、同一directoryのUUID quarantineへno-replace renameした後に
  実体を再照合する。remoteも強いSHA-256を取得できないbackendでは拒否し、quarantineへ
  移動・再照合してから単一objectだけを削除する
- 確認: path / Storage / Target差替え、symlink、hash不足、元path・quarantineの同時再作成を
  拒否し、既存実体を上書きしない障害注入試験を追加した

### [重大・対応済み] 削除と監査logの間にcrash windowがあった

- 内容: 当初はファイル削除後にだけ監査logを書いており、その間の停止では何を削除したか
  追跡できなかった
- 対応: path、quarantine path、実体識別情報、Storage fingerprintを含む`delete_intent`を
  先にwrite / flush / fsyncし、削除後に`deleted`を同様に記録する二相auditへ変更した
- 対応: 未完了intentは次のexecute用previewで読み取り専用検出し、422と手動確認案内を表示する。
  指示書の確認前移動禁止に従い、自動復旧・自動移動は行わない

### [中・対応済み] 大容量実体hashがWeb event loopを停止し得た

- 内容: execute用previewと再計画がasync route上で同期I/Oを直接実行していた
- 対応: DB sessionを内部で開く同期helperへ計画構築全体を切り出し、threadpoolで実行する

## 設定エクスポート / インポートの指摘と対応

### [重大・対応済み] blacklist型の除外では秘密を取りこぼし得た

- 内容: Storageの未知key、自由入力yt-dlp引数、設定URLからtoken等を出力し得た
- 対応: Storage kind別のpositive allowlistと値schemaへ変更した。自由入力yt-dlp引数、
  postprocess設定、`ytdlp.smoketest_url` override、秘密を含み得るsource URLは出力せず、
  要再入力として示す。import側も同じ境界で未知key・自由入力・不正URLを拒否する

### [重大・対応済み] import文書の再入力markerで既存secretを再利用できた

- 内容: 文書側が`requires_credentials` / `requires_cookie_reentry`をfalseにすると、既存の
  暗号化資格情報やCookieを新しい接続先へ適用し得た
- 対応: markerを権限判断に使わない。Storage overwriteでは資格情報を常に消去しremoteを無効化、
  Playlist overwriteではCookieを常に消去・無効化する。retention有効設定も必ず無効へ戻し、
  retention画面のdry-runを再要求する

### [中・対応済み] importでretention有効化dry-runを迂回できた

- 内容: 任意dictを検証せず保存し、`enabled: true`を直接反映できた
- 対応: `RetentionPolicy`で厳密検証し、有効な入力でもimport適用時は無効化する

## フックの指摘と対応

### [中・対応済み] 不正なhooks.yamlが本体処理を停止した

- 内容: Hook構築時の設定例外がWeb、CLI、workerの起動や操作へ伝播し得た
- 対応: 不正設定は安全なエラーだけを記録し、空購読へfallbackする。runtime DB失敗、queue飽和、
  submit失敗も本体から隔離する

### [中・対応済み] dry-runや一部の終端経路でイベント意味が不正だった

- 内容: discover dry-runがitemイベントを出し、Web / CLI取消、手動リンク取消等の状態遷移で
  必要な終端・missingイベントが欠けていた
- 対応: itemイベントは実差分適用時だけ発火し、取消・missing・stale / orphan終端を補完した。
  `artifact_missing`は状態遷移時だけ発火し、shutdownはproducer停止後にpending Hookをflushする

## 観点別の最終確認

- retentionは既定無効。有効化と実行の両方に署名付きdry-run、TTL、二段確認がある
- 20件上限、過半数guard、Playlist排他、DB / Storage / 実体CAS、二相auditがある
- 差分レポートには削除経路がない。実検証ではメディアの削除・移動を行っていない
- exportは実環境で10,675 bytes、Storage 3、Profile 7、Playlist 8、割当19、設定1を生成し、
  検査対象の秘密値一致0、禁止フィールド一致0、DB不変だった
- importのskip / overwrite / create previewはすべてDB不変で、ファイル操作経路を持たない
- 12イベントは単一workerのbounded queueで非同期・順序付きに記録され、失敗は本体へ伝播しない
- payloadはイベント別allowlistとURL / 秘密key除外を通る。検証環境の3サービスで全12購読を確認した
- 最終test imageで全472テスト、Ruff、mypy 86 source filesが成功した

## 残存事項

重大・中の残存指摘はない。Starlette TestClientのhttpx移行警告1件は既知であり、機能失敗ではない。
