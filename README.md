# sluicery

yt-dlp を用いた自己ホスト向けのプレイリスト同期サーバー。登録したプレイリスト（動画・音楽）の内容を
ローカルまたはネットワークストレージへ取得・保持し、実行のたびに差分だけを追記する。

## Quick Start

`make` を前提にせず、`docker compose` 直で動かせる最短手順です。詳細は後続セクションを参照してください。

```bash
git clone https://github.com/zawa356/sluicery.git sluicery
cd sluicery
cp .env.example .env

# SECRET_KEY を生成（python:3.12-slim には cryptography が入っていないため、その場で入れる）
docker run --rm python:3.12-slim sh -c \
  "pip install -q --root-user-action=ignore cryptography && python3 -c \
  'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"

$EDITOR .env          # SECRET_KEY を貼り付ける

# MEDIA_ROOT を事前作成（Docker に作らせると root 所有になり、PUID/PGID で
# 書き込めず起動時に落ちる。既定値は /mnt/media。.env で変更した場合はそちらに合わせる）
mkdir -p /mnt/media
sudo chown "$(id -u)":"$(id -g)" /mnt/media

docker compose up -d --build

# 起動確認
curl http://localhost:8080/healthz
```

`make` が使える環境では `cp .env.example .env && $EDITOR .env && make up` でも同じです（`MEDIA_ROOT` の
事前作成は別途必要）。

yt-dlp は起動後にバックグラウンドで自動導入されます。音声メタデータやサムネイルの
埋め込みに必要な公式 `default` extras も同じ venv に導入されます。導入完了は以下で確認できます。

```bash
docker compose exec app python3 -m sluicery.cli ytdlp status
# → ready になるまで数秒〜数十秒かかることがあります
```

CLI でファイルを生成する操作（`ytdlp fetch` 等）を手動実行する場合は `--user` でホスト側のユーザーと
揃えること。揃えないと生成物が `root` 所有になります（PUID/PGID の既定値 1000/1000 とホスト側の
`id` が一致している前提）。

```bash
docker compose exec --user "$(id -u):$(id -g)" app python3 -m sluicery.cli ytdlp fetch <URL>
```

## これは何か / 何ではないか

**sluicery が何をするか**：登録したプレイリストを定期的に確認し、新しいアイテムだけをダウンロードして
指定した保存先（ローカルディスクまたは rclone 経由のリモートストレージ）に配置します。

**何ではないか**：

- 外部公開・多人数利用は非対応（単一ユーザー向け）
- DRM 保護コンテンツは対象外
- 配信元での削除に追従してローカルファイルを削除する機能はありません（**仕様であり、意図的な設計**です）
- メディアサーバー（Jellyfin / Navidrome 等）との連携は未実装
- トランスコードは未実装
- Web UI は単一管理者の認証と共通画面を備えています。同期機能の一部は引き続きCLIからも操作できます

## 前提条件

**動作環境**

| 項目 | 内容 |
|---|---|
| OS | Linux x86_64、cgroup v2 |
| Docker | Docker Engine + Compose v2。検証環境では Docker 27.3.1 / Compose v2.29.7 で動作確認 |
| ストレージドライバ | `overlay2` であることを推奨。確認コマンド：`docker info \| grep "Storage Driver"` |
| ポート | 8080（`.env` の `HTTP_PORT` で変更可）が空いていること |

**容量とリソース**（検証環境での実測値。詳細は [docs/deployment.md](docs/deployment.md) §7）

- イメージサイズ：797MB（Deno 2.9.5 同梱後の開発機実測）
- ビルド時間：約3分（初回、ビルドキャッシュ無しの状態から）
- メモリ使用量：起動直後で `app`/`worker-network`/`worker-compute` 合計 約190MiB
- Staging 領域の必要量 = 取得する最大ファイルサイズ × 並列度 + 余裕。ここを見誤ると同期が詰まります

**ネットワーク**

- 外向き HTTPS：PyPI（yt-dlp の導入・更新）、取得対象サイトへの到達性が必要
- YouTube の JavaScript challenge は、イメージに同梱した Deno を yt-dlp が自動検出して処理します
- 時刻同期：cron 式の解釈とタイムゾーンの整合のため

