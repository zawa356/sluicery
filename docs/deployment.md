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

## 2. `.env` 全項目リファレンス

| 変数 | 既定値 | 内容 |
|---|---|---|
| `SECRET_KEY` | （必須、既定値なし） | Fernet 暗号化鍵。未設定時は起動を拒否する。ローテーション非対応 |
| `ADMIN_USERNAME` | `admin` | Phase 9 で実装予定の初期管理者アカウント名。現時点では作成処理に未接続 |
| `ADMIN_PASSWORD` | （空） | Phase 9 で実装予定。現時点では自動生成・ログ出力を行わない |
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

## 4. リバースプロキシ経由での公開

sluicery アプリ自体は HTTPS 終端を行いません（要件定義 §12）。外部からアクセスする場合は
nginx / Caddy 等のリバースプロキシを別途 `HTTP_PORT`（既定 8080）の手前に置き、TLS 終端はそちら側の
責務としてください。

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
- イメージサイズ: sluicery:local 591MB
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
