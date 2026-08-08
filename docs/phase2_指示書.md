# Phase 2 実装指示書 — 設定層・DB スキーマ・リポジトリ層

| 項目 | 内容 |
|---|---|
| 対象 | 要件定義 §20 実装順序 #2 |
| 前提 | Phase 1（リポジトリ骨格・Docker 環境）完了済み |
| 作成日 | 2026-08-08 |

本書は `docs/要件定義.md` と `CLAUDE.md` を補完するものであり、置き換えるものではない。**着手前に両方を読むこと。**

---

# 0. 着手前の是正タスク（P0）

**Phase 2 の実装より先に、以下を片付けること。** 1コミットにまとめてよい（`chore:` または `docs:`）。

## 0.1 `.gitignore` のアンカー修正

`data/` `staging/` `media/` `logs/` がアンカーされておらず、任意階層にマッチする。`tests/data/` にフィクスチャを置くとサイレントに除外されるため、Phase 2 で確実に踏む。

```gitignore
/data/
/staging/
/media/
/logs/
```

先頭に `/` を付けてルート直下限定にする。

## 0.2 `AISTATE.md` の用語是正

要件定義の用語からドリフトしている。以下を修正する。

| 現在 | 修正後 |
|---|---|
| 双方向同期（discover / download） | 二相同期（discover / download） |
| 通信中での削除は絶対にローカルファイルの削除に伝搬させない | 配信元での削除は絶対にローカルファイルの削除に伝播させない |
| フォーマット探査機能 | フォーマット検査機能 |
| 孤立リンク画面 | 手動リンク画面 |
| ランブック / リストアドキュメント一式 | バックアップ / リストア、ドキュメント一式 |
| 自己ホスト向け | 自己ホスト型 |

## 0.3 `CLAUDE.md` §3.2 への追記

以下のルールを追加する。

> - 「重要な合意」セクションおよび進捗リストの項目名は、**要件定義から逐語コピーする。** 要約・言い換えをしない。用語の揺れは次セッションでの誤実装に直結する。

## 0.4 その他のドキュメント修正

| 対象 | 内容 |
|---|---|
| `docs/legal.md` | 「sluicery からの削除操作（同期による delisted 化など）では削除されません」を修正。delisted を削除操作と呼ばず、「配信元でコンテンツが削除・非公開化されても、ローカルに保存済みのファイルが削除されることはありません（同期では `delisted` として記録するのみ）」とする |
| `docs/footprint.md` | (a) `${MEDIA_ROOT}` がホストに存在しない場合、Docker が **root 所有で**ディレクトリを自動作成する旨を追記。(b) `make purge` の説明に、Staging（`data` volume 内）も消える＝進行中ダウンロードの中間ファイルが失われる旨を追記 |
| `docs/基本設計.md` §2 | モジュール表に `cli.py` を追加 |
| `docs/変更履歴.md` | Phase 1 の1行記載を、Docker 環境 / Makefile / entrypoint / パッケージ骨格 / ドキュメント体系に分割。D-001〜D-003 に対応する変更が追える粒度にする |

## 0.5 実装上の確認

| # | 確認内容 |
|---|---|
| 1 | `entrypoint.sh` の setpriv 呼び出しに `--init-groups`（または `--clear-groups`）と `--inh-caps=-all` が付いているか。`--reuid` / `--regid` だけでは補助グループが root のまま残り、SMB 経由の権限問題（要件 N-5）を踏む |
| 2 | tmpfs `/run/sluicery` の所有者を PUID/PGID に合わせる処理が entrypoint にあるか |
| 3 | `${MEDIA_ROOT}` と Staging の存在・書き込み可否を entrypoint で事前チェックし、失敗時に明確なメッセージで停止するか |

## 0.6 D-002 の補強

ffmpeg の取得 URL を **build-arg 化**する（`ARG FFMPEG_URL` / `ARG FFMPEG_SHA256`）。

