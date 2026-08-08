# Phase 3 実装指示書 — yt-dlp venv 管理・CLI ラッパ・受入試験

| 項目 | 内容 |
|---|---|
| 対象 | 要件定義 §20 実装順序 #3 |
| 前提 | Phase 1（骨格）・Phase 2（設定層・DB・リポジトリ層）完了済み |
| 作成日 | 2026-08-08 |

本書は `docs/要件定義.md`（特に §5）と `CLAUDE.md` を補完する。**着手前に両方を読むこと。**

Phase 3 は、**実際に yt-dlp を動かして1本ダウンロードするところまで**を到達点とする。受入試験（§11）を通過して初めて完了とする。

---

# 0. 着手前の是正タスク（P0）

Phase 3 の実装より先に処理する。1〜2コミットにまとめてよい。

## 0.1 開発依存のロック（優先度：高）

`requirements.lock` に pytest / ruff / mypy が含まれておらず、テストがホスト環境依存になっている。`docker compose up` は再現できてもテストは再現できない状態。

- `requirements-dev.in` を作成し、`requirements-dev.lock` を `--generate-hashes` で生成する
- `make lock` が両方を生成するようにする
- `make test` を追加する。**コンテナ内で実行すること**（例：`docker compose run --rm --entrypoint "" app pytest`）
- Dockerfile に dev ステージを設けるか、テスト実行時のみ dev 依存を入れる方式にする（どちらでもよいが、本番イメージに dev 依存を焼き込まないこと）
- `README.md` の運用コマンド表に `make test` を追加

## 0.2 README のセットアップ手順（優先度：高）

要件定義 §4.1 および受入条件1は「clone → `cp .env.example .env` → `docker compose up -d`」で起動することを求めている。現在の README はその間に `make lock` を必須ステップとして挟んでおり、矛盾している。

`requirements.lock` はコミット済みなので、**`make lock` を「依存を更新するときのみ実行する」運用コマンドに降格**し、セットアップ手順から外す。

## 0.3 worker の restart ループ解消（優先度：高）

現状、worker は起動→即終了→`restart: unless-stopped` による再起動を繰り返している。これは以下の実害がある。

- Docker の restart backoff が効き始め、Phase 6 で**本物の異常が起きたときに区別できなくなる**
- ログが再起動メッセージで汚れる
- `depends_on` やヘルスチェックの判定が意味をなさない

**未実装の処理を持つ worker は、終了せず待機ループに入ること。** Phase 3 では yt-dlp の導入完了を待つポーリングループがそのまま待機処理になる（§2.5）。

## 0.4 内部設定キーの名前空間分離

`SECRET_KEY` の指紋が `setting` テーブルに保存されているが、これは運用パラメータではない。

- 内部キーを `_internal.*` の名前空間に分ける
- `sluicery settings list` / `get` / `set` の対象から除外する
- Phase 17 の設定エクスポートからも除外される前提を、`core/settings.py` のコメントに明記する

## 0.5 記録の追記

| 対象 | 内容 |
|---|---|
| `docs/基本設計.md` D-006 | `claim_next()` のアトミック性は「ローカルファイルシステム上の SQLite」を前提とする旨を追記（DB をネットワークストレージに置くと成立しない） |
| `docs/基本設計.md` §3 または新規 D | 要件 §7.2 の `blocked` は `target.status` の値であり、`TaskStatus` には対応する値がない。Phase 6/7 で「Storage 到達不能により Task を保留する」際に Task 側の表現を決める必要がある旨を、検討事項として記録 |
| `AISTATE.md` 重要な前提 | 「ユーザー作成は必ず `UserRepository.create_single()` を経由する。生の `session.add(User(...))` では2件目を防げない（D-009）」を1行追加 |

## 0.6 軽微

- `compose.yaml` の `depends_on` を `condition: service_healthy` に変更する
- `README.md` に、`make` が無い環境向けの `docker compose` 直コマンドを併記する（AISTATE 未解決 #2 への対応）

---

# 1. スコープ

## 1.1 含むもの

