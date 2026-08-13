# AISTATE

> このファイルはセッション間の引き継ぎ用です。
> セッション開始時に最初に読み、セッション終了時に必ず更新してください。

最終更新: 2026-08-13
対応コミット: Phase 8レビュー対応完了（`checkpoint/step-08`）

## プロジェクト概要

sluiceryはyt-dlpを用いた自己ホスト型のプレイリスト同期サーバー。詳細は
`docs/要件定義.md`。実装判断と実機結果は`docs/基本設計.md`、変更一覧は
`docs/変更履歴.md`を正とする。

## 現在の進捗

- [x] 1. リポジトリ骨格、Dockerfile、compose.yaml、Makefile、`.env.example`、entrypoint
- [x] 2. 設定読み込み、`SECRET_KEY` 検証、DB スキーマ + Alembic マイグレーション
- [x] 3. yt-dlp venv 管理（インストール、バージョン取得）と CLI ラッパ
- [x] 4. オプション合成モデル、ガード、コマンドラインプレビュー
- [x] 5. Storage アダプタ（local / remote-rclone）、接続テスト、クレデンシャル暗号化
- [x] 6. Task キューとワーカー（network / compute の2クラス）
- [x] 7. パイプライン（download → verify → postprocess(空) → publish → index）
- [x] 8. 二相同期（discover / download）、状態遷移
- [ ] 9. 認証、Web UI 骨格（レイアウト、ログイン）
- [ ] 10–20. CRUD画面以降

Phase 7 / 8は実装、実機検証、レビュー、全指摘対応まで完了。Part Bの詳細は
`docs/基本設計.md`の「Phase 8 開発機・SMB実機検証」と`docs/reviews/phase8.md`を参照。

## Phase 8で実装したもの

- `core/target_state.py`のItem / Target遷移表、不正遷移拒否、現在statusを所有権とするCAS
- network workerのdiscover Task、Item upsert、delisted / 再登場、新規ItemのTarget作成
- 空・エラーdiscoverでドメインDBを変更しない`empty_result`保護
- active Itemのpending / 再試行可能failed / blockedをplaylist順に選ぶdownloadフェーズ
- Storageごとの到達性・書込み・容量事前確認、使用不能時のチェーン非作成
- `sync.max_targets_per_run`（既定50）までの5段チェーン投入と残数記録
- discover / download Runの生成と8統計、全件blocked時のRun failed
- `sluicery sync discover/download/run --playlist/--all`、discover dry-run、`make sync`
- `sync run --all`で全discover完了後に全downloadを投入する二相順序
- 再試行でもcore遷移表を1段ずつ通るPhase 7ハンドラ

## Phase 8検証結果

- 5プレイリストを3.38〜7.52秒でdiscoverし、合計309 Item / 618 Targetを生成
- dry-runは新規0 / delisted 0を表示し、Item / Target / Playlist同期時刻を変更しない
- 投入上限2の反復、ワーカー再起動復旧、完了Playlistの新規0 / 重複0を確認
- delistedと再登場、空振りは実DBの制御差分で検証し、Artifact / Target / ファイル非変更
- local / SMB合計224 Artifact、863,302,990 bytes。Opusタグ・埋め込み画像も確認
- 読取専用SMBはTaskを作らずTarget blocked、書込可能SMBはpublish / index完了
- 連続downloadはHTTP 403多発時に指示書§16.3の停止条件を適用。停止時はdownloaded 224 / failed 68 / pending 320 / unavailable 7
- Stagingは53ファイル・89,834,689 bytes。孤立4件とTask追跡中の中間ファイルで、自動削除していない
- runtime全サービスを最新コードで再ビルド済み。app healthy、network / compute worker稼働
- `make test` 306件成功、Ruff成功、mypy 80 source files成功
- 実URL 5件と認証・接続値13件の追跡ファイル・全履歴・compose log・`/data/logs`一致は0件。gitleaksもleak 0件

## 次にやること

