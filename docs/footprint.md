# ホスト上に作られるものの一覧

compose 変更時は必ず本ドキュメントを更新すること（CLAUDE.md §2.1）。
アプリがホスト上に作成するのは、compose が宣言する volume と bind mount のみ（要件定義 §4.3）。

## コンテナ

| サービス | イメージ | 役割 |
|---|---|---|
| `app` | `sluicery:local`（ローカルビルド） | Web UI + REST API + スケジューラ |
| `worker-network` | `sluicery:local`（同一イメージ） | discover / download / publish |
| `worker-compute` | `sluicery:local`（同一イメージ） | postprocess / verify（現バージョンではほぼ待機） |

全サービスは Compose の `init: true` を使い、コンテナ内 PID 1 に tini 相当の init を置く。
これは終了した子孫プロセスの reap のためであり、ホスト上に新しいファイル、volume、bind mount、
ポート、常駐プロセスを追加しない。worker の `stop_grace_period` は30秒で、アプリ内の20秒の
shutdown猶予より長くしている。

## イメージ

- `sluicery:local`：`Dockerfile` の `runtime` ステージからローカルビルド。ベースは `python:3.12-slim`（digest 固定）。
- ビルド時に外部から取得するもの：rclone（バージョン固定 + checksum 検証）、ffmpeg/ffprobe 静的ビルド（checksum 検証）、Deno（バージョン固定 + checksum 検証）
- `sluicery:local-test`：`make test` / `make lint` 実行時に `Dockerfile` の `test` ステージ（`runtime` + dev 依存 + `tests/`）からビルドされる。`docker compose` の管理下ではないが、`make purge` の削除対象に含まれる（存在すれば削除、無ければ何もしない）
- `.dockerignore`：ビルドコンテキストから `.git` / `docs/` / `data/` 等を除外する。Dockerfile は個別 `COPY` のみを使うためイメージの中身には影響せず、送信量を減らす目的のみ

## ネットワーク

- compose のデフォルトネットワーク（`sluicery_default`）のみ。追加のネットワークは作成しない。

## Volume

| 名前 | マウント先 | 内容 |
|---|---|---|
| `data`（named volume） | `/data`（全サービス） | SQLite DB、yt-dlp venv、Staging 領域、ログ |

`/data/ytdlp/` の内部構造（Phase 3）：

```
/data/ytdlp/
├── versions/
│   └── <version>/        # venv 本体（bin/yt-dlp を含む）。導入・削除は app のみ
├── current -> versions/<version>   # symlink。worker はここ越しに読み取り専用でアクセス
└── .lock                 # fcntl.flock 用。中身は空
```

## Bind mount

| ホスト側 | コンテナ側 | 用途 |
|---|---|---|
| `${MEDIA_ROOT}`（既定 `/mnt/media`） | `/mnt/media`（全サービス） | `local` kind の Storage が書き込む最終保存先。ホストに存在しない場合、Docker が **root 所有で**ディレクトリを自動作成する（要事前作成を推奨） |

## tmpfs

| マウント先 | 用途 |
|---|---|
| `/run/sluicery` | rclone 設定ファイル・Cookie の実行時展開先（平文をディスクに残さないため、要件定義 §6.5, §9.7） |

## ポート

| ポート | 用途 |
|---|---|
| `${HTTP_PORT}`（既定 8080） | `app` の Web UI / REST API（コンテナ⇔ホスト直結、HTTPS 終端はしない） |

## `make purge` で削除されるもの／されないもの

削除される：`app` / `worker-network` / `worker-compute` コンテナ、ローカルビルドイメージ `sluicery:local`、`sluicery:local-test`（存在する場合）、named volume `data`、compose ネットワーク。

削除されない：`${MEDIA_ROOT}` 配下の bind mount 実体（メディア本体）。`compose.privileged.yaml` を併用している場合はホスト側のマウントポイント自体の後始末は別途 `umount` が必要（要件定義 §6.6、実装順序 #19 以降）。

**注意：** named volume `data` には Staging（ダウンロード・後処理の一時領域）も含まれる。`make purge` はこれも消すため、進行中のダウンロードがあれば中間ファイルが失われる。実行前は必ず `worker-network` / `worker-compute` が待機状態であることを確認すること。
