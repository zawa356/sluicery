# AISTATE

> このファイルはセッション間の引き継ぎ用です。
> セッション開始時に最初に読み、セッション終了時に必ず更新してください。

最終更新: 2026-08-08 19:30
対応コミット: (未コミット) 初回セットアップ

## プロジェクト概要

sluicery は yt-dlp を用いた自己ホスト向けのプレイリスト同期サーバー。
詳細は `docs/要件定義.md`。

## 現在の進捗

要件定義 §20 の実装順序に対する現在地。

- [x] 1. リポジトリ骨格・Docker 環境（requirements.lock 未生成、下記参照）
- [ ] 2. 設定読み込み・DB スキーマ
- [ ] 3. yt-dlp venv 管理と CLI ラッパ
- [ ] 4. オプション合成モデル
- [ ] 5. Storage アダプタ（local / remote-rclone）
- [ ] 6. Task キューとワーカー
- [ ] 7. パイプライン（download → verify → postprocess(空) → publish → index）
- [ ] 8. 双方向同期（discover / download）、状態遷移
- [ ] 9. 認証、Web UI 骨格
- [ ] 10. Playlist / Profile / Storage の CRUD 画面
- [ ] 11. Run 履歴、進捗表示、ログ閲覧、キャンセル
- [ ] 12. スケジューラ
- [ ] 13. 整合性チェック、relink、孤立リンク画面、差分レポート
- [ ] 14. フォーマット探査機能
- [ ] 15. yt-dlp 自動更新 + スモークテスト + ロールバック
- [ ] 16. retention（ドライラン必須）
- [ ] 17. 設定エクスポート / インポート
- [ ] 18. フック機構
- [ ] 19. compose.privileged.yaml、GPU 設定のコメントアウト配置
- [ ] 20. ランブック / リストアドキュメント一式

## 直近の作業

- git リポジトリを初期化（main ブランチ、リモートなし）
- `docs/requirements.md` を `docs/要件定義.md` にリネーム
- `.gitignore` を CLAUDE.md §5.2 のテンプレートで上書き（元は `/docs` のみで誤って docs 全体を除外していた）
- ドキュメント一式を新規作成: AISTATE.md / docs/基本設計.md / docs/変更履歴.md / README.md / docs/footprint.md / docs/storage.md / docs/legal.md
- 実装順序 #1（リポジトリ骨格）を実装: Dockerfile（python:3.12-slim を digest 固定、rclone v1.75.0 と ffmpeg 静的ビルドを checksum 検証付きで導入）、compose.yaml（app / worker-network / worker-compute の3サービス）、Makefile、`.env.example`、`scripts/entrypoint.sh`（PUID/PGID/UMASK 制御、SECRET_KEY 未設定時に起動拒否）
- `src/sluicery/` を要件定義 §18 のツリーに合わせてパッケージ化（多くのモジュールは中身が空のスタブ。各ファイル冒頭に、どの実装順序ステップで実装するかをコメントで明記済み）
- `src/sluicery/cli.py` / `web/app.py` のみ最小限動作する状態（`web` コマンドで `/healthz` を返す FastAPI アプリが起動する想定。`worker` コマンドは未実装メッセージを出すのみ）

## 次にすること

1. `requirements.lock` の生成（下記の未解決 #1 を参照。ユーザー側で Docker が使える環境で `make lock` を実行してもらう必要がある）
2. 生成後、実際に `docker compose up -d --build` が通ることを確認（本セッションでは Docker Desktop の WSL 連携が無効で検証できていない）
3. 実装順序 #2（設定読み込み・`SECRET_KEY` 検証・DB スキーマ + Alembic マイグレーション）に着手

## 未解決・保留

| # | 内容 | 状態 |
|---|---|---|
| 1 | `requirements.lock` が未生成。このセッションの実行環境に pip も動く Docker デーモンもなく、`pip-compile --generate-hashes` を実行できなかった。ハッシュを手で捏造すると `--require-hashes` インストールが確実に壊れるため、ダミーは作成していない。`Makefile` に `make lock`（Docker + ネットワークが使える環境で pip-compile を実行）を用意済み。**docker compose up の前に必ず `make lock` を実行すること** | 未着手・要ユーザー環境での実行 |
| 2 | compose.yaml / Dockerfile / entrypoint.sh は実際にビルド・起動して動作検証できていない（本セッションでは docker デーモンに接続できなかった）。YAML 構文・シェル構文は目視と `bash -n` のみ確認済み | 未検証 |

## 重要な合意（忘れてはいけないもの）

- 通信中での削除は絶対にローカルファイルの削除に伝搬させない
- `blocked` はリトライ回数を消費しない
- ファイル名の `[<source_id>]` は末尾（拡張子直前）に固定、relink がこれに依存している
- `SECRET_KEY` 未設定時は起動を拒否する
- push などのリモート git 操作は禁止

## 環境メモ

- 起動: `make up`（実装後）
- テスト: `make test` または `pytest tests/`（実装後）
- DB: `data/sluicery.db`（volume内）
- マイグレーション: `alembic upgrade head`
- ログ: `data/logs/`

## 既知の落とし穴

- SQLite の WAL モードでもワーカーとの同時書き込みが競合しないよう、書き込みトランザクションは短く保つ
- `docker compose down -v` は volume を消す。開発中は使わない