上流の johnvansickle は、最新版を固定名で配布し、旧版はバージョン番号付きファイル名で `old-releases/` 配下に残す。したがってローテーション発生時は、`old-releases/` の versioned URL に差し替えるだけで同一バージョンを再取得できる。**この復旧手順を Dockerfile のコメントと `docs/基本設計.md` の D-002 に追記すること。**

## 0.7 `requirements.lock`

Docker 導入済みのため、`make lock` を実行して生成物をコミットする。生成後、`docker compose up -d --build` が通ることを確認し、AISTATE の未解決 #1 / #2 を解消する。

**これが通らない限り Phase 2 本体に進まない。**

---

# 1. Phase 2 のスコープ

## 1.1 含むもの

1. 設定層（`.env` の読み込みと検証）
2. DB 基盤（エンジン、セッション、PRAGMA）
3. ORM モデル定義（要件定義 §7 の全テーブル）
4. Alembic セットアップと初期マイグレーション
5. 設定アクセサ（`.env` と `setting` テーブルの二層）
6. リポジトリ層（基本 CRUD）
7. 上記に対する CLI コマンド
8. ユニットテスト

## 1.2 含まないもの

- yt-dlp の呼び出し（Phase 3）
- ビジネスロジック（同期、パイプライン、状態遷移の実処理）
- Web UI、認証
- Storage アダプタの実装（モデル定義のみ行う）
- `SECRET_KEY` のローテーション・再暗号化（後述 §3.4 の方針により**対応しない**）

リポジトリ層は「CRUD と単純な検索」までとする。**状態遷移のロジックをリポジトリに書かない。** それは Phase 7〜8 で `core/` に置く。

---

# 2. 設定層

## 2.1 実装

`src/sluicery/config.py`。Pydantic Settings（pydantic-settings）を使用する。

## 2.2 `.env` に置く項目

**`.env` はセキュリティ境界とインフラ設定に限定する。** 運用パラメータは DB 側（§4）に置く。

| 変数 | 型 | 既定 | 検証 |
|---|---|---|---|
| `SECRET_KEY` | str | （なし） | **必須。未設定なら起動を拒否。** 長さ・形式を検証し、Fernet 鍵として使えることを確認する |
| `ADMIN_USERNAME` | str | `admin` | |
| `ADMIN_PASSWORD` | str \| None | None | 初回起動時のみ使用 |
| `TZ` | str | `Asia/Tokyo` | 有効な IANA タイムゾーンであること |
| `PUID` / `PGID` | int | 1000 | |
| `UMASK` | str | `022` | |
| `HTTP_PORT` | int | 8080 | |
| `DATA_DIR` | Path | `/data` | 存在・書き込み可 |
| `STAGING_DIR` | Path | `/data/staging` | 存在・書き込み可 |
| `MEDIA_ROOT` | Path | `/mnt/media` | 存在・書き込み可 |
| `ALLOW_EXEC` | bool | `false` | **UI から変更不可。`.env` のみ。**（要件 §9.3） |
| `AUTO_MIGRATE` | bool | `true` | §5.3 |
| `DB_PATH` | Path | `{DATA_DIR}/sluicery.db` | |
| `LOG_LEVEL` | str | `INFO` | |

`STAGING_WARN_PCT` / `STAGING_STOP_PCT` / `LOG_RETENTION_DAYS` / `YTDLP_UPDATE_CRON` / `YTDLP_SMOKETEST_URL` は**運用パラメータなので DB 側に移す**（要件 §17 の一覧からの変更点。`.env.example` を更新し、`docs/基本設計.md` の設計判断に記録すること）。

## 2.3 起動時検証

`SECRET_KEY` 未設定時は、**何が足りないかと対処方法を明示したメッセージ**を出して終了する。スタックトレースだけで落とさない。