1. 要件定義§12.1、§16、§18とPhase 9の指示書を読み、「認証、Web UI 骨格（レイアウト、ログイン）」の設計点検から始める
2. Phase 9着手前に`checkpoint/step-08`がHEADを指すこと、worktreeがcleanであることを確認する
3. Phase 8の全download完走を将来再検証する場合は、HTTP 403が解消したことを少数Taskで確認し、並列度を上げずに再開する

## 未解決・保留

| # | 内容 | 状態 |
|---|---|---|
| 1 | Alembic autogenerateがSQLite CHECK制約で偽陽性diffを出す（D-008） | マイグレーション追加時に手で除去 |
| 4 | GitHubリポジトリのpublic化 | 見送り中・判断待ち |
| 5 | Issues / Wiki / Projectsの要否 | 未確認 |
| 6 | Dependabot alertsの要否 | 未確認 |
| 7 | README / deploymentのclone URLが`<repo>`のまま | public化時に差し替え |
| 8 | ffmpegの`--download-sections` 1秒区間切り出しは`-11` | ffprobe通常検証は健全。D-036 |
| 9 | ローカルコミットとタグのGitHub push | 公開前監査とユーザー承認前は禁止 |
| 10 | D-022 YouTube素材が検証時にHTTP 403 | Blender公式PeerTube素材で代替 |
| 11 | Phase 8実運用規模downloadのHTTP 403多発 | 指示書に従い安全停止。未取得分はDBで追跡可能 |

## 重要な合意

- 「ローカルのデータを失わない」
- 「不完全なファイルを最終保存先に残さない」
- 「システムに残骸を残さない」
- 「不確実な状態で削除しない」
- 「すべての操作を追跡可能にする」
- discoverの取得結果が空またはエラーの場合、以降の処理を中止する
- delistedへの遷移は Artifact に一切影響しない。ファイルを削除しない
- `blocked` は原因解消後に自動で `pending` に戻す
- `missing` からの自動再取得は既定で無効
- `ignored` はユーザー操作でのみ設定・解除される
- `artifact` は index タスクで作成する
- Stagingはindex後だけ削除し、失敗・中断時は削除しない
- final既存時は期待サイズ一致だけをpublish済み復旧とし、不一致は上書きしない
- payloadへ資格情報・取得元URLを保存しない
- `git push` / `gh repo create` / `gh repo edit`は履歴監査とユーザー承認後だけ許可

## 環境メモ

- 起動: `make up`または`docker compose up -d --build`
- テスト: `make test`、lint: `make lint`
- 同期: `make sync`または`docker compose exec --user "$(id -u):$(id -g)" app python3 -m sluicery.cli sync ...`
- 実機用のSMB / Docker SSH / Playlist URLは、ignoredかつmode 600の`.local/*.env`と`.local/test_playlists.txt`だけにある
- `/data/staging/trailer_1080p.mov`は削除禁止。現在の孤立検出は同ファイルを含む4件
- 検証用Playlist ID 4〜8、Task、Artifactは開発DBに残っている。一時的な読取専用Storageと割当ては削除済み
- `sync.max_targets_per_run`は既定50へ戻し、Playlist ID 1〜8のpausedはfalse
- DBマイグレーションheadは`e4a1f7b9c203`

## 既知の落とし穴

- SQLite WALでも書込み競合は起こる。claim、heartbeat、進捗、状態更新のtransactionを短くする
- workerは運用設定を起動時に読むため、設定変更後は該当workerの再起動が必要
- `sync run --all`のdownload Runは投入完了時点で成功になる。メディア取得の成否はTarget / Taskを確認する
- HTTP 403多発時は並列度を上げず停止する。中間ファイルは調査・再開まで削除しない
- Profile自由引数よりPlaylist自由引数が後勝ちになる。検証用`--format`が残るPlaylistを別Profileへ流用しない
- `docker compose down -v`はDBとStagingを消す。通常開発では使わない
