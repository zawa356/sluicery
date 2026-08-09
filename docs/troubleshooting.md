# トラブルシューティング

実際に踏んだ問題のみを記載しています。想像で書いた項目はありません。VM 実機検証（Phase 3.5）で
新たに踏んだ問題は、検証後にここへ追記します。

## `ytdlp status` が `broken` になる

**症状**：`sluicery ytdlp status` の結果が `broken` になり、`fetch` / `probe` が実行できない。

**原因**：venv のリネーム後にシェバン（pip の console_scripts が参照する絶対パス）が書き換わって
おらず、旧パスを指したまま実行不能になっている（`docs/基本設計.md` 参照）。

**対処**：

```bash
docker compose exec app python3 -m sluicery.cli ytdlp install --force
```

## Quick Start の `SECRET_KEY` 生成コマンドが `ModuleNotFoundError: No module named 'cryptography'` で失敗する

**症状**：README Quick Start の `docker run --rm python:3.12-slim python3 -c "from cryptography.fernet import Fernet; ..."` が
`ModuleNotFoundError: No module named 'cryptography'` で失敗する。

**原因**：`python:3.12-slim` イメージには `cryptography` パッケージが含まれていない。VM 実機検証（Phase 3.5
§5 #2）で、追加手順なしの起動を確認する過程で発見した。

**対処**：コンテナ内で `pip install` してから実行する。

```bash
docker run --rm python:3.12-slim sh -c \
  "pip install -q --root-user-action=ignore cryptography && python3 -c \
  'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
```

README・`docs/deployment.md` は本コマンドに修正済み。

## `app` コンテナが `FATAL: MEDIA_ROOT（/mnt/media）に PUID=1000/PGID=1000 で書き込めません` で再起動を繰り返す

**症状**：`docker compose up -d --build` 後、`app` コンテナが `Restarting` を繰り返す。ログに
`FATAL: MEDIA_ROOT（/mnt/media）に PUID=1000/PGID=1000 で書き込めません。ホスト側の所有者・権限を
確認してください。` と出る。

**原因**：`MEDIA_ROOT`（既定 `/mnt/media`）を事前作成せずに起動すると、Docker がコンテナ起動時に
root 所有でディレクトリを自動作成する。`entrypoint.sh` は PUID/PGID（既定 1000/1000）での書き込み
可否を起動時に検査しており、root 所有のままだと書き込めず起動を拒否する（意図した fail-fast 動作）。
VM 実機検証（§5 #2）で、`MEDIA_ROOT` の事前作成を Quick Start の手順に含めていなかったために発見した。

**対処**：`MEDIA_ROOT` を事前作成し、所有者を PUID/PGID（またはホスト側の自分の uid/gid）に合わせる。

```bash
mkdir -p /mnt/media
sudo chown "$(id -u)":"$(id -g)" /mnt/media
docker compose up -d
```

README・`docs/deployment.md` の Quick Start は本手順を含む形に修正済み。

## local Storage の `push` が named volume から bind mount への転送で失敗する

**症状**：`/data` の Staging ファイルを `/mnt/media` 配下の local Storage へ `push` すると、
接続テストは成功するのに publish が失敗する。

**原因**：Docker Desktop / WSL2 の一部構成では、named volume と bind mount が同じ `st_dev` を
返しても mount 境界をまたぐ rename / hardlink は `EXDEV` で失敗する。Phase 5 の実機検証で発見した。

**対処**：現行版は Staging 元を保持したまま hardlink を試し、`EXDEV` や非対応時は一時名への copy に
フォールバックする。検証後は no-replace rename で最終化し、成功後だけ Staging 元を削除する。
修正前の版を使用している場合は更新する。一時名が報告された場合は内容を確認してから手動で扱い、
sluicery は自動削除しない。

## `docker compose exec app` で生成したファイルが `root` 所有になる

**症状**：`docker compose exec app python3 -m sluicery.cli ytdlp fetch <URL>` 等で Staging に生成された
ファイルの所有者が `root:root` になっている（`ls -la /data/staging/` で確認できる）。

**原因**：`docker compose exec` は既定でコンテナの root ユーザーとして実行される。コンテナの主プロセス
（`entrypoint.sh` 経由で起動する `web`/`worker`）は `setpriv` で PUID/PGID に権限降格しているが、これは
主プロセスにのみ適用され、`exec` で新規に起動するプロセスには影響しない。VM 実機検証（§5 #9）の
`ytdlp fetch` 実行時に発見した。

**対処**：ファイルを生成する CLI コマンドは `--user` でホスト側のユーザーと揃えて実行する。

```bash
docker compose exec --user "$(id -u):$(id -g)" app python3 -m sluicery.cli ytdlp fetch <URL>
```

## `docker compose down -v` で volume が消える

**症状**：`docker compose down -v` を実行すると、DB・yt-dlp venv・Staging 領域を含む named volume
`data` が丸ごと削除され、再構築が必要になる。

**原因**：`-v` オプションは compose が管理する volume を明示的に削除する（意図した動作）。

**対処**：開発中は `docker compose down`（`-v` なし）を使う。データを保持したまま停止したい場合、
`-v` を付けないこと。volume ごと削除したい場合の後始末は `make purge` を使う
（[docs/footprint.md](footprint.md) 参照）。