```
ERROR: SECRET_KEY が設定されていません。
  .env に SECRET_KEY を設定してください。
  生成例: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  この鍵を紛失すると、保存済みの認証情報を復号できなくなります。バックアップを取得してください。
```

`config check` CLI コマンド（§7）で、全項目の検証結果を一覧表示できるようにする。

---

# 3. DB 基盤

## 3.1 エンジンとセッション

`src/sluicery/db/session.py`。SQLAlchemy 2.x スタイル（`Mapped` / `mapped_column`）を使用する。

## 3.2 PRAGMA（必須）

接続イベント（`event.listens_for(engine, "connect")`）で以下を設定する。

| PRAGMA | 値 | 理由 |
|---|---|---|
| `foreign_keys` | `ON` | **SQLite は既定でオフ。明示しないと外部キー制約が一切効かない** |
| `journal_mode` | `WAL` | 要件 N-7 |
| `busy_timeout` | `5000` | ワーカーとの競合時に即座に落ちないため |
| `synchronous` | `NORMAL` | WAL との組み合わせで妥当 |

エンジン生成時に接続文字列へ `check_same_thread=False` を渡し、ワーカーからの利用に耐えること。

## 3.3 タイムスタンプ

**DB には全て UTC の aware datetime で保存する。** 表示時のみ `TZ` に変換する。

- カラム型は `DateTime(timezone=True)`
- アプリ側で `datetime.now(timezone.utc)` を使う。`utcnow()` は使わない（naive になる）
- SQLAlchemy の `server_default=func.now()` は SQLite で naive になるため使わない。**アプリ側でデフォルト値を設定する**
- cron 式の解釈のみ `TZ` を用いる（要件 §10.1）

この方針を破ると、スケジューラと履歴表示で必ず食い違いが出る。

## 3.4 暗号化カラム

`SECRET_KEY` を鍵とする Fernet で暗号化する `TypeDecorator` を実装する。

```python
class EncryptedJSON(TypeDecorator):
    """JSON を Fernet で暗号化して TEXT カラムに保存する。"""
```

- モデル層で透過的に読み書きできること
- **ログ・UI 出力のマスクは別層の責務**（要件 §6.5 の「共通層に置く」）。この型は暗号化のみを担う

### 鍵の指紋チェック

**`SECRET_KEY` のローテーションには対応しない**（鍵紛失時はクレデンシャル再入力とする方針）。ただし、鍵が変わった際に復号エラーが散発して原因不明になるのを防ぐため、以下を実装する。

- 初回起動時、`SECRET_KEY` のハッシュ（指紋）を `setting` テーブルに保存する
- 起動時に現在の鍵の指紋と照合し、**不一致なら明確な警告を出す**

```
WARNING: SECRET_KEY が前回起動時と異なります。
  保存済みの認証情報は復号できません。各 Storage の認証情報を再入力してください。
  意図しない変更の場合、以前の SECRET_KEY に戻してください。
```

起動自体は継続してよい（再入力すれば復旧できるため）。

この方針を `README.md` と `docs/legal.md` に明記すること。

---

# 4. ORM モデル

## 4.1 対象テーブル

要件定義 §7.1 の全テーブルを実装する。

`user` / `storage` / `profile` / `playlist` / `playlist_profile` / `item` / `target` / `artifact` / `task` / `run` / `setting` / `event_log`

## 4.2 Enum

**Python 側は `Enum`、DB 側は文字列カラム + CHECK 制約**とする。SQLite のネイティブ enum は使わない（状態値追加時のマイグレーションが軽くなる）。

定義する Enum（値は要件定義 §7.2 から**逐語で**取ること）：

