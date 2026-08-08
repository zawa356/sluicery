# AISTATE

> このファイルはセッション間の引き継ぎ用です。
> セッション開始時に最初に読み、セッション終了時に必ず更新してください。

最終更新: 2026-08-08 23:40
対応コミット: e5f8349 docs: 基本設計・変更履歴を Phase 2 の実装内容に合わせて更新

## プロジェクト概要

sluicery は yt-dlp を用いた自己ホスト型のプレイリスト同期サーバー。
詳細は `docs/要件定義.md`。Phase 2 は `docs/phase2_指示書.md` の指示に基づき実施した。

## 現在の進捗

要件定義 §20 の実装順序に対する現在地。

- [x] 1. リポジトリ骨格・Docker 環境（requirements.lock 未生成、下記参照）
- [x] 2. 設定読み込み・DB スキーマ
- [ ] 3. yt-dlp venv 管理と CLI ラッパ  ← 次の着手点
- [ ] 4. オプション合成モデル
- [ ] 5. Storage アダプタ（local / remote-rclone）
- [ ] 6. Task キューとワーカー
- [ ] 7. パイプライン（download → verify → postprocess(空) → publish → index）
- [ ] 8. 二相同期（discover / download）、状態遷移
- [ ] 9. 認証、Web UI 骨格
- [ ] 10. Playlist / Profile / Storage の CRUD 画面
- [ ] 11. Run 履歴、進捗表示、ログ閲覧、キャンセル
- [ ] 12. スケジューラ
- [ ] 13. 整合性チェック、relink、手動リンク画面、差分レポート
- [ ] 14. フォーマット検査機能
- [ ] 15. yt-dlp 自動更新 + スモークテスト + ロールバック
- [ ] 16. retention（ドライラン必須）
- [ ] 17. 設定エクスポート / インポート
- [ ] 18. フック機構
- [ ] 19. compose.privileged.yaml、GPU 設定のコメントアウト配置
- [ ] 20. バックアップ / リストア、ドキュメント一式

## 直近の作業

- Phase 2（設定層・DB スキーマ・リポジトリ層）を完了。実装順序 #2 に対応
- P0 是正タスク（`.gitignore` アンカー、用語是正、entrypoint.sh の権限強化、
  D-002 の FFMPEG_URL build-arg 化）を実施
- `config.py`（Pydantic Settings）、`db/models.py`（要件定義 §7.1 の全12テーブル）、
  `db/session.py`（PRAGMA）、`db/crypto.py`（EncryptedJSON・鍵指紋）、
  `core/settings.py`（運用パラメータの二層アクセサ）、`db/repositories/*`
  （基本 CRUD + `Task.claim_next()` のアトミック実装）、Alembic 初期マイグレーション、
  CLI（`config check` / `db upgrade|current|revision` / `settings list|get|set|unset`）
  を実装し、テスト一式（`tests/`、28件）を追加。詳細は `docs/変更履歴.md` を参照
- テスト作成中に SQLite + SQLAlchemy で `DateTime(timezone=True)` が tzinfo を
  保持しないバグを発見し、`UTCDateTime` 型で修正（`docs/基本設計.md` D-007）
- 設計判断は `docs/基本設計.md` §7 に D-004〜D-009 として記録済み

## 次にすること

1. **`make lock` と `docker compose up -d --build` の実行**（下記未解決 #1。
   これが通るまで実装順序 #3 には着手しない方針だったが、ユーザーへの確認と
   並行して Phase 2 のコード実装は完了させた。次セッション開始時、まずこれの
   完了確認を最優先で行うこと）
2. 完了していれば `docs/phase2_指示書.md` §11 の完了条件（1〜16）を一通り再確認
3. 実装順序 #3（yt-dlp venv 管理と CLI ラッパ）に着手

## 未解決・保留

| # | 内容 | 状態 |
|---|---|---|
| 1 | `requirements.lock` が未生成。本セッションの実行環境では docker daemon への権限がなく（socket が `root:docker` 所有でユーザーが未所属）、`make lock`（`docker run` 経由の pip-compile）も `docker compose up` も実行できなかった。ユーザーに `sudo usermod -aG docker $USER` を依頼済みだが、本セッション終了時点で未確認 | 未着手・要ユーザー環境での実行 |
| 2 | 上記が未解決のため、`docker compose up -d --build` によるコンテナ起動・entrypoint.sh の実地動作（setpriv、tmpfs 所有権、MEDIA_ROOT/STAGING_DIR 事前チェック）は未検証。ローカルの venv（`/tmp` 配下、Git 管理外）で config/DB/CLI 層の動作とテストは確認済み | 未検証 |
| 3 | Alembic の `revision --autogenerate` は、SQLite の CHECK 制約比較の既知の制限により、実際の変更がなくても「削除→再作成」の偽陽性 diff を出す（`docs/基本設計.md` D-008）。Phase 3 以降でマイグレーションを追加する際、生成物からこの偽陽性を手で取り除く必要がある | 恒常的な既知の制限（対応不要、注意事項） |

## 重要な前提（忘れやすいもの）

- 配信元での削除は絶対にローカルファイルの削除に伝播させない
- `blocked` はリトライ回数を消費しない
- ファイル名の `[<source_id>]` は末尾（拡張子直前）に固定。relink がこれに依存している
- `SECRET_KEY` 未設定時は起動を拒否する
- `SECRET_KEY` はローテーション非対応（鍵紛失・変更時はクレデンシャル再入力。D-004）
- 運用パラメータ（Staging しきい値、cron 式、download.* 等）は `.env` ではなく
  `setting` テーブル側。既定値はコード側（`core/settings.py` の `CODE_DEFAULTS`）
- DB のタイムスタンプは独自の `UTCDateTime` 型を使う。生の `DateTime(timezone=True)`
  を新しいカラムに使わないこと（SQLite で tzinfo が保持されないバグを踏む）
- リポジトリ層に状態遷移ロジックを書かない（Phase 7〜8 の `core/` に置く）
- push などのリモート git 操作は禁止

## 環境メモ

- 起動: `make up`（`make lock` を先に実行する必要あり。上記未解決 #1）
- テスト: `make test` または `pytest`（ローカル venv で 28件パス確認済み。
  Docker 経由では未確認）
- DB: `data/sluicery.db`（volume 内）
- マイグレーション: `alembic upgrade head` または `sluicery db upgrade`
  （`AUTO_MIGRATE=true` なら `app` 起動時に自動実行）
- ログ: `data/logs/`
- CLI: `python -m sluicery.cli {config check | db ... | settings ...}`

## 既知の落とし穴

- SQLite の WAL モードでもワーカーとの同時書き込みが競合しないよう、書き込みトランザクションは短く保つ
- `docker compose down -v` は volume を消す。開発中は使わない
- `make purge` は Staging（`data` volume 内）も消す。進行中ダウンロードの中間ファイルを失う
- Alembic の autogenerate は CHECK 制約の偽陽性 diff を出す（上記未解決 #3）