1. yt-dlp の venv 管理（インストール、バージョン別保持、切替、削除）
2. degraded 起動と導入状態の管理
3. yt-dlp CLI ラッパ（subprocess、タイムアウト、プロセスグループ制御）
4. 進捗出力のパーサ
5. エラー分類
6. `ytdlp_release` テーブルの追加と Alembic リビジョン
7. `sluicery ytdlp` CLI サブコマンド群
8. ユニットテスト
9. **受入試験（実機での手動確認）**

## 1.2 含まないもの

- **スモークテスト、自動更新スケジュール、自動ロールバック**（Phase 15）
 ※ ロールバックの**土台となるバージョン切替機構**は Phase 3 で作る
- オプション合成・レイヤー構造（Phase 4）
- Task キュー、ワーカーの実処理（Phase 6）
- Storage への publish（Phase 5・7）
- DB への進捗永続化（Phase 6/7）

## 1.3 責務の境界（重要）

**Phase 3 の CLI ラッパは「引数リストを受け取って実行し、結果を返す」だけに留める。**

予約引数の注入、レイヤー合成、プロファイル適用は Phase 4（`core/options.py`）の責務である。ここで混ぜると Phase 4 で二重管理になる。

例外は §9 の受入試験用コマンド（`probe` / `fetch`）で、これらは**暫定の固定オプション**を使う。Phase 4 で置き換える前提であることをコード内コメントに明記すること。

---

# 2. venv 管理

## 2.1 ディレクトリ構造

```
/data/ytdlp/
├── versions/
│   ├── 2026.07.21/          # venv 本体（bin/yt-dlp を含む）
│   └── 2026.08.01/
├── current -> versions/2026.08.01
└── .lock
```

実行パスは常に `/data/ytdlp/current/bin/yt-dlp`。呼び出し側は symlink 越しにアクセスし、バージョン番号を意識しない。

## 2.2 インストール手順

1. ファイルロック（§2.4）を取得する
2. 一時ディレクトリ `versions/.tmp-<uuid>/` に `python -m venv` で venv を作る
3. `pip install "yt-dlp==<version>"`（バージョン未指定時は `pip install yt-dlp` で最新）
4. `bin/yt-dlp --version` を実行し、**実際にインストールされたバージョンを取得する**（事前に PyPI を照会しない。依存を減らすため）
5. 一時ディレクトリを `versions/<version>/` にリネームする。同名が既に存在する場合は一時ディレクトリを破棄して既存を採用する
6. symlink を差し替える（§2.3）
7. `ytdlp_release` レコードを記録する（§6）
8. 保持世代数を超えた古いバージョンを削除する（§2.6）

途中で失敗した場合、**一時ディレクトリを確実に削除し、`current` は一切変更しない。**

## 2.3 symlink の原子的差し替え

`os.symlink` で一時名の symlink を作り、`os.replace`（`rename(2)`）で `current` に上書きする。

**`os.remove` してから `os.symlink` する実装にしないこと。** その間に別プロセスが `current` を参照すると失敗する。

## 2.4 排他制御

- `/data/ytdlp/.lock` に対する `fcntl.flock` で排他する
- **インストール・切替・削除を行うのは `app` サービスのみ。** worker は読み取り専用でアクセスする
- 3サービスが同時に venv を作りにいくと確実に壊れるため、この分担は厳守する

## 2.5 degraded 起動

**yt-dlp が未導入でもアプリは起動する。**

| サービス | 挙動 |
|---|---|
| `app` | 起動する。`ytdlp.auto_install` が true なら、起動後に**非同期で**インストールを試行する。失敗してもアプリは動き続ける |
| `worker-*` | `current` が有効になるまで**待機ループに入る**（ポーリング間隔 10秒）。終了しない。ログは初回と状態変化時のみ出力し、毎回出さない |

導入状態は以下の3値で表現する。

| 状態 | 意味 |
|---|---|
| `ready` | `current` が存在し、`--version` が成功する |
| `not_installed` | `current` が存在しない |
| `broken` | `current` は存在するが `--version` が失敗する |

`broken` は、volume の破損や中断されたインストールで発生しうる。自動で修復を試みず、**状態として報告する**（`sluicery ytdlp install` での明示的な再導入を促す）。