**事前に用意するもの**

- `SECRET_KEY`：上記 Quick Start の方法で生成。Storage の認証情報などを暗号化する鍵で、
  **ローテーションには対応していません。** 紛失・変更すると保存済み認証情報を復号できなくなり、
  各 Storage の認証情報を再入力する必要があります
- `MEDIA_ROOT` ディレクトリの事前作成：Docker に作らせると root 所有になります
- `PUID` / `PGID` の確認：`id` コマンドで取得

**検証状況**

| 環境 | 状況 |
|---|---|
| 一般的な Linux VM（Debian / Ubuntu） | 検証済み（Ubuntu 22.04.5 LTS、2026-08-09。ただし検証環境は専用のクリーンな VM ではなく、他サービスと同居する共用ホストだった。詳細・注意点は [docs/deployment.md](docs/deployment.md) §7） |
| Proxmox LXC | 未検証。`nesting=1,keyctl=1` 等の設定が必要になる見込み（参考情報。[docs/deployment.md](docs/deployment.md) §8 参照） |
| WSL2 | 通常Compose起動と、隔離した一時コンテナでのcapability継承まで検証済み。ホストのbind元がprivate mountのため、CIFS / NFSの実mountは未検証 |
| NAS（Synology / QNAP 等） | 未検証 |

## 初期設定

`requirements.lock` / `requirements-dev.lock` はコミット済みのため、通常のセットアップに
`make lock` は不要です。

`SECRET_KEY` を設定しない場合、明確なエラーメッセージを出して起動を拒否します。意図せず
`SECRET_KEY` が変わった場合、起動時に警告が表示されます。`make backup`は鍵そのものを含めないため、
archiveとは別の安全な場所へ同じ`SECRET_KEY`を保管してください。`make restore FILE=...`は鍵の指紋、
archive全体、SQLite整合性を検査し、既存状態の自動backupと確認後に復元します。

初回起動時に `.env` の `ADMIN_USERNAME` / `ADMIN_PASSWORD` から単一の管理者を作成します。
`ADMIN_PASSWORD` が空ならランダムな初期パスワードを起動ログへ一度だけ表示します。パスワードは
argon2でハッシュ化され、平文はDBへ保存しません。

起動後は `http://localhost:8080/`（ポート変更時はその値）をブラウザで開きます。HTTPSの
リバースプロキシ経由で利用する場合は `AUTH_COOKIE_SECURE=true` にしてください。

カーネルCIFS / NFSを使う`mount` Storageはオプトインの非推奨機能です。通常の
`docker compose up`では選択肢に表示されず利用できません。隔離が大幅に弱くなること、ホスト側の
shared mount設定が必要なこと、実CIFS / NFS環境では未検証であることを理解した場合だけ、
`compose.privileged.yaml`を明示指定してください。詳細は[docs/storage.md](docs/storage.md)を参照してください。

認証が必要な取得元では、Playlist単位でNetscape形式のCookieファイルを暗号化保存できます。
既定は無効で、有効化時にはアカウント停止リスクへの明示確認が必要です。平文は実行中だけ
tmpfsの`/run/sluicery`へ展開され、実行後に削除されます。
検証環境では、Denoが正常に検出される状態でもCookieなしではHTTP 403となる取得対象がありました。
Cookieを常時有効にするのではなく、Cookieなしの少量試験で失敗したPlaylistだけに限定して
有効化してください。

Playlistを有効にすると、`app`サービス内のスケジューラがdiscoverとdownloadを独立したcronで
実行します。cronは`.env`の`TZ`で解釈され、Playlist個別設定が無い場合は設定画面のグローバル
既定（各6時間）を使います。ダウンロード時間帯、±ジッター、一時停止、次回予定もWeb UIから
確認・変更できます。workerやホストのcron / systemd timerを別途設定する必要はありません。

Web UIの整合性レポートでは、missing TargetとStorage内の未追跡ファイルを
確認し、手動で対応付けできます。リンクと取消が変更するのはDBのパスと状態だけで、
実ファイルの削除・移動は行いません。
差分レポートでは配信元から見えなくなったItemと関連Artifactパスを確認できますが、
この画面から削除はできません。