| Enum | 値 |
|---|---|
| `StorageKind` | `local` / `remote` / `mount` |
| `ProfileKind` | `video` / `music` / `other` |
| `LayoutStrategy` | `flat` / `custom` |
| `PlaylistKindHint` | `video` / `music` / `mixed` |
| `ItemMembership` | `active` / `delisted` |
| `TargetStatus` | `pending` / `queued` / `downloading` / `processing` / `downloaded` / `failed` / `unavailable` / `blocked` / `missing` / `ignored` |
| `ArtifactRole` | `source` / `derived` |
| `TaskType` | `discover` / `download` / `verify` / `postprocess` / `publish` / `index` / `integrity_check` / `retention` / `update_ytdlp` |
| `WorkerClass` | `network` / `compute` |
| `TaskStatus` | 実装時に確定し、要件定義との差分を基本設計 §3 に記録する |
| `RunTrigger` | `manual` / `schedule` / `api` |

## 4.3 制約とインデックス

**一意制約（要件定義に明記されているもの）**

- `item`: `UNIQUE(playlist_id, source_id)`
- `target`: `UNIQUE(item_id, playlist_profile_id)`
- `playlist_profile`: `UNIQUE(playlist_id, profile_id)`
- `user`: `UNIQUE(username)`。加えて**2件目のレコード作成を禁止**する（DB 制約またはリポジトリ層で担保。どちらにしたか基本設計に記録）

**インデックス（Phase 6〜8 のクエリを想定）**

- `item(playlist_id, membership)`
- `item(source_id)` — relink の再走査で使用
- `target(status)`
- `target(playlist_profile_id, status)`
- `task(status, worker_class, priority)` — ワーカーの claim クエリ用
- `task(depends_on_task_id)`
- `artifact(storage_id)`
- `run(started_at)`

## 4.4 命名規約

Alembic の batch モードで制約名が必要になるため、`MetaData` に `naming_convention` を設定する。**これを後から入れるのは困難なので Phase 2 で必ず行う。**

```python
naming_convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
```

## 4.5 共通 Mixin

`created_at` / `updated_at` を持つ Mixin を定義する。`updated_at` は `onupdate` で更新する（§3.3 の方針に従い、アプリ側で UTC aware な値を生成すること）。

## 4.6 主キー

整数の autoincrement とする。UUID は使わない。

## 4.7 カスケード

- `playlist` 削除時の `item` の扱いは、**要件 §13.2 でユーザーに選択させる仕様**である。DB レベルで無条件 CASCADE にしない
- `item` 削除時の `target`、`target` 削除時の `artifact` は CASCADE でよい
- **いかなる場合も、レコード削除がファイル削除を引き起こさないこと**（設計原則1）。Phase 2 の時点でファイル操作コードを書かないため自動的に守られるが、リポジトリ層のドキュメント文字列に明記しておく

---

# 5. Alembic

## 5.1 設定

- `render_as_batch=True` を `env.py` に設定する。**SQLite は `ALTER TABLE` の制限が厳しく、これがないと後続フェーズでカラム変更・制約変更ができない**
- `compare_type=True` を有効化
- `target_metadata` に §4.4 の `naming_convention` 付き `MetaData` を渡す

## 5.2 初期マイグレーション

`alembic revision --autogenerate` で生成した後、**手で確認・整形してからコミットする。** autogenerate は CHECK 制約やインデックスを取りこぼすことがある。

生成したマイグレーションに対し、`upgrade` → `downgrade` → `upgrade` が通ることを確認する。

## 5.3 適用方式

**自動適用とし、`.env` のフラグで無効化できる形にする。**

- `AUTO_MIGRATE=true`（既定）：`app` サービスの起動時に `alembic upgrade head` を実行する
- `AUTO_MIGRATE=false`：適用せず、未適用のリビジョンがある場合は**警告を出して起動を継続**する（起動不能にしない）
- **`worker-network` / `worker-compute` はマイグレーションを実行しない。** 起動時に `alembic current` が `head` と一致するまで待機し、タイムアウトしたらエラー終了する
- 手動適用用に `make migrate` を用意する

複数プロセスからの同時実行を避けるため、この分担を厳守すること。

---

# 6. 設定アクセサ

`src/sluicery/core/settings.py`（新規）。