## 2.6 世代管理

- 保持世代数は `ytdlp.keep_versions`（既定3）
- 削除対象は古い順。ただし **`current` と、`current` の直前に active だったバージョンは必ず残す**（Phase 15 のロールバック先を失わないため）
- 削除は `sluicery ytdlp remove <version>` でも手動実行できる。`current` の削除は拒否する

---

# 3. CLI ラッパ

`src/sluicery/downloader/ytdlp.py`。

## 3.1 インターフェース

```python
@dataclass
class TimeoutPolicy:
    idle_sec: int | None          # 無進捗タイムアウト
    absolute_sec: int | None      # 絶対上限
    term_grace_sec: int           # SIGTERM 後 SIGKILL までの猶予

@dataclass
class RunResult:
    returncode: int
    classification: Classification       # §5
    stdout_lines: list[str]              # プレフィックス除去済みの --print 出力
    progress_events: list[ProgressEvent] # §4（呼び出し側が保持を選べること）
    stderr_tail: str                     # 末尾 N KB
    log_path: Path                       # stderr 全文
    duration_sec: float
    terminated_by: str | None            # "idle" / "absolute" / "cancel" / None

class YtdlpRunner:
    def run(self, args: list[str], *, timeout: TimeoutPolicy,
            on_progress: Callable[[ProgressEvent], None] | None = None,
            cwd: Path | None = None) -> RunResult: ...
    def cancel(self) -> None: ...
```

## 3.2 プロセス起動

- **`shell=True` を使わない。** 引数はリストで渡す（要件 N-10）
- **`start_new_session=True` で新しいプロセスグループを作る**
- 環境変数は**明示的に構築する**。親プロセスの環境をそのまま継承しない。渡すのは `PATH` / `HOME` / `TMPDIR` / ロケール等の必要最小限に限る
- **ロケールを `LC_ALL=C` に固定する。** §5 のエラー分類が英語メッセージのパターンマッチに依存するため、ロケールが揺れると分類が壊れる

## 3.3 出力の読み取り

- stdout と stderr を**別々に、並行して**読む。片方だけを読むとパイプバッファが埋まってデッドロックする
- スレッド2本、または `selectors` を使う。どちらでもよいが、**必ず並行に読むこと**
- stdout には進捗 JSON と `--print` 出力が混在するため、**プレフィックスで区別する**

プレフィックス規約を `src/sluicery/downloader/protocol.py` に定数として定義する。

```
SLUICERY_PROGRESS <json>     # --progress-template で出力
SLUICERY_PRINT <text>        # --print で出力
```

Phase 4 のオプション合成がこのプレフィックスを含む引数を注入する。**Phase 3 では規約を定義し、ラッパがそれを解釈できる状態にする**（実際の注入は §9 の暫定コマンドで行う）。

## 3.4 タイムアウト

監視スレッドを1本立て、以下を判定する。

| 種別 | 設定キー | 既定 |
|---|---|---|
| 無進捗タイムアウト | `ytdlp.idle_timeout_sec` | 300 |
| 絶対上限 | `ytdlp.absolute_timeout_sec` | 21600（6時間） |
| discover 用の全体タイムアウト | `ytdlp.discover_timeout_sec` | 300 |
| SIGTERM → SIGKILL の猶予 | `ytdlp.term_grace_sec` | 10 |

無進捗タイムアウトは、**最後に進捗イベントまたは出力行を受け取った時刻**からの経過で判定する。

## 3.5 終了処理（重要）

**必ずプロセスグループ単位で終了させる。**

```
os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
  → term_grace_sec 待機
  → os.killpg(..., signal.SIGKILL)
```

yt-dlp は ffmpeg を子プロセスとして起動する。**親プロセスだけを kill すると ffmpeg が孤児として残り、Staging に不完全ファイルを書き続ける。** 要件 N-8 の「孤児プロセスを残さない」はここで担保される。

正常終了時も、`proc.wait()` 後にプロセスグループが空になっていることを確認すること。

## 3.6 ログとマスク

