# Phase 19–20 レビュー④

実施日: 2026-08-21

対象はPhase 19–20だけでなく、Phase 1–20の要件、設計判断、受け入れ条件、tracked fileとGit履歴を含む。
レビュー中は実メディアを削除・移動せず、外部Storageへの書込みも行っていない。

## 初回指摘

| 重要度 | 指摘 | 対応 |
|---|---|---|
| 重大 | CLI editとconfig overwriteが既存`folder_name`を直接変更し、専用previewを迂回する | CLIは変更値を拒否し、overwriteは既存値を保持。新規importだけ文書値を使う回帰試験を追加 |
| 重大 | 物理move後・Artifact commit前のprocess停止やremote応答不明を再実行できない | move前にDBとfsync済みRun logへintentを保存。Playlist file lock、source / destination、強いidentityを照合し、安全な単一状態だけ回収 |
| 中 | 鍵不一致overrideがmanifest HMAC認証を省略する | overrideをCLI / Makefile / restore APIから廃止し、鍵指紋一致とHMAC検証を常に必須化 |
| 中 | 現DB WAL checkpoint前にconfig / logを書き換える | checkpointを最初のmutation前へ移動し、失敗時にDB / config / logが不変の試験を追加 |
| 中 | `dedup_hardlink`がschemaだけで動作しない | Web / CLIのopt-in、同一source ID・Profile検索、同一filesystemのno-replace hardlink、通常publish fallbackとRun logを実装 |
| 中 | `download.item_concurrency`がworkerへ反映されず、2以上の警告がない | 全network workerのDOWNLOAD claim数をDBで上限化。1以上を強制し、2以上はWebの警告確認を必須化 |
| 中 | `log.retention_days`にcleanupがない | workerで1時間ごとに終了済みRunの安全な既知logと既知runner一次logだけを削除。未完了intent、未知file、symlink、実行中TaskのあるRunは除外 |
| 中 | folder move previewがremote I/O中もSQLite read transactionを保持する | ORM値をimmutable snapshotへ移し、transactionを閉じてからStorage I/Oを実行 |

## 再確認

- folder moveはWeb通常編集、CLI edit、config overwriteの全経路で既存`folder_name`を保護する。
- intentより前に物理moveは起きない。move後停止時は`sourceなし / destination一致`だけを回収し、両方あり、
  両方なし、identity不一致では自動処理しない。生きている同一Playlist処理はfile lockにより奪わない。
- remote `moveto`が非zeroでも、source消失とdestinationの強いidentity一致を確認できた場合だけ完了として扱う。
- backupはarchive作成時と同じ鍵がなければ、書込み前に必ず停止する。checkpoint失敗でも部分適用しない。
- hardlinkは既定無効。別filesystem、remote、非対応時は既定の独立実体を通常publishで作る。
- download claim上限は複数network worker間でもSQLiteのatomic claimに適用される。
- log cleanupは`Run.finished_at`と`run-<id>.log`を照合し、DATA log root外や不正fileを削除しない。

初回対応後の再レビューで、hardlink候補のsizeのみ比較、単一workerで並列度が増えない点、cleanupが
起動時1回だけでretention監査logを保持し続ける点を追加検出した。hardlinkはStagingと既存実体のSHA-256一致を
必須化し、worker loopは設定数のhandlerを実並列実行する。cleanupは1時間ごとに障害隔離して実行し、
retention auditは`delete_intent`が全て`deleted`で閉じた終了済みRunだけを削除対象にした。

続く再レビューでは、folder moveのcommit後journal失敗、Run初期化失敗、hardlinkの外部差替え競合、
log cleanupとTask再投入の競合も検出した。DB commit後の状態をjournal障害で反転させず、初期化失敗Runを
FAILEDへ終端して生成済みlogを追跡する。hardlinkはopen FDのinodeとSHA-256へ束縛し、自分が作ったと
確認できないentryを削除しない。cleanupは`BEGIN IMMEDIATE`とfile lockでTask更新・writerと直列化した。

全対応後の独立静的再レビューで、重大・中・軽微の残存指摘がないことを確認した。

## 検証

- 全511 tests: PASS（既知のStarlette TestClient非推奨警告1件）
- Ruff: PASS
- mypy: 88 source files PASS
- 重点回帰: hardlink hash不一致fallback、実並列2、retention未完了intent保護を含めPASS
- migration: 既存revisionのみでschema変更なし。隔離DBのupgrade / downgrade base / upgradeをPASS
- 公開前監査: 履歴182 commitsを対象に危険file名、秘密pattern、環境patternを分類し、検出は既知の
  sample・test・監査手順・環境記録だけだった。gitleaks（redact有効）は検出0

## 公開前の利用者判断

tracked文書には、秘密値、内部IP、hostname、個人home pathではないものの、過去の実機検証規模や環境特性が
含まれる。公開前チェックリストどおり、これらを公開文書へ残すかは利用者が判断する。履歴書換えや削除は
このレビューでは行っていない。