## 6.1 二層構造

| 層 | 保存先 | 変更手段 |
|---|---|---|
| インフラ・セキュリティ設定 | `.env` | ファイル編集 + 再起動 |
| 運用パラメータ | `setting` テーブル | Web UI / CLI |

## 6.2 既定値の持ち方

**既定値はコード側に定義し、`setting` テーブルには「ユーザーが上書きした項目のみ」を保存する。**

初期投入で全項目を DB に書き込まないこと。全項目を書き込むと、将来コード側の既定値を変更しても既存環境に反映されなくなる。

```python
# 概念
value = db_override.get(key, CODE_DEFAULTS[key])
```

## 6.3 定義する運用パラメータ

要件定義から拾えるもの。Phase 2 では**定義と読み書きのみ**行い、実際に参照する処理は各フェーズで実装する。

| キー | 型 | 既定 | 参照フェーズ |
|---|---|---|---|
| `staging.warn_pct` | int | 80 | 7 |
| `staging.stop_pct` | int | 90 | 7 |
| `log.retention_days` | int | 30 | 11 |
| `schedule.discover_cron` | str | 6時間ごと | 12 |
| `schedule.download_cron` | str | 6時間ごと | 12 |
| `schedule.integrity_cron` | str | 日次 | 13 |
| `schedule.jitter_minutes` | int | 5 | 12 |
| `schedule.download_window` | str \| None | None | 12 |
| `ytdlp.update_cron` | str | 週次 | 15 |
| `ytdlp.smoketest_url` | str | （CC ライセンスの公開URL） | 15 |
| `download.item_concurrency` | int | 1 | 6 |
| `download.concurrent_fragments` | int | 3 | 3 |
| `download.sleep_requests` | float | 1.5 | 3 |
| `download.sleep_interval` | int | 3 | 3 |
| `download.max_sleep_interval` | int | 12 | 3 |
| `download.limit_rate` | str | `8M` | 3 |
| `download.retries` | int | 5 | 3 |
| `download.fragment_retries` | int | 10 | 3 |
| `retry.max_attempts` | int | 5 | 8 |
| `defaults.video.*` / `defaults.music.*` | — | 要件 §9.5 の表 | 4 |

`defaults.video.*` / `defaults.music.*` は、Phase 4 のオプション合成モデルで層3（種別既定）として使う。Phase 2 ではキー体系だけ定め、値を保持できる状態にする。

## 6.4 型付きアクセサ

キー文字列を直に扱わせない。型付きの属性としてアクセスできるインターフェースを用意する。

キャッシュを持つ場合は、更新時に確実に無効化されること（**マルチプロセス構成のため、プロセス跨ぎのキャッシュ無効化は行わない。TTL 付きの短命キャッシュか、キャッシュなしとする**）。

---

# 7. リポジトリ層

`src/sluicery/db/repositories/`。

## 7.1 方針

- 汎用の `BaseRepository`（get / list / create / update / delete / count）を用意し、エンティティごとに継承する
- **セッションは外部から受け取る。** リポジトリが自前でセッションを作らない
- **クエリはリポジトリの内側に閉じる。** 呼び出し側に SQLAlchemy の式を書かせない
- **状態遷移のロジックを書かない。** リポジトリは永続化のみを担う。遷移の妥当性判定は Phase 7〜8 の `core/` に置く
- 各リポジトリの docstring に「レコード削除はファイル削除を引き起こさない」旨を明記する

## 7.2 各リポジトリに用意する検索

Phase 2 時点で最小限。呼び出し側が固まっていないものは作らない。