Playlist名と保存フォルダ名は独立しています。通常のPlaylist編集で名前を変えても既存ファイルは
移動しません。実ファイルも変更する場合だけ、詳細画面の「フォルダも移動する」で対象件数と移動先を
previewし、確認後に明示実行します。remote / mountを含む大量移動は途中で失敗しうるため、成功分を
1件ずつDBへ反映します。物理move前の永続intentにより、move後・DB反映前の停止も安全に再実行できます。
Playlistの重複hardlinkは既定無効で、明示有効時だけ同一source ID・Profile・filesystemの既存Artifactを再利用し、
成立しなければ通常copyへ戻ります。

Profile編集画面のフォーマット検査では、任意のURLに対する利用可能format、現在のselectorで
選ばれるformat、推定サイズを確認できます。検査は外部アクセスを伴い、既定では全Profile共通で
10秒の実行間隔があります。ファイルのダウンロードやStorageへの保存は行いません。

yt-dlpは既定で週次に更新確認されます。更新後はDeno検出、メタデータ取得、公開CC素材の実ダウンロード、
固定markerのメタデータとサムネイル埋込み、challenge警告を検査し、専用Stagingを削除します。失敗時は健全性を確認できた直前版だけへ
自動で戻ります。Webの「yt-dlp」画面で現在版、結果、履歴、手動更新・ロールバックを確認できます。

Playlist詳細のretentionで「最新N件」または「M日より古いもの」の削除を明示設定できます。
既定は無効で、有効化と実行の両方に期限付きdry-runと二段確認が必要です。1回の削除は既定20件までで、
PlaylistのArtifactの過半数になる計画は拒否します。削除履歴はRunと監査logに残り、差分レポート自体には
削除機能がありません。実行直前にはDB snapshotだけでなく実ファイルの強い識別情報も再確認し、
未完了の削除意図を検出した場合は自動復旧・自動移動せず、監査logの手動確認を求めます。

設定画面からPlaylist、Profile、割当、Storageの非秘密設定、グローバル設定を単一YAMLへ
エクスポートし、差分preview後にインポートできます。衝突時はスキップ・上書き・新規作成を選べます。
Storage資格情報、Cookie、内部設定、実行状態は含めません。安全性を証明できない自由入力yt-dlp引数、
postprocess設定、URLは省略され、画面での再入力が必要です。インポート時は既存の資格情報とCookieも
再利用せず、remote Storageとretentionを無効化して明示的な再確認を求めます。

`config/hooks.yaml`では12種のイベントを組み込み`event_log`へ記録する購読を設定できます。
記録は単一のbounded非同期queueで順序を保ち、Hook障害は同期・worker・Web処理へ伝播しません。
payloadはイベント別allowlistで絞り、URL・Cookie・秘密値を保存しません。将来のJellyfin / Navidrome
設定例は`config/hooks.example.yaml`にコメントだけを置いており、外部アダプタ自体は未実装です。

## 運用コマンド

| コマンド | 内容 | 実装状況 | `make` 無し環境での等価コマンド |
|---|---|---|---|
| `make up` | ビルドして起動 | 実装済み | `docker compose up -d --build` |
| `make down` | 停止（データは保持） | 実装済み | `docker compose down` |
| `make logs` | ログ追跡 | 実装済み | `docker compose logs -f` |
| `make shell` | `app` コンテナにシェル接続 | 実装済み | `docker compose exec app /bin/bash` |
| `make sync` | 全プレイリストをdiscover後、取得対象を最大50件ずつ投入 | 実装済み | `docker compose exec --user "$(id -u):$(id -g)" app python3 -m sluicery.cli sync run --all` |
| `make test` | dev 依存込みの test ステージをビルドし、コンテナ内で pytest を実行 | 実装済み | `docker build --target test -t sluicery:local-test . && docker run --rm --entrypoint pytest sluicery:local-test` |
| `make lint` | ruff / mypy をコンテナ内で実行 | 実装済み | 上記 test イメージに対し `docker run --rm --entrypoint ruff sluicery:local-test check src tests` 等 |
| `make lock` | `requirements.in` / `requirements-dev.in` から lock ファイルを再生成（依存を更新したときのみ） | 実装済み | — |
| `make migrate` | DB マイグレーションを手動適用（`AUTO_MIGRATE=false` 運用時など） | 実装済み | `docker compose exec app python3 -m sluicery.cli db upgrade` |
| `make revision MSG="..."` | autogenerate でマイグレーションリビジョンを生成 | 実装済み | `docker compose exec app python3 -m sluicery.cli db revision -m "..."` |
| `make backup` | SQLite snapshot + config（暗号化済み資格情報はDB内）を単一archiveへ保存。`INCLUDE_LOGS=1`でlogも含める。SECRET_KEYは含めない | 実装済み | Makefile内の`python3 -m sluicery.backup create`呼出しを参照 |
| `make restore FILE=...` | 自動事前backup・確認・全service停止後に検証済みarchiveを復元し、migration headを確認 | 実装済み | Makefile内の`python3 -m sluicery.backup restore`呼出しを参照 |
| `make purge` | 削除対象を表示して確認した上でコンテナ・イメージ・volume を削除（bind mount 先の実体は削除しない） | 実装済み | `docker compose down --rmi local --volumes --remove-orphans` |

