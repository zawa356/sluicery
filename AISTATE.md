# AISTATE

> このファイルはセッション間の引き継ぎ用です。
> セッション開始時に最初に読み、セッション終了時に必ず更新してください。

最終更新: 2026-08-09 23:52
対応コミット: 679f341 fix(storage): handle cross-mount local publish

## プロジェクト概要

sluicery は yt-dlp を用いた自己ホスト型のプレイリスト同期サーバー。
詳細は `docs/要件定義.md`。

## 現在の進捗

要件定義 §20 の実装順序に対する現在地。

- [x] 1. リポジトリ骨格、Dockerfile、compose.yaml、Makefile、`.env.example`、entrypoint
- [x] 2. 設定読み込み、`SECRET_KEY` 検証、DB スキーマ + Alembic マイグレーション
- [x] 3. yt-dlp venv 管理（インストール、バージョン取得）と CLI ラッパ
- [x] 4. オプション合成モデル、ガード、コマンドラインプレビュー
- [x] 5. Storage アダプタ（local / remote-rclone）、接続テスト、クレデンシャル暗号化
- [ ] 6. Task キューとワーカー（network / compute の2クラス） ← 次の着手点
- [ ] 7. パイプライン（download → verify → postprocess(空) → publish → index）
- [ ] 8. 二相同期（discover / download）、状態遷移
- [ ] 9. 認証、Web UI 骨格（レイアウト、ログイン）
- [ ] 10. Playlist / Profile / Storage の CRUD 画面
- [ ] 11. Run 履歴、進捗表示、ログ閲覧、キャンセル
- [ ] 12. スケジューラ（分離スケジュール、時間帯制限、ジッター）
- [ ] 13. 整合性チェック、relink、手動リンク画面、差分レポート
- [ ] 14. フォーマット検査機能
- [ ] 15. yt-dlp 自動更新 + スモークテスト + ロールバック
- [ ] 16. retention（ドライラン必須）
- [ ] 17. 設定エクスポート / インポート
- [ ] 18. フック機構（インターフェース + eventlog 実装）
- [ ] 19. `compose.privileged.yaml`（mount アダプタ）、GPU 設定のコメントアウト配置
- [ ] 20. バックアップ / リストア、ドキュメント一式

## 直近の作業

- Phase 5 の P0 を是正。公開前監査のホームパス・IPv4/IPv6・ローカルホスト名パターンを
  修正し、ffmpeg SIGSEGV の未解決事項を Phase 7 の健全性再評価へ更新した
- 外部 CLI 実行を `runner/base.py` へ切り出し、`YtdlpRunner` / `RcloneRunner` が継承する構成にした
- local / remote-rclone の `StorageAdapter` 6メソッド、factory、容量判定、4段階接続、
  一時名→検証→rename の publish、既定の上書き拒否を実装した（D-024〜D-028）
- rclone password は stdin で obscure し、対象子プロセスだけの環境変数へ注入する。
  資格情報と `RCLONE_CONFIG_*` 名は保持・ログ出力前にマスクする
- 暫定 Storage CLI に remote SMB の登録・認証情報更新と `test` / `space` / `ls` / `push` を追加した
- 専用 SMB 環境で Phase 5 実機検証20項目を完了。サーバー停止は閉鎖ポートの接続拒否で同等検証し、
  正常/異常4段階、容量、転送、中断、孤児0、マスク、local、所有者を確認した
- 実機で timeout / SMB logon 文言の分類、非秘密設定値の誤検知、Docker/WSL cross-mount の
  local publish を修正した。試験ファイルと資格情報入り Storage レコードは削除済み
- 最新の `make test` は204件、Ruff、mypy は全件成功

## 次にやること

1. Phase 5 の独立レビュー指摘を `docs/reviews/phase5.md` に記録し、必要な修正を行う
2. 修正後に全件テスト、履歴監査、gitleaks を再実行し `checkpoint/step-05` を付与する
3. push は監査結果を報告し、ユーザーの明示承認を得るまで行わない
4. Phase 6 で Task claim、network / compute ワーカー、blocked 相当の表現を設計・実装する

## 未解決・保留