| リポジトリ | 追加メソッド |
|---|---|
| `UserRepository` | `get_single()`、`create_single()`（2件目を拒否） |
| `StorageRepository` | `list_enabled()` |
| `PlaylistRepository` | `list_enabled()`、`get_with_profiles()` |
| `ItemRepository` | `get_by_source_id(playlist_id, source_id)`、`upsert_many()`、`list_by_membership()` |
| `TargetRepository` | `list_by_status()`、`count_by_status(playlist_id)` |
| `ArtifactRepository` | `find_by_source_id(storage_id, source_id)`（relink 用の下準備） |
| `TaskRepository` | `claim_next(worker_class)`（**アトミックであること**）、`list_pending()` |
| `RunRepository` | `list_recent(limit)` |
| `SettingRepository` | `get_all_overrides()`、`set_override()`、`delete_override()` |

`TaskRepository.claim_next()` は Phase 6 のワーカーが依存する中核。**単一の UPDATE ... RETURNING、または `BEGIN IMMEDIATE` を用いた排他で実装し、二重 claim が起きないことをテストで確認する。**

---

# 8. CLI

`src/sluicery/cli.py` に追加する。

| コマンド | 動作 |
|---|---|
| `sluicery config check` | `.env` の全項目の検証結果を一覧表示。**シークレットはマスクする** |
| `sluicery db upgrade` | `alembic upgrade head` |
| `sluicery db current` | 現在のリビジョンと head の一致を表示 |
| `sluicery db revision -m "..."` | autogenerate によるリビジョン生成 |
| `sluicery settings list` | 全運用パラメータを、既定値／上書き値の別付きで表示 |
| `sluicery settings get <key>` | |
| `sluicery settings set <key> <value>` | 型検証を通した上で保存 |
| `sluicery settings unset <key>` | 上書きを削除し既定値に戻す |

`Makefile` に `make migrate` / `make revision MSG="..."` を追加する。

---

# 9. テスト

`tests/` 配下に配置する。

## 9.1 テスト用 DB

**インメモリではなく一時ファイルの SQLite を使う。** WAL とマルチスレッドの挙動が本番構成と揃うため。フィクスチャで毎テスト作り直す。

## 9.2 必須テスト

| 対象 | 内容 |
|---|---|
| 設定層 | `SECRET_KEY` 未設定で起動拒否。不正な `TZ` の拒否。`ALLOW_EXEC` の既定が false |
| PRAGMA | `foreign_keys` が実際に ON になっており、外部キー違反が拒否される |
| タイムスタンプ | 保存した値が UTC aware で読み出せる |
| 暗号化カラム | 書き込み → 読み出しで元の値に戻る。DB 上の生データが平文でない |
| 鍵指紋 | 異なる `SECRET_KEY` で起動した場合に警告が出る |
| 一意制約 | `item(playlist_id, source_id)` / `target(item_id, playlist_profile_id)` の重複が拒否される |
| user | 2件目の作成が拒否される |
| マイグレーション | `upgrade` → `downgrade` → `upgrade` が通る |
| 設定アクセサ | 上書きなしで既定値が返る。上書き後に上書き値が返る。unset で既定値に戻る |
| `claim_next` | **並行呼び出しで同一 Task が二重に claim されない** |

## 9.3 マーカー

外部ネットワークに依存するテストは `@pytest.mark.network` を付ける（Phase 2 では該当なしの見込み）。

---

# 10. コミット計画

CLAUDE.md §4.2 に従い、**細かく分ける。** Phase 1 は全体が1コミットだったが、Phase 2 では以下程度の粒度を目安とする。

