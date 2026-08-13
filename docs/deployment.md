# デプロイ手順

README には要点のみを置いています。ここでは VM への構築手順と詳細を記載します。

## 1. 一般的な Linux VM への構築手順

対象は Debian 12/13 または Ubuntu 24.04 LTS 相当の一般的な Linux VM を想定しています。

### OS の準備・Docker のインストール

Docker 公式手順（[docs.docker.com](https://docs.docker.com/engine/install/)）に従い、Docker Engine と
Compose v2 プラグインをインストールしてください。ディストリビューション標準パッケージの
`docker.io` / `docker-compose` ではなく、Docker 公式リポジトリからのインストールを推奨します
（Compose v2 が確実に含まれるため）。

インストール後、ストレージドライバが `overlay2` であることを確認してください。

```bash
docker info | grep "Storage Driver"
```

### ディスクレイアウトの推奨

root パーティションとデータ用パーティションを分けることを推奨します。`data` named volume
（DB・yt-dlp venv・Staging 領域・ログ）と `MEDIA_ROOT`（メディア本体）はどちらも容量を消費するため、
root パーティションを圧迫しないディスク配置にしてください。Docker の named volume の実体は
既定で `/var/lib/docker/volumes/` 配下に置かれます。

### `MEDIA_ROOT` の事前作成と所有者設定

```bash
mkdir -p /path/to/media
chown $(id -u):$(id -g) /path/to/media
```

事前作成しない場合、Docker がコンテナ起動時に root 所有で自動作成します（トラブルの元になるため
避けてください）。`.env` の `PUID` / `PGID` は上記で使った UID/GID と合わせてください。

### 起動、yt-dlp の導入確認

README の Quick Start と同じ手順です。

```bash
git clone <repo> sluicery
cd sluicery
cp .env.example .env
$EDITOR .env
docker compose up -d --build
curl http://localhost:8080/healthz
docker compose exec app python3 -m sluicery.cli ytdlp status
```

ブラウザで `http://<SERVER>:8080/` を開き、初期管理者でログインします。`ADMIN_PASSWORD` を空に
した場合の初期パスワードは、管理者を新規作成した起動のログにだけ表示されます。

## 2. `.env` 全項目リファレンス

| 変数 | 既定値 | 内容 |
|---|---|---|
| `SECRET_KEY` | （必須、既定値なし） | Fernet 暗号化鍵。未設定時は起動を拒否する。ローテーション非対応 |
| `ADMIN_USERNAME` | `admin` | DBにユーザーが無い初回起動時だけ作成する管理者アカウント名 |
| `ADMIN_PASSWORD` | （空） | 初回管理者のパスワード。空ならランダム生成し起動ログへ一度だけ表示する |
| `AUTH_COOKIE_SECURE` | `false` | `true` でセッションCookieへ `Secure` 属性を付ける。HTTPS利用時は有効化する |
| `TZ` | `Asia/Tokyo` | タイムゾーン。cron 式はこの TZ で解釈する |
| `PUID` / `PGID` | `1000` / `1000` | 生成ファイルの所有者。ホスト側で `id` コマンドの値と合わせる |
| `UMASK` | `022` | 生成ファイルの権限 |
| `HTTP_PORT` | `8080` | Web UI / REST API の待受ポート |
| `DATA_DIR` | `/data` | DB・yt-dlp venv・ログを置く永続ディレクトリ（コンテナ内パス） |
| `STAGING_DIR` | `/data/staging` | ダウンロードと後処理の一時領域（コンテナ内パス、常にローカル） |
| `MEDIA_ROOT` | `/mnt/media` | `local` kind の Storage が書き込む最終保存先（ホスト側パス） |
| `ALLOW_EXEC` | `false` | `--exec` 系オプションの許可。Profile 側の `allow_exec` との両方が有効な場合のみ許可される（要件定義 §9.3） |
| `AUTO_MIGRATE` | `true` | 起動時に DB マイグレーションを自動適用するか |

Staging しきい値・ログ保持日数・discover/download/integrity/yt-dlp 更新の cron 式・ダウンロードの
並列度やレート制限などの運用パラメータは `.env` ではなく DB の `setting` テーブル側で管理します
（`sluicery settings list` / `set` / `unset`。`docs/基本設計.md` D-005）。

## 3. Staging 容量の見積もり方

Staging 領域の必要量 ≈ 取得する最大ファイルサイズ × 並列度 + 余裕。

- 「並列度」は運用パラメータ（`setting` テーブル側）で設定するダウンロードの同時実行数
- 後処理（postprocess）中は一時的に元ファイルと処理後ファイルが同居するため、想定より大きく
  見積もっておくこと
- ここを見誤ると、Staging 容量不足で同期が詰まります（`target.status` が `blocked` になる設計。
  リトライ回数は消費しません）

Phase 8の開発機検証では、最終保存先へ224ファイル・823.3 MiBを生成した。正常完了分の
Staging元はindex後に削除された一方、HTTP 403多発で安全停止した時点では、再開に使える
中間ファイルを含む53ファイル・約85.7 MiBがStagingに残った。このため、見積りには並列実行中の
ファイルだけでなく、リトライ・中断分を調査するまで保持する余裕を加える。実測値は素材と選択形式に
強く依存するため、容量保証ではない。

## 4. リバースプロキシ経由での公開

sluicery アプリ自体は HTTPS 終端を行いません（要件定義 §12）。外部からアクセスする場合は
nginx / Caddy 等のリバースプロキシを別途 `HTTP_PORT`（既定 8080）の手前に置き、TLS 終端はそちら側の
責務としてください。HTTPS経由で運用するときは `.env` の `AUTH_COOKIE_SECURE=true` を設定し、
ブラウザが平文HTTPへセッションCookieを送らないようにしてください。`HttpOnly` と `SameSite=Lax` は
常に付与されます。

### 取得用Cookie

PlaylistごとのCookie設定画面では、Netscape形式のCookieファイルをwrite-onlyで登録できます。
内容は`SECRET_KEY`で暗号化してDBへ保存し、画面には設定済みかどうかだけを表示します。既定は
無効であり、有効化にはアカウント停止リスクへの確認が必要です。実行時の平文はComposeが
tmpfsとしてマウントする`/run/sluicery`だけへ600で展開し、成功・失敗にかかわらず削除します。
`--cookies-from-browser`はコンテナ内にブラウザが無いため使用できません。

### 定期同期

APSchedulerは`app`サービスのプロセス内だけで起動し、jobを既存SQLiteへ永続化します。
worker、ホストcrontab、systemd timerへ追加設定は不要です。discover / downloadのグローバルcron、
±ジッター、download実行可能時間帯はWeb UIの設定画面で変更でき、Playlist編集画面では個別cronと
一時停止を設定できます。cronと次回予定は`.env`の`TZ`で解釈・表示されます。

CLIで設定やPlaylistを変更した場合も60秒以内にjobstoreへ反映されます。設定反映を即時確認したい
場合は`app`を通常の再起動で起動し直せます。停止中に複数の予定時刻を過ぎても、復帰時は直近1回へ
畳み込まれます。同一Playlistの手動syncと自動syncは並行せず、自動側の見送り理由はRun履歴で
確認できます。

## 5. バックアップとリストア

> **未実装:** バックアップ / リストアは要件定義 §20 の Phase 20 で実装予定です。
> 以下は完成後の予定インターフェースであり、現時点では実行できません。

```bash
make backup
# → backups/sluicery-<timestamp>.tar.gz
```

**`backups/` 配下のアーカイブには Storage の認証情報が暗号化された形で含まれます**
（`docs/legal.md`）。`.gitignore` で除外済みですが、保管場所・共有範囲には注意してください。
`SECRET_KEY` とセットで漏洩すると復号可能です。

```bash
make restore FILE=backups/sluicery-<timestamp>.tar.gz
```

## 6. アンインストール

```bash
make purge
```

削除されるもの・されないものは [docs/footprint.md](footprint.md) を参照してください。要点：

- 削除される：`app` / `worker-network` / `worker-compute` コンテナ、ローカルビルドイメージ
  `sluicery:local`・`sluicery:local-test`（存在する場合）、named volume `data`、compose ネットワーク
- 削除されない：`MEDIA_ROOT` 配下のメディア本体（bind mount の実体）
- named volume `data` には Staging（ダウンロード・後処理の一時領域）も含まれるため、進行中の
  ダウンロードがあれば中間ファイルを失います。実行前に `worker-network` / `worker-compute` が
  待機状態であることを確認してください

## 7. 検証環境の記録

**検証日: 2026-08-09。** 開発機とは別の Ubuntu VM に、開発機からファイルをコピーせず
`git bundle` 転送 → `git clone` の手順で新規構築し、`docs/phase3.5_指示書_改訂版.md` §5.2 の
17項目をすべて実施した。

```
- OS: Ubuntu 22.04.5 LTS
- カーネル: 5.15.0-142-generic
- Docker: 27.3.1 / Compose v2.29.7
- ストレージドライバ: overlay2
- cgroup バージョン: v2
- メモリ: 3.8GiB（搭載）
- ディスク: 60GB（うち空き 45GB、検証開始時点）
- ビルド時間: 3分1秒（初回、ビルドキャッシュ無しの状態から）
- イメージサイズ: sluicery:local 797MB（Deno 2.9.5 同梱後の開発機再計測値）
- 起動直後のメモリ使用量: app 80.7MiB / worker-network 53.8MiB / worker-compute 53.9MiB
- ダウンロード中のメモリ使用量: app 88.4MiB（D-015 の検証用ファイル、約30MB。ファイルサイズが
  小さく、起動直後との有意差は見られなかった）
```

**注意（この検証環境固有の制約）**：このVMは本検証専用に用意された空のVMではなく、
Immich・Portainer 等の既存サービスが同居する共用ホストだった（ポート 8080 の空きは確認済み）。
「汚れていない環境」という意味では厳密な条件を満たしていないが、`git clone` からの新規構築・
ビルド・起動・yt-dlp 導入・fetch・test/lint/purge がすべて成功したことは確認できている。
compose はプロジェクト名でリソースを分離するため、他サービスへの影響は無かった。

検証中に2件の実バグ、1件のホスト側環境要因を発見し、修正・記録済み：

- Quick Start の `SECRET_KEY` 生成コマンドが `python:3.12-slim` に `cryptography` が無く失敗する
  問題（[docs/troubleshooting.md](troubleshooting.md) 参照）
- `MEDIA_ROOT` 未事前作成時に `app` が再起動ループに陥る問題。Quick Start に事前作成手順を追加
- `docker compose exec app`（`--user` 無指定）がrootとして実行され、`ytdlp fetch` 等の生成物が
  root 所有になる問題。CLI 例に `--user "$(id -u):$(id -g)"` を追加
- （sluicery とは無関係）検証 VM 側で `systemd-resolved` が起動失敗しており DNS が引けなかった。
  VM 側の `/etc/resolv.conf` を静的設定して復旧。sluicery のドキュメント・コードに変更は無い

## 8. LXC 環境について

**現時点で未検証です。** 以下は参考情報であり、検証済みの手順ではありません。

- `nesting=1,keyctl=1` が必要になる見込み
- 非特権コンテナでは UID オフセット +100000 のズレを考慮する必要がある見込み
- `mount` kind（bind mount 以外のマウント方式）は利用不可の見込み

Proxmox LXC 環境での動作を試す場合は、上記を出発点にしつつ実際に踏んだ問題を
[docs/troubleshooting.md](troubleshooting.md) にフィードバックしてください。

## 9. SMB Storage の設定と検証

Phase 5 で実装・実機検証済みの remote protocol は SMB だけです。実値を文書やシェル履歴へ
残さないよう、以下のプレースホルダを各環境の値へ読み替えてください。password は引数に書かず、
非エコーのプロンプトへ入力します。

```bash
docker compose exec app python3 -m sluicery.cli storage add \
  --kind remote --name smb-media --protocol smb \
  --host <SMB_HOST> --share <SHARE> --path library --user <USER>
docker compose exec app python3 -m sluicery.cli storage show smb-media
docker compose exec app python3 -m sluicery.cli storage test smb-media
docker compose exec app python3 -m sluicery.cli storage space smb-media
docker compose exec app python3 -m sluicery.cli storage ls smb-media
docker compose exec --user "$(id -u):$(id -g)" app \
  python3 -m sluicery.cli storage push smb-media /data/staging/example.bin library/example.bin
```

自動化から password を渡す場合は `--password-stdin` を明示し、標準入力の先頭行だけへ渡します。
`storage show` は資格情報を `********（設定済み）` と表示し、平文を返しません。`storage push` は
Phase 7 の pipeline が入るまでの検証用であり、既定では同名ファイルを上書きしません。

### Phase 5 SMB 実機検証の実測値

検証日 2026-08-09。専用の書込可能共有・読取専用共有・専用ユーザーを使用し、識別情報は記録して
いません。値は同一 LAN 上の一回の測定であり、性能保証ではありません。

| 項目 | 結果 |
|---|---|
| 4段階接続テスト | 約2.4秒（疎通・認証・一覧・作成/読出/削除） |
| 単一ファイル転送 | 約5.5 MiB/s（16 MiB、CLI 起動時間を含む） |
| `rclone about` | SMB で利用可能。空き容量を bytes で取得できた |

実機検証20項目は、正常系、誤ホスト、誤password、存在しないパス、読取専用、上書き拒否、
転送中断、孤児プロセス、マスク、所有者まで確認した。共有ホスト自体は停止せず、転送開始後に
app コンテナだけを compose ネットワークから一時切断した。確立済み接続の喪失が `unreachable`、
最終名なし、Staging 元保持、一時名報告、rclone 残留0となることを確認し、直後に再接続した。
試験後は生成ファイルと資格情報入りの試験レコードを削除した。