- stderr は全文をログファイルに書き、`RunResult` には末尾 N KB（既定 64KB）のみを保持する
- **実行コマンドラインをログに出す際は、必ずマスク層を通す**（要件 §6.5）。Cookie ファイルパス、`--password`、URL 中のトークンなどが対象
- マスクは共通層に置き、呼び出し側での付け忘れが起きない構造にする

---

# 4. 進捗パーサ

`src/sluicery/downloader/progress.py`。

## 4.1 スコープ

**パーサとイベント型の定義までとし、DB への書き込みは行わない。**

進捗を毎行 DB に書くと SQLite が競合する（要件 N-7）。永続化は Phase 6/7 のワーカー側で、スロットリング（2秒間隔または5%刻み、最終状態は必ず書く）をかけて行う。責務を分けておけば、後から書き込み戦略だけ差し替えられる。

## 4.2 イベント型

```python
@dataclass
class ProgressEvent:
    status: str                  # downloading / finished / error
    downloaded_bytes: int | None
    total_bytes: int | None      # total_bytes または total_bytes_estimate
    speed: float | None
    eta: int | None
    fragment_index: int | None
    fragment_count: int | None
    filename: str | None
    raw: dict                    # 元の JSON（将来の拡張用）
```

## 4.3 堅牢性

パーサは**壊れた入力で例外を投げないこと**。以下を必ずテストする。

- 不完全な JSON 行（プロセスが途中で kill された場合に発生する）
- プレフィックスのない行（yt-dlp 自身の警告など）
- 数値であるべきフィールドが `NA` や空文字の場合
- 極端に長い行

解釈できない行は**破棄せずログに残し**、進捗イベントとしては無視する。

---

# 5. エラー分類

`src/sluicery/downloader/errors.py`。

## 5.1 分類

| 分類 | 意味 | 後続の扱い（Phase 8） |
|---|---|---|
| `ok` | 成功 | — |
| `failed` | 一時的な失敗 | リトライ対象。`retry_count` を消費する |
| `unavailable` | 回復不能 | 自動リトライしない |
| `blocked` | 外的要因による保留 | **`retry_count` を消費しない** |

## 5.2 原則

- **未知のエラーは `failed` を既定とする。** 安全側に倒す
- `unavailable` に倒すのは、**確実に回復不能と判定できるものだけ**に限定する（削除済み、非公開、メンバー限定、地域制限など）
- 分類ルールは yt-dlp の英語メッセージに依存しており壊れやすい。**ルールを一箇所のテーブルに集約する**
- 分類できなかった stderr は記録し、後からルールを育てられるようにする

## 5.3 ルールテーブル

```python
ERROR_RULES: list[tuple[re.Pattern[str], Classification, str]]
# (パターン, 分類, 理由コード)
```

初期ルールとして最低限カバーするもの（正確な文言は実装時に yt-dlp のソースまたは実機で確認すること）：

**`unavailable`**
- 動画が利用不可・削除済み
- 非公開動画
- メンバー限定コンテンツ
- 地域制限
- 未公開のプレミア公開・予定されたライブ配信

**`blocked`**
- HTTP 429（レート制限）
- 名前解決の失敗、接続拒否、ネットワーク到達不能
- bot 判定によるアクセス拒否（Cookie が必要な旨のメッセージ）

**`failed`（明示ルールなし＝既定）**
- フォーマット選択の失敗、一時的な HTTP 5xx、断片ダウンロードの失敗、その他すべて

## 5.4 分類結果の利用

`RunResult.classification` と理由コードを返すのみ。**Target の状態遷移はここで行わない**（Phase 8 の `core/` の責務）。

---

# 6. データモデルの追加

要件定義 §5.3 は「更新履歴（バージョン、日時、結果）を DB に記録する」ことを求めているが、Phase 2 のスキーマに該当テーブルがない。Phase 3 で追加する。

## 6.1 `ytdlp_release`

```
id
version                  -- "2026.08.01"
installed_at
source                   -- initial | manual | auto
status                   -- installed | active | removed
activated_at             -- nullable
deactivated_at           -- nullable
smoketest_result_json    -- nullable。Phase 15 で使用
notes                    -- nullable
created_at, updated_at
```