| # | コミット |
|---|---|
| 1 | `chore: .gitignore のアンカー修正とドキュメント用語の是正`（§0） |
| 2 | `chore: requirements.lock を生成し compose の起動を確認` |
| 3 | `feat: 設定層を実装（Pydantic Settings、SECRET_KEY 検証）` |
| 4 | `feat: DB エンジンとセッション管理（PRAGMA 設定）` |
| 5 | `feat: 暗号化カラム型 EncryptedJSON と鍵指紋チェック` |
| 6 | `feat: ORM モデル定義（user / storage / profile / playlist / playlist_profile）` |
| 7 | `feat: ORM モデル定義（item / target / artifact / task / run / setting / event_log）` |
| 8 | `chore: Alembic をセットアップし初期マイグレーションを生成` |
| 9 | `feat: 設定アクセサ（既定値はコード側、DB は上書きのみ保持）` |
| 10 | `feat: リポジトリ層の基本 CRUD` |
| 11 | `feat: Task の claim_next をアトミックに実装` |
| 12 | `feat: CLI に config / db / settings サブコマンドを追加` |
| 13 | `test: Phase 2 のユニットテストを追加` |
| 14 | `docs: 基本設計・変更履歴・AISTATE を更新` |

完了後、**`checkpoint/step-02` タグを打つ**（CLAUDE.md §4.4）。

---

# 11. 完了条件

1. `make lock` 済みで、`docker compose up -d --build` が成功する
2. `SECRET_KEY` 未設定時、対処方法を含む明確なメッセージで起動が停止する
3. `sluicery config check` が全項目の検証結果を表示し、シークレットがマスクされている
4. `alembic upgrade head` で全テーブルが作成される
5. `AUTO_MIGRATE=true` で `app` 起動時に自動適用され、worker は head 到達まで待機する
6. `AUTO_MIGRATE=false` で自動適用されず、未適用があれば警告を出して起動が継続する
7. `sluicery settings list` が既定値／上書き値の別付きで一覧表示する
8. `settings set` → `list` → `unset` → `list` で値が期待通り遷移する
9. 外部キー違反が実際に拒否される
10. 暗号化カラムの生データが DB 上で平文でない
11. 異なる `SECRET_KEY` での起動時に警告が出る
12. `claim_next` の並行テストで二重 claim が発生しない
13. `upgrade` → `downgrade` → `upgrade` が通る
14. `pytest` が全てパスする
15. `docs/基本設計.md` §3 に、要件定義 §7 の論理設計との差分が記載されている（差分がなければその旨）
16. `AISTATE.md` が更新され、Phase 3 の着手点が書かれている

---

# 12. ドキュメント更新義務

CLAUDE.md §2.1 に従い、以下を更新する。

| ファイル | 更新内容 |
|---|---|
| `docs/基本設計.md` §2 | モジュール表に `core/settings.py`、`db/repositories/` を追加 |
| `docs/基本設計.md` §3 | 実装スキーマと要件定義 §7 の差分（`TaskStatus` の値、`.env` から DB へ移した設定項目など） |
| `docs/基本設計.md` §7 | 新たに行った設計判断を D-004 以降として追記。最低限、以下は記録対象：`SECRET_KEY` ローテーション非対応の決定、運用パラメータを `.env` から DB へ移した判断、`claim_next` の排他方式 |
| `docs/変更履歴.md` | 未リリース欄に追加項目を記載 |
| `README.md` | `SECRET_KEY` の生成方法と、**紛失時は認証情報の再入力が必要**である旨 |
| `docs/legal.md` | 同上（クレデンシャルの取り扱いセクションに追記） |
| `.env.example` | 項目の追加・削除を反映（`AUTO_MIGRATE` 追加、運用パラメータの削除） |
| `AISTATE.md` | 全文書き換え |

---

# 13. 実装時の注意（再掲）

- `SECRET_KEY` は**ローテーション非対応**。再暗号化コマンドを作らない
- 運用パラメータの既定値は**コード側**。`setting` テーブルには上書きのみ
- タイムスタンプは**全て UTC aware**
- `foreign_keys` PRAGMA を**必ず ON**
- Alembic は **`render_as_batch=True`**
- `naming_convention` は**Phase 2 で必ず設定**（後から入れるのは困難）
- リポジトリ層に**状態遷移ロジックを書かない**
- **レコード削除がファイル削除を引き起こす実装をしない**
- 判断に迷ったら要件定義 §1.4 の設計原則に照らし、それでも決まらなければ**確認を取る**
