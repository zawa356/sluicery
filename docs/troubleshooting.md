# トラブルシューティング

## restoreが`SECRET_KEY指紋が現在の鍵と一致しません`で停止する

backup作成時と同じ`SECRET_KEY`を`.env`へ戻してから再実行する。鍵はarchiveへ意図的に含めていない。
元の鍵を紛失した場合、DB内のStorage資格情報とPlaylist Cookieは復号できない。復号不能を承知して
DB・非秘密設定だけを救出する場合に限り`ALLOW_SECRET_KEY_MISMATCH=1`を明示し、復元後に全資格情報を
再入力する。安易にこのguardを無効化しない。

## restoreがarchive検証またはSQLite WAL checkpointで停止する

archiveのpath / link / size / SHA-256 / SQLite整合性に問題がある場合、上書き前に停止する。別archiveを
使用する。WAL checkpointエラーでは他のsluicery serviceやDBを開く外部processが残っていないか確認する。
`make restore`は3serviceを停止してから書き換えるが、失敗時はtrapで再起動を試みるため、最後に
`docker compose ps`と`docker compose logs`を確認する。

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
フォールバックする。検証後は no-replace rename で最終化し、Staging 元は Artifact が確定する index 完了後まで保持する。
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

## 大規模同期で HTTP 403 が多発する

**症状**：複数Playlistの`sync run --all`中、download Taskが短時間に連続してHTTP 403になる。

**原因**：Phase 8実機検証時のイメージにはYouTubeのJavaScript challengeを処理するランタイムが
なく、保存済みログで「対応するJavaScriptランタイムなし」の警告を確認した。これに加え、配信元の
一時的な制限や利用可能なformatの変化が個別に重なる場合がある。

**対処**：Deno同梱後のイメージへ更新し、`deno --version`と少数の`ytdlp probe`で検出を確認する。
HTTP 403とボット確認は自動的に`blocked`となり、既定で1時間空けて再試行される。ワーカーの
並列度やレート上限を引き上げず、十分時間を置いて3〜5件だけで再確認する。形式非互換は
`Requested format is not available`として`unavailable`になるため、Profileのformatも確認する。
中断時のStagingファイルは`--continue`に使うため自動削除されない。

## app再起動後に停止中のスケジュールが実行されない

**症状**：jobstoreには停止中に期限を過ぎた`next_run_time`が残っているのに、`app`起動後のmisfireが
発火しない。

**原因**：起動時整合で、設定が変わっていない永続jobまで`replace_existing`すると、期限超過時刻が
新しい将来時刻へ置き換わり、APSchedulerがmisfireとして認識できなくなる。Phase 12の実機検証前に
この問題をテストで検出した。

**対処**：Phase 12以降はcron、`TZ`、jitter、引数、coalesce、同時実行数、猶予が一致するjobを
保持し、設定が変わった場合だけ再登録する。停止中の複数回分は`coalesce=true`で復帰直後の1回へ
畳み込まれる。予定が見えない場合は、Playlistがenabledかつpausedでないこと、cron式、
`app`の稼働、ダッシュボードの次回予定を順に確認する。worker側にschedulerログが無いのは正常である。

## yt-dlp更新後のスモークテストでDeno検出に失敗する

**症状**：yt-dlp更新履歴のスモーク結果が`deno_not_detected`または`challenge_warning`となる。
自動更新は、健全な直前版があればそこへ戻る。

**原因**：新しいyt-dlpが要求するDenoのversionや実行契約が、イメージへ固定したDenoと合わなくなった
可能性がある。Denoはvenvではなくruntimeイメージへ焼き込まれているため、yt-dlp自動更新の対象外である。

**対処**：Deno公式releaseの同じversionに属するURLとSHA-256を確認し、Dockerfileの
`DENO_VERSION`、`DENO_URL`、`DENO_SHA256`を同じ組で更新して再buildする。checksumを無効化したり、
URLだけを`latest`へ向けたりしない。

```bash
docker compose build --no-cache app worker-network worker-compute
docker compose up -d
docker compose exec app python3 -m sluicery.cli ytdlp update
```

更新後はWebのyt-dlp更新履歴で、Deno検出、challenge警告なし、実ダウンロード、Staging削除が
すべて成功したことを確認する。