- `active` は高々1件。切替時に旧レコードを `installed` に戻す
- 制約で強制するか、リポジトリ層で担保するかは実装判断。**どちらにしたか基本設計に記録すること**（D-009 と同じ論点）

## 6.2 Alembic

新規リビジョンを生成する。**D-008 の既知の制限により、autogenerate の出力に CHECK 制約の削除→再作成が偽陽性として混ざる。実際の変更に無関係なものは手で取り除いてからコミットすること。**

`upgrade` → `downgrade` → `upgrade` が通ることを確認する。

---

# 7. 設定パラメータの追加

`core/settings.py` の `CODE_DEFAULTS` に追加する。

| キー | 型 | 既定 |
|---|---|---|
| `ytdlp.auto_install` | bool | `true` |
| `ytdlp.keep_versions` | int | 3 |
| `ytdlp.idle_timeout_sec` | int | 300 |
| `ytdlp.absolute_timeout_sec` | int | 21600 |
| `ytdlp.discover_timeout_sec` | int | 300 |
| `ytdlp.term_grace_sec` | int | 10 |
| `ytdlp.stderr_tail_kb` | int | 64 |

---

# 8. モジュール構成

| ファイル | 責務 |
|---|---|
| `downloader/version.py` | venv 管理（インストール・切替・削除・状態判定・ロック） |
| `downloader/ytdlp.py` | CLI ラッパ（subprocess・タイムアウト・プロセスグループ制御） |
| `downloader/progress.py` | 進捗パーサ、`ProgressEvent` |
| `downloader/errors.py` | エラー分類ルールと `Classification` |
| `downloader/protocol.py` | 出力プレフィックス規約の定数 |
| `db/repositories/ytdlp_release.py` | `ytdlp_release` のリポジトリ |

`docs/基本設計.md` §2 のモジュール表を更新すること。

---

# 9. CLI コマンド

`sluicery ytdlp` サブコマンド群を追加する。

| コマンド | 動作 |
|---|---|
| `ytdlp status` | 導入状態（`ready` / `not_installed` / `broken`）、現在バージョン、`current` の向き先 |
| `ytdlp list` | 導入済みバージョン一覧。active に印を付ける |
| `ytdlp install [--version X]` | 導入（未指定なら最新）。既に導入済みなら何もしない（`--force` で再導入） |
| `ytdlp use <version>` | 切替。未導入バージョンは拒否 |
| `ytdlp remove <version>` | 削除。`current` は拒否 |
| `ytdlp exec -- <args...>` | 生実行。**予約引数を一切注入しない**。デバッグ用 |
| `ytdlp probe <URL>` | 受入試験用。`--simulate` でメタデータとフォーマット一覧を取得し整形表示 |
| `ytdlp fetch <URL> [--dest DIR]` | 受入試験用。Staging（既定）へ実ダウンロード。進捗を表示する |

## 9.1 `probe` / `fetch` の暫定性

これらは Phase 4 のオプション合成を経ないため、**暫定の固定オプション**を使う。

- Phase 4 で `core/options.py` に置き換える前提であることを、コード内コメントと `docs/基本設計.md` に明記する
- 予約引数（`--paths` / `--output` / `--print` / `--progress-template`）は、この暫定実装が直接指定する
- **Storage への publish は行わない**（Phase 5・7）。Staging に置いたまま終わる

---

# 10. ユニットテスト

## 10.1 必須項目

| 対象 | 内容 |
|---|---|
| 進捗パーサ | 正常系。不完全 JSON。プレフィックスなしの行。`NA` を含むフィールド。極端に長い行。**いずれも例外を投げないこと** |
| エラー分類 | 各ルールの一致。**未知のメッセージが `failed` に落ちること**。空の stderr |
| symlink 差し替え | `current` の向き先が変わること。差し替え中に `current` が消える瞬間がないこと |
| バージョン管理 | 世代削除で `current` と直前 active が残ること。`current` の削除が拒否されること |
| 状態判定 | `ready` / `not_installed` / `broken` の3状態が正しく判定されること |
| タイムアウト | 無進捗タイムアウトで kill されること。絶対上限で kill されること。**子プロセスも終了すること** |
| プロセスグループ | 子プロセスを起動する擬似スクリプトを使い、親の kill 後に子が残らないこと |
| ロック | 2つの並行インストール要求が直列化されること |
| マイグレーション | `upgrade` → `downgrade` → `upgrade` |

