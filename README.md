# sluicery

yt-dlp を用いた自己ホスト向けのプレイリスト同期サーバー。登録したプレイリスト（動画・音楽）の内容を
ローカルまたはネットワークストレージへ取得・保持し、実行のたびに差分だけを追記する。

## Quick Start

`make` を前提にせず、`docker compose` 直で動かせる最短手順です。詳細は後続セクションを参照してください。

```bash
git clone <repo> sluicery
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

yt-dlp は起動後にバックグラウンドで自動導入されます。導入完了は以下で確認できます。

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
- **現在は開発途上であり、Web UI は未実装です**（要件定義 §20 の実装順序 #9 以降）。現段階は CLI のみで
  操作します

## 前提条件

**動作環境**

| 項目 | 内容 |
|---|---|
| OS | Linux x86_64、cgroup v2 |
| Docker | Docker Engine + Compose v2。検証環境では Docker 27.3.1 / Compose v2.29.7 で動作確認 |
| ストレージドライバ | `overlay2` であることを推奨。確認コマンド：`docker info \| grep "Storage Driver"` |
| ポート | 8080（`.env` の `HTTP_PORT` で変更可）が空いていること |

**容量とリソース**（検証環境での実測値。詳細は [docs/deployment.md](docs/deployment.md) §7）

- イメージサイズ：591MB
- ビルド時間：約3分（初回、ビルドキャッシュ無しの状態から）
- メモリ使用量：起動直後で `app`/`worker-network`/`worker-compute` 合計 約190MiB
- Staging 領域の必要量 = 取得する最大ファイルサイズ × 並列度 + 余裕。ここを見誤ると同期が詰まります

**ネットワーク**

- 外向き HTTPS：PyPI（yt-dlp の導入・更新）、取得対象サイトへの到達性が必要
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
| WSL2 | 未検証 |
| NAS（Synology / QNAP 等） | 未検証 |

## 初期設定

`requirements.lock` / `requirements-dev.lock` はコミット済みのため、通常のセットアップに
`make lock` は不要です。

`SECRET_KEY` を設定しない場合、明確なエラーメッセージを出して起動を拒否します。意図せず
`SECRET_KEY` が変わった場合、起動時に警告が表示されます。バックアップ / リストアは Phase 20 で
実装予定であり、現時点の `make backup` / `make restore` は実行できません。

初回起動時、`.env` の `ADMIN_USERNAME` / `ADMIN_PASSWORD` で管理者アカウントが作成されます。
`ADMIN_PASSWORD` を空のままにした場合はランダムなパスワードが生成され、起動ログに一度だけ出力されます。

## 運用コマンド

| コマンド | 内容 | 実装状況 | `make` 無し環境での等価コマンド |
|---|---|---|---|
| `make up` | ビルドして起動 | 実装済み | `docker compose up -d --build` |
| `make down` | 停止（データは保持） | 実装済み | `docker compose down` |
| `make logs` | ログ追跡 | 実装済み | `docker compose logs -f` |
| `make shell` | `app` コンテナにシェル接続 | 実装済み | `docker compose exec app /bin/bash` |
| `make sync` | 全プレイリストの同期を即時実行 | **未実装（Phase 8）** | — |
| `make test` | dev 依存込みの test ステージをビルドし、コンテナ内で pytest を実行 | 実装済み | `docker build --target test -t sluicery:local-test . && docker run --rm --entrypoint pytest sluicery:local-test` |
| `make lint` | ruff / mypy をコンテナ内で実行 | 実装済み | 上記 test イメージに対し `docker run --rm --entrypoint ruff sluicery:local-test check src tests` 等 |
| `make lock` | `requirements.in` / `requirements-dev.in` から lock ファイルを再生成（依存を更新したときのみ） | 実装済み | — |
| `make migrate` | DB マイグレーションを手動適用（`AUTO_MIGRATE=false` 運用時など） | 実装済み | `docker compose exec app python3 -m sluicery.cli db upgrade` |
| `make revision MSG="..."` | autogenerate でマイグレーションリビジョンを生成 | 実装済み | `docker compose exec app python3 -m sluicery.cli db revision -m "..."` |
| `make backup` | DB + 設定 + シークレットを単一アーカイブに書き出し | **未実装（Phase 20）** | — |
| `make restore FILE=...` | バックアップから復元 | **未実装（Phase 20）** | — |
| `make purge` | 削除対象を表示して確認した上でコンテナ・イメージ・volume を削除（bind mount 先の実体は削除しない） | 実装済み | `docker compose down --rmi local --volumes --remove-orphans` |

## トラブルシューティング

起動しない・動作がおかしい場合は [docs/troubleshooting.md](docs/troubleshooting.md) を参照してください。

## ドキュメント

| ファイル | 内容 |
|---|---|
| [AISTATE.md](AISTATE.md) | セッション間の引き継ぎ用（開発者・AI 向け） |
| [docs/要件定義.md](docs/要件定義.md) | 何を作るか |
| [docs/基本設計.md](docs/基本設計.md) | どう作るか、設計判断の記録 |
| [docs/変更履歴.md](docs/変更履歴.md) | 変更履歴 |
| [docs/deployment.md](docs/deployment.md) | VM への構築手順、`.env` 全項目リファレンス、バックアップ/リストア |
| [docs/troubleshooting.md](docs/troubleshooting.md) | 既知の問題と対処 |
| [docs/footprint.md](docs/footprint.md) | ホスト上に作られるものの一覧 |
| [docs/storage.md](docs/storage.md) | ストレージ方式の解説 |
| [docs/legal.md](docs/legal.md) | 利用上の注意 |

## 現在の状態

実装は要件定義 §20 の順序で段階的に進めています。現在地は [AISTATE.md](AISTATE.md) を参照してください。
本リポジトリは開発途上であり、Web UI 等の未実装機能があります（上記「これは何か / 何ではないか」参照）。

## ライセンス

[MIT License](LICENSE)
