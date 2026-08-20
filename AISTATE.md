# AISTATE

> セッション間の引き継ぎ用。開始時に読み、作業終了時に更新する。

最終更新: 2026-08-21
対応状態: Phase 1–20実装完了、最終受け入れ確認・レビュー④を実施中

## プロジェクト概要

sluiceryはyt-dlpを使う自己ホスト型Playlist同期サーバー。単一管理者向けWeb UI、CLI、
app専用scheduler、network / compute worker、local / remote-rclone / opt-in mount Storageを持つ。
要件は`docs/要件定義.md`、設計判断は`docs/基本設計.md`、変更一覧は`docs/変更履歴.md`を正とする。

## 実装状態

- [x] Phase 1–8: 基盤、暗号化設定、DB、yt-dlp、Storage、Task queue、5段pipeline、二相同期
- [x] Phase 9–12: 単一管理者認証、CSRF、Web CRUD、Run/Task運用、app専用scheduler
- [x] Phase 13: 読み取り専用integrity、relink、missing方針、手動link、差分report
- [x] Phase 14–15: format検査、yt-dlp自動更新、実download smoke、rollback
- [x] Phase 16–18: retention、非秘密config transfer、非同期Hook
- [x] Phase 19: 既定無効`compose.privileged.yaml`とCIFS / NFS mount adapter
- [x] Phase 20: backup / restore、隔離purge・clean rebuild、Playlist folder明示移動
- [ ] 要件定義§19の26項目を`docs/受け入れ条件確認.md`へ確定
- [ ] プロジェクト全体のレビュー④と公開前監査を記録

## 直近の確定事項

- `make backup` / `make restore`はSQLite backup API、config、任意logを認証済みarchiveで扱う。
  `SECRET_KEY`、`.env`、メディア、Staging、yt-dlp venvは含めない。restoreは現状態の自動backup、
  全service停止、member/hash/HMAC/鍵指紋/SQLite検証、migration head確認後の再起動を行う（D-061）。
- 実DBの読み取りbackupと隔離restore、別Compose projectでのend-to-end restoreを完了した。
  DB/config復元、自動pre-restore backup、3service healthy、SQLite整合性を確認し、実メディアは変更していない。
- コミット済み状態だけの隔離cloneで`make purge`→build→初回起動→再purgeを完了した。
  隔離コンテナ・volume・networkは残らず、bind先メディアdirectoryは保持された。
- `mount`は固定sentinelと実効capabilityが揃う明示overlayでだけ有効。通常Composeとcompute workerは
  非特権のまま。WSL2のbind元がprivate mountで、外部VMへ接続できなかったため、実CIFS / NFS mountは
  未検証と明記する（D-060）。既定・推奨はrclone remote。
- Playlist通常編集では既存`folder_name`を変えない。専用画面で件数・remote警告をpreviewし、
  期限付き署名確認後だけ実体を移動する。local / remoteとも移動先を上書きせず、成功ごとにArtifact pathと
  Run進捗をcommitして残件を再実行できる（D-062）。実メディアではなく合成local fileだけで検証した。
- retentionは既定無効。有効化・削除実行ともdry-run必須で、件数/割合guard、DB/Storage/実体CAS、
  no-replace quarantine、削除意図と結果の二相fsync auditを持つ。実環境では候補確認だけで削除していない。
- config exportはpositive schemaで秘密と自由入力を省略する。importは資格情報/Cookieを再利用せず、
  remote Storageとretentionを無効化する。実環境exportで秘密値一致0、DB/file変更0を確認した。
- Hookは12eventのpayload allowlistとbounded単一worker queueを使い、設定・DB・queue障害を本体から隔離する。
- Phase 8再検証はCookieを必要とする取得対象のHTTP 403制限が残った。実DBは直近記録でTarget 659件中
  downloaded 599、blocked 1、failed 1、unavailable 58、Artifact 599件・約2.54GiB。
  「downloadedが大半」は満たすが、全件完走は検証制限として正直に記録する。

## 最新検証

- 最新test image: 全497 tests PASS（既知のStarlette TestClient deprecation warning 1件）
- Ruff: PASS
- mypy: 88 source files PASS
- folder move重点試験: preview無副作用、2件完了、途中1件成功後のDB反映、残り1件再実行 PASS
- backup archive: `SECRET_KEY`実値一致0、SQLite `quick_check=ok`、migration head確認 PASS
- clean clone: 初回起動、health、初期管理者作成、purge残骸0、メディアdirectory保持 PASS
- 現行project: app healthy、両worker稼働をPhase 20隔離検証後に確認済み

## Git状態と禁止事項

- 最新実装commit: `ebaf5f8 feat: Playlist フォルダの明示移動`
- checkpoint: `checkpoint/step-18`まで作成済み。最終は`checkpoint/step-20`予定
- `docs/phase13-20_指示書.md`は利用者指定により未追跡のまま維持し、commitへ含めない
- `git push`、repository公開、GitHub設定変更は、公開前監査と利用者の明示承認まで行わない
- `.env`、`.local/`、実URL、Cookie、資格情報、ホスト識別情報を文書・commit・出力へ含めない
- 実メディアの削除・移動は利用者確認なしに行わない。テストは合成fileか読み取り専用で行う

## 未解決・保留

| # | 内容 | 状態 |
|---|---|---|
| 1 | 実CIFS / NFSでの`mount` adapter検証 | 外部VM接続不可・WSL2 shared mount条件不足。未検証と明記 |
| 2 | Phase 8の全Target完走 | HTTP 403 / 形式非互換による検証制限。追加負荷を避け停止 |
| 3 | Alembic autogenerateのSQLite CHECK偽陽性diff | migration追加時に手で除去（D-008） |
| 4 | ffmpeg `--download-sections` 1秒切出しの`-11` | 通常ffprobe/verifyは健全（D-036） |
| 5 | clone URLの`<repo>`置換、GitHub public化、Issues/Wiki/Dependabot | 利用者判断とpush承認待ち |

## 次に行うこと

1. 文書最終整備commitを確定する
2. 要件定義§19の26項目をPASS / FAIL / NOT RUNで根拠付き記録する
3. 全体レビュー④、秘密・環境情報・非目標・過去review残件を独立点検する
4. 指摘修正後に全test/lint、migration往復、Git履歴・未追跡状態を再確認する
5. 最終記録commitと`checkpoint/step-20`を作成し、pushせず利用者へ報告する

## 運用メモ

- 起動: `make up` / 停止: `make down`
- test: `make test` / lint: `make lint`
- backup: `make backup` / restore: `make restore FILE=...`
- schedulerはappだけ。workerやホストcron/systemdへ置かない
- worker設定変更はworker再起動、scheduler設定は60秒以内に整合
- `docker compose down -v`と`make purge`はnamed volumeのDB/Stagingを消す。通常運用では使わない
- HTTP 403多発時は並列度を上げず停止する