## 10.2 モック方針

- yt-dlp の実行を伴うテストは、**yt-dlp を模した擬似スクリプト**（進捗 JSON を吐く、子プロセスを起動する、指定秒数無反応になる、など）を使う
- 実際の yt-dlp とネットワークを使うテストは `@pytest.mark.network` を付け、既定でスキップする

---

# 11. 受入試験（実機・手動）

**Phase 3 は本節を通過して完了とする。** 各項目の実行結果を `docs/変更履歴.md` または AISTATE に記録すること。

## 11.1 試験素材について

**著作権上明確に問題のない素材を使うこと。** 推奨は以下。

- Blender Foundation のオープンムービー（Creative Commons）
- パブリックドメイン作品
- 自分自身がアップロードしたコンテンツ

**選定した URL を `docs/基本設計.md` に記録する。** Phase 15 のスモークテスト用 URL（`ytdlp.smoketest_url`）としてもそのまま使えるよう、長期的に安定したものを選ぶこと。

## 11.2 試験項目

| # | 手順 | 期待結果 |
|---|---|---|
| 1 | クリーンな状態から `docker compose up -d --build` | 3サービスが起動する。`/healthz` が 200 |
| 2 | `ytdlp status`（`ytdlp.auto_install=false` で起動） | `not_installed` と表示され、**app は正常に動作している**（degraded 起動） |
| 3 | worker のログを確認 | **待機ログのみ。再起動ループになっていない**（§0.3） |
| 4 | `ytdlp install` | 最新版が導入され、`status` が `ready` になる |
| 5 | `ytdlp install --version <1つ前>` → `ytdlp list` | 2件が一覧され、active に印が付く |
| 6 | `ytdlp use <旧version>` → `status` | 切替が反映される。`current` symlink の向き先が変わっている |
| 7 | `ytdlp use <最新>` に戻す | 同上 |
| 8 | `ytdlp probe <試験URL>` | フォーマット一覧とメタデータが取得できる |
| 9 | `ytdlp fetch <試験URL>` | Staging にファイルが生成される。**進捗が表示される** |
| 10 | worker のログを確認 | yt-dlp 導入後、待機ループを抜けている（または待機状態のまま安定している） |
| 11 | 存在しない動画 ID で `fetch` | `unavailable` に分類される |
| 12 | ネットワークを遮断して `fetch` | `blocked` に分類される。**`failed` ではないこと** |
| 13 | `ytdlp.idle_timeout_sec` を 5 に設定し、大きめのファイルを `fetch` | タイムアウトで終了する。`terminated_by` が `idle` |
| 14 | 13 の直後にコンテナ内で `ps` を確認 | **ffmpeg / yt-dlp の孤児プロセスが残っていない**（§3.5） |
| 15 | `ytdlp remove <current のバージョン>` | 拒否される |
| 16 | `ytdlp remove <古いバージョン>` | 削除され、`list` から消える |
| 17 | `current` symlink を手動で壊して `status` | `broken` と表示され、**自動修復を試みない** |
| 18 | `ytdlp install --force` | 復旧する |
| 19 | `make test` | 全テストがコンテナ内でパスする |
| 20 | 実行ログを確認 | **クレデンシャル・Cookie パスがマスクされている**（§3.6） |

## 11.3 判断が必要になった場合

試験中に想定外の挙動が出た場合、**その場で仕様を変えず、まず記録すること。** 要件定義 §1.4 の設計原則に照らして判断し、決まらなければ確認を取る（CLAUDE.md §8.4）。

---

# 12. コミット計画