| # | 内容 | 状態 |
|---|---|---|
| 1 | Alembic の autogenerate は SQLite の CHECK 制約比較で偽陽性 diff を出す（D-008） | マイグレーション追加時に手で除去 |
| 2 | `target.status=blocked` に対応する `TaskStatus` がない | Phase 6/7 着手時に判断（D-028） |
| 3 | compose に init がなく、yt-dlp の孫プロセスが zombie として残る可能性がある | Phase 6 着手時に評価 |
| 4 | GitHub リポジトリの public 化 | 見送り中・判断待ち |
| 5 | Issues / Wiki / Projects の要否 | 未確認 |
| 6 | Dependabot alerts の要否 | 未確認 |
| 7 | README・`docs/deployment.md` の clone URL が `<repo>` のまま | public 化時に差し替え |
| 8 | 1秒区間を `--download-sections` で切り出す追加試験は ffmpeg `-11`（SIGSEGV）で失敗した。通常 fetch は成功 | ffmpeg 静的ビルドの健全性に関わる可能性がある。Phase 7（verify で ffprobe / ffmpeg を使用）着手時に再評価する |
| 9 | Phase 4/5 のローカルコミットとタグの GitHub push | 最終監査後もユーザー承認待ち |

generic extractor で `uploader` / `duration` / `upload_date` が欠損する件は Phase 4 で再確認済み。
命名は空文字または `0` へ fallback し、`NA` を混入させない（D-019）。

## 重要な前提（忘れやすいもの）

- 配信元での削除は絶対にローカルファイルの削除に伝播させない
- `blocked` はリトライ回数を消費しない
- ファイル名の `[<source_id>]` は末尾（拡張子直前）に固定。relink がこれに依存している
- `SECRET_KEY` 未設定時は起動を拒否する
- `SECRET_KEY` はローテーション非対応（鍵紛失・変更時はクレデンシャル再入力。D-004）
- 運用パラメータは `.env` ではなく `setting` テーブル、既定値は `core/settings.py` の `CODE_DEFAULTS`
- `MEDIA_ROOT` はホスト側パスであり、コンテナ内では常に `/mnt/media` を使う（D-010）
- yt-dlp / rclone とその子はプロセスグループ単位で終了させる
- Storage の publish は一時名で検証後に最終化し、既定で上書きしない
- rclone 資格情報は引数・設定ファイルへ載せず、子プロセス限定の環境変数で渡す
- Phase 5 で実装・実機検証済みの remote protocol は SMB だけ
- 未知の yt-dlp / Storage エラーは `failed` に分類する（D-014, D-028）
- `git push` / `gh repo create` / `gh repo edit` は履歴監査とユーザー承認後のみ許可

## 環境メモ

- 起動: `make up` または `docker compose up -d --build`
- テスト: `make test`。lint: `make lint`
- DB: `data/sluicery.db`（named volume `data` 内）
- yt-dlp venv: `data/ytdlp/`（`versions/<version>/`、`current` symlink、`.lock`）
- マイグレーション: `sluicery db upgrade`（`AUTO_MIGRATE=true` なら起動時に自動実行）
- ログ: `data/logs/`
- CLI: `docker compose exec app python3 -m sluicery.cli ...`。ファイル生成時は
  `docker compose exec --user "$(id -u):$(id -g)" app ...` を使う
- 開発機の compose 3サービスは稼働済み。DB current=head、yt-dlp 2026.07.04 は `ready`
- Phase 5 SMB 試験の生成ファイル・local 試験ディレクトリ・資格情報入り Storage レコードは削除済み
- P0 是正後の監査は75コミット時点で gitleaks 漏えい0件。Phase 5 完了後の最終再監査は未実施

## 既知の落とし穴

- SQLite の WAL モードでワーカーとの同時書き込みが競合しやすい。書き込みトランザクションは短く保つ
- `docker compose down -v` は volume を消す。開発中は使わない
- Docker/WSL では異なる mount が同じ `st_dev` を返し得る。local publish は `EXDEV` で copy へ切り替える
- `docker compose exec -T` を外側から中断すると exec クライアントだけが終了し得る。中断試験では
  コンテナ内の CLI へ SIGINT を送り、BaseRunner のプロセスグループ終了まで確認する