yt-dlpの更新・直前版への安全な切替はCLIからも実行できます。どちらも実ダウンロード付きスモークテストを行います。

```bash
docker compose exec app python3 -m sluicery.cli ytdlp update
docker compose exec app python3 -m sluicery.cli ytdlp rollback
```

Playlist単位の二相同期は次のCLIでも実行できます。`discover`は1つのnetwork Taskの完了を待ち、
`download`は5段チェーンの投入完了時点で戻ります。`--dry-run`は一覧取得と差分表示を行いますが、
Item / Target / Playlistの同期状態を変更せず、downloadも開始しません（実行履歴用のRun / Taskは記録します）。

```bash
docker compose exec --user "$(id -u):$(id -g)" app \
  python3 -m sluicery.cli sync discover --playlist <名前またはID>
docker compose exec --user "$(id -u):$(id -g)" app \
  python3 -m sluicery.cli sync download --playlist <名前またはID>
docker compose exec --user "$(id -u):$(id -g)" app \
  python3 -m sluicery.cli sync run --all
docker compose exec --user "$(id -u):$(id -g)" app \
  python3 -m sluicery.cli sync discover --all --dry-run
```

Staging上で対応するTaskを持たないファイルは、削除せず一覧だけ確認できます。

```bash
docker compose exec app python3 -m sluicery.cli staging orphans
```

Artifactの実体確認とrelinkはCLIからも実行できます。絞り込みは省略できます。
整合性チェックはファイルを削除・移動しません。

```bash
docker compose exec app python3 -m sluicery.cli integrity check \
  --storage <名前またはID> --playlist <名前またはID>
```

### レコード管理 CLI

Web UIと併用して、以下の CLI でもStorage / Profile / Playlistレコードと運用設定を管理できます。CLIは自動化とデバッグのための恒久的な操作手段として維持します。
Storage は `local` と `remote`（rclone）に
対応しています。remote で実装・実機検証する protocol は現時点では SMB だけです。

```bash
docker compose exec app python3 -m sluicery.cli storage add \
  --kind local --name media --path /mnt/media
# password は引数に書かず、表示される非エコーのプロンプトへ入力する
docker compose exec app python3 -m sluicery.cli storage add \
  --kind remote --name smb-media --protocol smb \
  --host <SMB_HOST> --share <SHARE> --path library --user <USER>
docker compose exec app python3 -m sluicery.cli storage test smb-media
docker compose exec app python3 -m sluicery.cli storage space smb-media
docker compose exec app python3 -m sluicery.cli storage ls smb-media
docker compose exec --user "$(id -u):$(id -g)" app \
  python3 -m sluicery.cli storage push smb-media /data/staging/example.bin library/example.bin
docker compose exec app python3 -m sluicery.cli profile add \
  --name video --kind video
docker compose exec app python3 -m sluicery.cli playlist add \
  --name sample --folder-name sample --url 'https://example.com/playlist'
docker compose exec app python3 -m sluicery.cli playlist attach-profile \
  sample video --storage media
docker compose exec app python3 -m sluicery.cli options preview \
  --playlist sample --profile video --kind download
docker compose exec app python3 -m sluicery.cli settings set \
  schedule.download_window '23:00-05:00'
```