| # | コミット |
|---|---|
| 1 | `chore: 開発依存をロックし make test をコンテナ実行に変更`（§0.1） |
| 2 | `docs: README のセットアップ手順を修正し make lock を運用コマンドに降格`（§0.2） |
| 3 | `fix: worker を待機ループ化して restart ループを解消`（§0.3） |
| 4 | `refactor: 内部設定キーを _internal 名前空間に分離`（§0.4） |
| 5 | `docs: 基本設計と AISTATE に前提・検討事項を追記`（§0.5, §0.6） |
| 6 | `feat: ytdlp_release テーブルとマイグレーションを追加` |
| 7 | `feat: yt-dlp venv 管理（インストール・切替・削除・状態判定）` |
| 8 | `feat: 出力プレフィックス規約と進捗パーサ` |
| 9 | `feat: yt-dlp CLI ラッパ（プロセスグループ制御とタイムアウト）` |
| 10 | `feat: エラー分類ルール` |
| 11 | `feat: CLI に ytdlp サブコマンド群を追加` |
| 12 | `feat: degraded 起動と worker の導入待機` |
| 13 | `test: Phase 3 のユニットテストを追加` |
| 14 | `docs: 受入試験の結果と設計判断を記録` |

完了後、**`checkpoint/step-03` タグを打つ**。

---

# 13. 完了条件

1. §0 の是正タスクがすべて完了している
2. `make test` がコンテナ内で全件パスする
3. `ruff check` / `mypy` がクリーン
4. `ytdlp status` が3状態（`ready` / `not_installed` / `broken`）を正しく報告する
5. yt-dlp 未導入でも `app` が起動し、`/healthz` が 200 を返す
6. worker が restart ループに入らず、待機状態を維持する
7. 2バージョンを導入し、`use` で切り替えられる
8. `current` symlink の差し替え中に、参照が失敗する瞬間がない
9. 実際に1本ダウンロードでき、Staging にファイルが生成される
10. 進捗が表示される
11. 無進捗タイムアウトで終了し、**ffmpeg の孤児プロセスが残らない**
12. `unavailable` / `blocked` / `failed` が期待通りに分類される
13. 未知のエラーが `failed` に落ちる
14. ログにクレデンシャルが平文で現れない
15. 受入試験（§11.2）の全20項目が通過し、結果が記録されている
16. `AISTATE.md` が更新され、Phase 4 の着手点が書かれている

---

# 14. ドキュメント更新義務

| ファイル | 内容 |
|---|---|
| `docs/基本設計.md` §2 | `downloader/*`、`db/repositories/ytdlp_release.py` の責務を更新 |
| `docs/基本設計.md` §3 | `ytdlp_release` テーブルの追加を記載 |
| `docs/基本設計.md` §4 | venv インストール・切替のフローを追記 |
| `docs/基本設計.md` §6 | yt-dlp との境界（プレフィックス規約、ロケール固定、プロセスグループ）を更新 |
| `docs/基本設計.md` §7 | D-011 以降として設計判断を追記。最低限：venv のバージョン管理方式、degraded 起動の採用、エラー分類の既定を `failed` にした理由、試験素材 URL の選定 |
| `docs/変更履歴.md` | 未リリース欄に追加項目と受入試験の結果 |
| `docs/footprint.md` | `/data/ytdlp/` 配下の構造を volume の内容に追記 |
| `README.md` | `make test` の追加、セットアップ手順の修正、`make` 無し環境向けの直コマンド併記 |
| `AISTATE.md` | 全文書き換え |

---

# 15. 実装時の注意（再掲）

- **プロセスグループで kill する。** 親だけ殺すと ffmpeg が孤児になる
- **ロケールを `LC_ALL=C` に固定する。** エラー分類が英語メッセージに依存している
- **stdout / stderr を並行に読む。** 片方だけだとデッドロックする
- **symlink は `os.replace` で差し替える。** remove → symlink にしない
- **未知のエラーは `failed`。** `unavailable` に倒さない
- **進捗を DB に書かない。** Phase 3 はパーサまで
- **オプション合成をここでやらない。** Phase 4 の責務
- **venv の書き込みは `app` のみ。** worker は読み取り専用
- 判断に迷ったら要件定義 §1.4 の設計原則に照らし、それでも決まらなければ**確認を取る**
