# AISTATE

> セッション間の引き継ぎ用。開始時に読み、作業終了時に更新する。

最終更新: 2026-08-24
対応状態: Phase 1–20完了、本体public化、Wiki初版公開、維持ルール整備完了

## プロジェクト概要

sluiceryはyt-dlpを使う自己ホスト型Playlist同期サーバー。単一管理者向けWeb UI、CLI、
app専用scheduler、network / compute worker、local / remote-rclone / opt-in mount Storageを持つ。
仕様は`docs/要件定義.md`、設計判断は`docs/基本設計.md`、変更一覧は`docs/変更履歴.md`を正とする。

## 実装状態

- [x] Phase 1–8: 基盤、暗号化設定、DB、yt-dlp、Storage、Task queue、5段pipeline、二相同期
- [x] Phase 9–12: 単一管理者認証、CSRF、Web CRUD、Run/Task運用、app専用scheduler
- [x] Phase 13: 読み取り専用integrity、relink、missing方針、手動link、差分report
- [x] Phase 14–15: format検査、yt-dlp自動更新、実download smoke、rollback
- [x] Phase 16–18: retention、非秘密config transfer、非同期Hook
- [x] Phase 19–20: 既定無効`mount`、backup / restore、隔離purge、Playlist folder明示移動
- [x] 要件定義§19の26項目を`docs/受け入れ条件確認.md`へ確定
- [x] 履歴186コミットとWikiを監査し、GitHub本体をpublic化
- [x] Wikiに利用者向け13ページ、サイドバー、フッターを公開

## 公開状態

- 本体: `https://github.com/zawa356/sluicery`、既定ブランチ`main`
- Wiki: `https://github.com/zawa356/sluicery/wiki`、別リポジトリの`master`
- GitHub Actionsは無効、Issuesは有効、Projectsは無効、Dependabot alertsは有効
- 本体の既存17タグをpush済み。統合Phaseの個別タグ不足は利用者合意により許容
- `docs/phase13-20_指示書.md`と`docs/wiki構築_指示書.md`も監査後に追跡・公開済み

## 最新検証

- 最新test image: 全511 tests PASS（既知のStarlette TestClient deprecation warning 1件）
- Ruff: PASS
- mypy: 88 source files PASS
- 本体公開前監査: gitleaksで履歴186コミットを走査し、漏えい0件
- Wiki監査: 実パス、IPv4、秘密パターン、ローカルhost名、内部リンク欠落が全0件
- Wiki公開確認: 13ページがHTTP 200、サイドバー・フッターと主要`docs/`リンクを確認

## 未解決・保留

| # | 内容 | 状態 |
|---|---|---|
| 1 | 実CIFS / NFSでの`mount` adapter検証 | 外部VM接続不可・WSL2 shared mount条件不足。未検証と明記 |
| 2 | Phase 8の全Target完走 | HTTP 403 / 形式非互換による検証制限。追加負荷を避け停止 |
| 3 | Alembic autogenerateのSQLite CHECK偽陽性diff | migration追加時に手で除去（D-008） |
| 4 | ffmpeg `--download-sections` 1秒切出しの`-11` | 通常ffprobe/verifyは健全（D-036） |

## 重要な合意

- 配信元での削除は絶対にローカルファイルの削除に伝播させない
- `blocked`はリトライ回数を消費しない
- ファイル名の`[<source_id>]`は末尾（拡張子直前）に固定し、relinkが依存する
- `SECRET_KEY`はbackupに含めず、archiveと別の安全な場所へ保管する
- Wikiは利用者向けの読み物とし、仕様は`docs/`を正とする。利用者から見える変更ではWiki更新の要否を確認する
- 本体とWikiの`git push`は、それぞれの履歴監査と利用者の明示承認後にだけ実行する

## 次に行うこと

1. 利用者から見える挙動を変える場合、本体の`docs/`とWikiの両方を点検する
2. 実CIFS / NFS検証やPhase 8の制限を解消する場合は、別作業として安全に再検証する
3. 次回push前に`docs/公開前チェックリスト.md`を本体またはWikiそれぞれで実行する

## 運用メモ

- 起動: `make up` / 停止: `make down`
- test: `make test` / lint: `make lint`
- backup: `make backup` / restore: `make restore FILE=...`
- schedulerはappだけ。workerやホストcron/systemdへ置かない
- worker設定変更はworker再起動、scheduler設定は60秒以内に整合
- `docker compose down -v`と`make purge`はnamed volumeのDB/Stagingを消す。通常運用では使わない
- HTTP 403多発時は並列度を上げず停止する
