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

## `docker compose down -v` で volume が消える

**症状**：`docker compose down -v` を実行すると、DB・yt-dlp venv・Staging 領域を含む named volume
`data` が丸ごと削除され、再構築が必要になる。

**原因**：`-v` オプションは compose が管理する volume を明示的に削除する（意図した動作）。

**対処**：開発中は `docker compose down`（`-v` なし）を使う。データを保持したまま停止したい場合、
`-v` を付けないこと。volume ごと削除したい場合の後始末は `make purge` を使う
（[docs/footprint.md](footprint.md) 参照）。