CLIでPlaylistやスケジュール設定を変更した場合も、`app`が60秒以内に永続ジョブを整合します。
手動syncと自動syncが同じPlaylistで重なった場合、自動側はTaskを作らず`skipped` Runとして理由を
残します。時間帯制限は自動downloadだけに適用され、明示的な手動downloadは制限しません。

各リソースは `add|list|show|edit|remove` に対応します。Playlist には
`attach-profile` / `detach-profile` もあります。`playlist remove` は
`--keep-items`（無効化・一時停止）または `--delete-items`（関連 DB レコードも削除）を
必ず指定しますが、どちらも保存済みファイルを削除・移動しません。

`storage test` は疎通・認証・一覧・書き込みを個別表示し、結果を DB に保存します。
`storage space` は backend が空き容量取得に対応しない場合に「取得不可」と表示します。
`storage push <storage> <local-path> <dest-rel>` はStorage単体の検証用で、
完成済みの単一ファイルを一時名で転送・検証後に最終化します。既定では同名を上書きしません。

### 暫定の Task 検証 CLI

Taskキュー単体の検証用に、`noop` / `sleep` / `fail` / `fail_unavailable` /
`fail_blocked` / `spawn` のダミーTaskでキューを検証できます。本番で誤実行しないよう既定は無効です。
検証環境でだけ有効化し、設定を読み直すためworkerを再起動してください。

```bash
docker compose exec app python3 -m sluicery.cli settings set worker.enable_test_tasks true
docker compose restart worker-network worker-compute

docker compose exec app python3 -m sluicery.cli task enqueue noop
docker compose exec app python3 -m sluicery.cli task enqueue sleep --payload '{"sec":30}'
docker compose exec app python3 -m sluicery.cli task list --status running
docker compose exec app python3 -m sluicery.cli task show <ID>
docker compose exec app python3 -m sluicery.cli task cancel <ID>
docker compose exec app python3 -m sluicery.cli task retry <ID>

# 検証終了後に無効化し、workerを再起動
docker compose exec app python3 -m sluicery.cli settings unset worker.enable_test_tasks
docker compose restart worker-network worker-compute
```

`--worker-class network|compute` と `--priority N` も指定できます。このCLIとダミーTaskは
Phase 7までの暫定実装であり、通常の同期処理には使用しません。

## トラブルシューティング

起動しない・動作がおかしい場合は [docs/troubleshooting.md](docs/troubleshooting.md) を参照してください。

## ドキュメント

| ファイル | 内容 |
|---|---|
| [Wiki](https://github.com/zawa356/sluicery/wiki) | 導入ガイド・運用レシピ・FAQ |
| [AISTATE.md](AISTATE.md) | セッション間の引き継ぎ用（開発者・AI 向け） |
| [docs/要件定義.md](docs/要件定義.md) | 何を作るか |
| [docs/基本設計.md](docs/基本設計.md) | どう作るか、設計判断の記録 |
| [docs/変更履歴.md](docs/変更履歴.md) | 変更履歴 |
| [docs/deployment.md](docs/deployment.md) | VM への構築手順、`.env` 全項目リファレンス、バックアップ/リストア |
| [docs/troubleshooting.md](docs/troubleshooting.md) | 既知の問題と対処 |
| [docs/footprint.md](docs/footprint.md) | ホスト上に作られるものの一覧 |
| [docs/storage.md](docs/storage.md) | ストレージ方式の解説 |
| [docs/legal.md](docs/legal.md) | 利用上の注意 |
| [docs/reviews/](docs/reviews/) | フェーズごとの独立レビュー記録 |

## 現在の状態

要件定義 §20 の実装は完了しています。受け入れ条件の実測結果と、環境上確認できなかった項目は
[docs/受け入れ条件確認.md](docs/受け入れ条件確認.md) に記録します。公開・pushは別途、履歴監査と
利用者の明示承認後に行います。現在の引き継ぎ状態は [AISTATE.md](AISTATE.md) を参照してください。

## ライセンス

[MIT License](LICENSE)
