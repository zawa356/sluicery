# Phase 6 実装指示書 — Task キューとワーカー

| 項目 | 内容 |
|---|---|
| 対象 | 要件定義 §20 実装順序 #6 |
| 前提 | Phase 5 完了（Storage アダプタ、実機 SMB 検証済み） |
| 作成日 | 2026-08-10 |

本書は `docs/要件定義.md`（特に §8.2、§8.3、§10.3、N-2、N-5、N-7、N-8）と `CLAUDE.md` を補完する。**着手前に両方を読むこと。**

Phase 6 は「**タスクを誰が、いつ、どう実行するか**」を確定させるフェーズである。Phase 7 のパイプラインが乗る土台であり、**graceful shutdown と状態の一貫性が中心の論点**となる。

**保留中の未解決 #2 と #3 に、本フェーズで決着をつける。**

---

# 0. 着手前の是正・確認

## 0.1 `checkpoint/step-01` の欠落

タグ一覧が `checkpoint/step-02` から始まっており、Phase 1 のタグが存在しない。

Phase 1 完了コミットが `git log` から特定できるなら、遡って `checkpoint/step-01` を打つこと。特定できない場合は無理に打たず、**その旨を AISTATE に記録する**（以後、タグ間の差分参照をする際に混乱しないため）。

## 0.2 `*:Zone.Identifier` の掃除

作業ツリーに1件残っている。ignore 済みなので実害はないが、**監査のたびに「1件ある」と報告されると、本当の問題を見落とす原因になる。**

削除し、WSL 経由の作業で再生成された場合に掃除する手順を `CLAUDE.md` §5 か `docs/公開前チェックリスト.md` に一行加えること。

## 0.3 実装状況の確認

以下を確認し、結果を報告してから着手すること。

| # | 確認内容 | 影響 |
|---|---|---|
| 1 | `task` テーブルの現在のカラム構成（Phase 2 で定義したもの） | §2 のマイグレーション範囲 |
| 2 | `TaskRepository.claim_next()` の実装方式（`UPDATE ... RETURNING` か `BEGIN IMMEDIATE` か） | §4 の拡張方法 |
| 3 | Phase 2 で入れた「起動時に `running` のまま残った Task を `failed` に落とす」処理の実装箇所 | §6.4 と衝突しないか |
| 4 | 現在の worker の待機ループ実装（Phase 3 の degraded 起動対応） | §3 で置き換える範囲 |
| 5 | `compose.yaml` の `stop_grace_period` の指定有無 | §6.2 |

---

# 1. スコープ

## 1.1 含むもの

1. §0 の是正
2. `TaskStatus` への `blocked` 追加と `blocked_until` / `heartbeat_at` / `cancel_requested` の追加（§2）
3. `compose.yaml` への `init: true` 追加（§8）
4. ワーカーループ（§3）
5. claim の拡張（§4）
6. リトライとバックオフ（§5）
7. graceful shutdown（§6）
8. ハートビートと stale 回収（§7）
9. キャンセル（§9）
10. 進捗の DB 書き込みとスロットリング（§10）
11. ダミータスクタイプと検証用 CLI（§11）
12. ユニットテストと実機検証（§13、§14）
13. レビュー役による点検（§15）

## 1.2 含まないもの

- パイプライン（download → verify → publish → index）の実装（Phase 7）
- 実際の yt-dlp / rclone 実行タスク（Phase 7）。**Phase 6 はダミータスクで検証する**
- Discover の実処理（Phase 8）
- スケジューラによる自動投入（Phase 12）。Phase 6 は**手動投入のみ**
- Web UI（Phase 9 以降）
- ffmpeg SIGSEGV（未解決 #8）の調査。**Phase 7 で扱う**

---

# 2. データモデルの変更

## 2.1 `TaskStatus` に `blocked` を追加（未解決 #2 の決着）

**`TaskStatus` に `blocked` を追加する。**

判断の根拠：`target.status` と `task.status` は**寿命が違う**。Target は永続的な状態、Task は実行の単位である。「Storage が到達不能なので今は実行できない」を Task 側で表現できないと、失敗として `retry_count` を消費するか、キューに残したまま無限にリトライするかの二択になる。

要件定義 §7.2 の `target.status=blocked` と同じく、**`blocked` はリトライ回数を消費しない。**

`docs/基本設計.md` に D-029 以降として記録し、未解決 #2 を解消すること。

## 2.2 `task` テーブルへの追加カラム

| カラム | 型 | 用途 |
|---|---|---|
| `blocked_until` | datetime nullable | 再試行可能になる時刻。ワーカーはこれを過ぎるまで claim しない |
| `blocked_reason` | str nullable | 保留の理由（Phase 5 の分類を格納） |
| `heartbeat_at` | datetime nullable | 実行中ワーカーが定期更新する（§7） |
| `worker_id` | str nullable | 実行中のワーカー識別子（§7） |
| `cancel_requested` | bool | キャンセル要求フラグ（§9） |
| `available_at` | datetime | 実行可能になる時刻。リトライのバックオフに使う（§5） |

`available_at` と `blocked_until` を分ける理由：前者は**自分の失敗によるバックオフ**、後者は**外的要因による保留**。混ぜると「リトライ回数を消費しない」の判定ができなくなる。

**タイムスタンプは全て `UTCDateTime` 型を使うこと**（既存の前提）。

## 2.3 マイグレーション

- Alembic リビジョンを追加する
- **D-008 の既知の制限により、autogenerate 出力に CHECK 制約の偽陽性 diff が混ざる。実際の変更に無関係なものは手で取り除くこと**
- `TaskStatus` の CHECK 制約に `blocked` を追加する
- `upgrade` → `downgrade` → `upgrade` が通ることを確認する

## 2.4 インデックス

claim クエリの性能に直結する。Phase 2 で `task(status, worker_class, priority)` を張っているが、`available_at` / `blocked_until` の条件が加わるため見直すこと。

---

# 3. ワーカーループ

`src/sluicery/tasks/worker.py`。

## 3.1 構造

```
起動
  ↓
yt-dlp の ready を待つ（Phase 3 の待機ループを維持）
  ↓
ループ:
  1. シャットダウン要求を確認 → あれば §6 へ
  2. claim_next(worker_class) を試みる
  3. 取れなければスリープして 1 へ
  4. 取れたらハンドラを実行（§7 のハートビートを並行）
  5. 結果に応じて状態を更新
  6. 1 へ
```

## 3.2 ポーリング間隔

| 設定キー | 既定 | 用途 |
|---|---|---|
| `worker.poll_interval_sec` | 3 | タスクが無いときのスリープ |
| `worker.poll_jitter_sec` | 1 | 複数ワーカーの同時アクセスを散らす |

**ジッターを必ず入れること。** network / compute の2ワーカーが同期して DB を叩くと、WAL でも競合しやすくなる。

## 3.3 Phase 3 の待機ループとの関係

Phase 3 で入れた「yt-dlp が `ready` になるまで待つ」ループは維持する。**degraded 起動の挙動を壊さないこと。**

`ready` になった後、本ループに入る。`worker-compute` は Phase 7 まで compute クラスのタスクが発生しないため、**タスクが無い状態でポーリングを続けるのが正常**である。

## 3.4 ワーカーの識別子

`worker_id` は「サービス名 + コンテナ ID の一部 + プロセス ID」などで一意に構成する。ログにも出力し、どのワーカーが何を処理したか追えるようにすること。

---

# 4. claim の拡張

## 4.1 既存の `claim_next()`

Phase 2 でアトミックに実装済み。**振る舞いを壊さないこと。** 既存の並行テスト（二重 claim が起きないこと）が通り続けること。

## 4.2 追加する条件

```
status = 'pending'
AND worker_class = ?
AND (available_at IS NULL OR available_at <= now)
AND (blocked_until IS NULL OR blocked_until <= now)
AND (depends_on_task_id IS NULL OR 依存先が完了している)
ORDER BY priority DESC, scheduled_at ASC
```

**依存の判定を忘れないこと。** Phase 7 のパイプライン（download → verify → publish → index）はこれに依存する。

## 4.3 依存先が失敗した場合

依存先のタスクが `failed` / `unavailable` で終わった場合、後続タスクは**実行せずキャンセル扱いにする**（要件定義 §8.2「Task の失敗は後続 Task をキャンセルする」）。

この判定をどこで行うか（claim 時か、失敗時の後処理か）を決めて記録すること。**推奨は失敗時の後処理**。claim のクエリを複雑にしない。

## 4.4 claim 時の記録

claim と同時に `worker_id` / `started_at` / `heartbeat_at` を設定する。**別クエリにすると、claim 直後にワーカーが落ちた場合に持ち主不明のタスクができる。**

---

# 5. リトライとバックオフ

## 5.1 リトライの分類

| 結果 | 扱い |
|---|---|
| 成功 | `succeeded` |
| 一時的失敗（`failed`） | `attempts` を +1 し、上限未満なら `pending` に戻して `available_at` を設定 |
| 回復不能（`unavailable`） | リトライしない |
| **外的要因（`blocked`）** | **`attempts` を増やさず** `blocked_until` を設定 |
| キャンセル | `cancelled` |

## 5.2 バックオフ

指数バックオフ + ジッターとする。

| 設定キー | 既定 |
|---|---|
| `worker.retry_base_sec` | 60 |
| `worker.retry_max_sec` | 3600 |
| `worker.max_attempts` | 5 |

`blocked` の再試行間隔は別に持つ（外的要因の解消を待つので、より長くてよい）。

| 設定キー | 既定 |
|---|---|
| `worker.blocked_retry_sec` | 300 |

## 5.3 `blocked` の解消

`blocked_until` を過ぎたタスクは、claim クエリの条件（§4.2）で自然に拾われる。**別途「`pending` に戻す」バッチ処理は不要。**

ただし `status` は `blocked` のままなので、claim 条件に `status IN ('pending', 'blocked')` を含めるか、`blocked_until` 経過時に `pending` へ戻すかを決めること。**前者を推奨**（余計な更新クエリが要らない）。

---

# 6. graceful shutdown

## 6.1 方針

**SIGTERM を受けたら、実行中のタスクを `pending` に戻して速やかに終了する。**

完了を待たない。理由：ダウンロードは最長6時間かかりうるため、待つ設計は現実的でない。

## 6.2 手順

```
SIGTERM 受信
  ↓
1. 新規 claim を停止する
  ↓
2. 実行中の Runner に cancel を要求する（プロセスグループ終了）
  ↓
3. 実行中タスクを pending に戻す
   - attempts を増やさない（自分の失敗ではない）
   - available_at を即時（またはごく短い遅延）に設定
   - worker_id / started_at / heartbeat_at をクリア
  ↓
4. 終了
```

`compose.yaml` に `stop_grace_period` を明示すること（既定10秒では足りない可能性がある）。推奨は30秒程度。**プロセスグループ終了が完了するだけの余裕があればよい。**

## 6.3 Staging の中間ファイルは消さない（重要）

**タスクは `pending` に戻すが、Staging の中間ファイルは削除しないこと。**

yt-dlp は `--continue` により部分ファイルから再開できる。「タスクとしてはやり直し、ファイルとしては再開」という形にすれば、単純さと効率を両立できる。

中間ファイルを消すと、6時間かけたダウンロードが再起動のたびにゼロからになる。

## 6.4 Phase 2 の起動時処理との関係

Phase 2 で「起動時に `running` のまま残った Task を `failed` に落とす」処理を入れている。これは**異常終了時の保険**である。

正常な shutdown では §6.2 で `pending` に戻るため、この処理は発動しない。

**両者が衝突しないことを確認すること。** また、起動時処理の対象を「`heartbeat_at` が一定時間更新されていないもの」に限定できるなら、§7 の stale 回収と統合してよい（その場合は `failed` ではなく `pending` に戻す方が適切。§7.3 参照）。

---

# 7. ハートビートと stale 回収

## 7.1 目的

ワーカーが応答不能になった場合（OOM kill、コンテナの強制終了など）、タスクが `running` のまま永久に残る。

## 7.2 ハートビート

- 実行中のワーカーが `heartbeat_at` を定期更新する
- 間隔は `worker.heartbeat_interval_sec`（既定30秒）
- **更新はごく短いトランザクションで行う**（要件 N-7）
- ハンドラの実行をブロックしないこと（別スレッド、または進捗コールバックのタイミングで更新）

## 7.3 stale 回収

`heartbeat_at` が `worker.stale_threshold_sec`（既定180秒）以上更新されていない `running` タスクを回収する。

**回収は `failed` ではなく `pending` に戻す。** ワーカーが落ちたことはタスクの失敗ではない。

ただし**無限ループを防ぐため、`attempts` は +1 する。** 同じタスクが毎回ワーカーを落としている可能性があるため。

回収は `app` サービスが定期実行する（ワーカー同士が互いを回収すると複雑になる）。

## 7.4 誤回収の防止

`stale_threshold_sec` はハートビート間隔の**数倍**に設定すること。一時的な DB ロック競合で1回更新が飛んだだけで回収されると、二重実行が起きる。

---

# 8. `init: true`（未解決 #3 の決着）

**`compose.yaml` の全サービスに `init: true` を追加する。**

判断の根拠：Phase 6 でワーカーが実際に yt-dlp / rclone を起動し始めるため、zombie が現実の問題になる。`init: true` は Docker が tini 相当を PID 1 に置くだけで副作用がほぼなく、入れないコストの方が高い。

- `docs/footprint.md` に影響がないことを確認する（PID 1 が変わるが、ホスト上に作られるものは変わらない）
- `entrypoint.sh` の setpriv による権限降格と組み合わせて正しく動くことを**実機で確認する**
- 実機検証で、タイムアウトによるプロセスグループ終了後に **zombie が残らないこと**を `ps` で確認する（§14）

`docs/基本設計.md` に記録し、未解決 #3 を解消すること。

---

# 9. キャンセル

## 9.1 経路

```
CLI / UI が task.cancel_requested = true を設定
  ↓
ワーカーがポーリングで検知
  ↓
Runner.cancel() を呼ぶ（プロセスグループ終了、Phase 3/5 で実装済み）
  ↓
task.status = 'cancelled'
```

## 9.2 検知のタイミング

ハートビート更新と同じタイミングで確認するのが素直（既に DB にアクセスしているため）。

**検知の遅延は最大でハートビート間隔となる。** これを許容範囲とするか、別途短い間隔でポーリングするかを決めて記録すること。

## 9.3 Run 単位のキャンセル

要件定義 §10.3 は Run 単位のキャンセルも求めている。**Run に属する未実行タスクを `cancelled` にし、実行中のものに `cancel_requested` を立てる。**

Phase 6 では Run を生成する経路がまだない（Phase 8）ため、**インターフェースだけ用意して実処理は Phase 8 でよい。**

## 9.4 キャンセル後の Staging

**キャンセルでも Staging の中間ファイルを削除しない**（§6.3 と同じ方針、設計原則1）。

---

# 10. 進捗の DB 書き込み

## 10.1 背景

Phase 3 で「`progress.py` はパーサまで、DB 書き込みは Phase 6/7」と決めた。Phase 6 でその仕組みを実装する。

## 10.2 スロットリング（必須）

**進捗を毎行 DB に書くと SQLite が競合する**（要件 N-7）。

| 条件 | 書き込む |
|---|---|
| 前回書き込みから `worker.progress_write_interval_sec`（既定2秒）経過 | ○ |
| 進捗率が `worker.progress_write_percent_step`（既定5）% 以上進んだ | ○ |
| **最終状態（完了・失敗・キャンセル）** | **必ず書く** |
| 上記以外 | 書かない |

## 10.3 書き込み先

Phase 6 の時点では、進捗を保持するカラムが `task` に無い。以下のいずれかを選び、記録すること。

- `task.payload_json` の一部として保持する（マイグレーション不要）
- `task` に進捗用カラムを追加する

**推奨は前者**。Phase 11（進捗表示）で表示要件が固まってから、必要ならカラム化する。

## 10.4 トランザクションを短く保つ

進捗更新は頻度が高い。**単一 UPDATE で完結させ、他の処理と同一トランザクションにしないこと。**

---

# 11. ダミータスクと検証用 CLI

## 11.1 ダミータスクタイプ

Phase 7 のパイプラインが入るまで、実タスクが存在しない。**検証用のダミータスクタイプを用意する。**

| タイプ | 挙動 |
|---|---|
| `noop` | 即座に成功する |
| `sleep` | payload で指定された秒数スリープして成功する。進捗イベントを出す |
| `fail` | 必ず失敗する（`failed` 分類） |
| `fail_unavailable` | `unavailable` で失敗する |
| `fail_blocked` | `blocked` で失敗する |
| `spawn` | 子プロセスを起動してスリープする（プロセスグループ終了と zombie の検証用） |

**これらは検証専用であり、本番の実行経路に載らないようにすること。** 環境変数またはビルド設定で無効化できる形が望ましい。無効化の方法を記録すること。

## 11.2 検証用 CLI

```
sluicery task enqueue <type> [--worker-class network|compute] [--priority N] [--payload JSON]
sluicery task list [--status ...] [--worker-class ...]
sluicery task show <id>
sluicery task cancel <id>
sluicery task retry <id>          手動で pending に戻す
```

**Phase 7 のパイプラインが入るまでの暫定実装**であることを、コード内コメントと README に明記する。

---

# 12. 設定パラメータ

`CODE_DEFAULTS` に追加する。

| キー | 既定 |
|---|---|
| `worker.poll_interval_sec` | 3 |
| `worker.poll_jitter_sec` | 1 |
| `worker.heartbeat_interval_sec` | 30 |
| `worker.stale_threshold_sec` | 180 |
| `worker.retry_base_sec` | 60 |
| `worker.retry_max_sec` | 3600 |
| `worker.max_attempts` | 5 |
| `worker.blocked_retry_sec` | 300 |
| `worker.progress_write_interval_sec` | 2 |
| `worker.progress_write_percent_step` | 5 |
| `worker.shutdown_grace_sec` | 20 |

`worker.shutdown_grace_sec` は `compose.yaml` の `stop_grace_period` より**短く**設定すること。逆だと Docker に強制終了される。

---

# 13. ユニットテスト

| 対象 | 内容 |
|---|---|
| `claim_next()` の既存動作 | **Phase 2 の並行テストが通り続けること**（二重 claim なし） |
| claim の新条件 | `available_at` / `blocked_until` が未来のタスクを取らない。依存先未完了を取らない |
| 依存の失敗 | 依存先が `failed` のとき後続が `cancelled` になる |
| リトライ | `failed` で `attempts` +1、バックオフが指数的に伸びる |
| `blocked` | **`attempts` が増えないこと**。`blocked_until` 経過後に claim されること |
| 上限到達 | `max_attempts` で `unavailable` に落ちる |
| graceful shutdown | SIGTERM で実行中タスクが `pending` に戻る。`attempts` が増えない。`worker_id` がクリアされる |
| shutdown 後の再開 | 再起動後に同じタスクが claim される |
| ハートビート | 実行中に `heartbeat_at` が更新される |
| stale 回収 | 閾値超過で `pending` に戻る。`attempts` は +1 される |
| 誤回収 | 閾値未満では回収されないこと |
| キャンセル | `cancel_requested` の検知で Runner が停止し、`cancelled` になる |
| 進捗スロットリング | 間隔・刻み未満では書かれない。**最終状態は必ず書かれる** |
| マイグレーション | `upgrade` → `downgrade` → `upgrade` |
| ダミータスク | 各タイプが期待どおりの分類で終わる |

**時間に依存するテストは擬似クロックを使う**（Phase 5 の deadline テストと同じ方式）。実時間の sleep でテストを遅くしないこと。

---

# 14. 実機検証

## 14.1 実施環境

開発機で実施する。SMB は不要。

CLI でファイルを生成する操作には `--user "$(id -u):$(id -g)"` を付けること。

## 14.2 検証項目

| # | 手順 | 期待結果 |
|---|---|---|
| 1 | `task enqueue noop` | 即座に `succeeded` になる |
| 2 | `task enqueue sleep --payload '{"sec":30}'` → `task show` | `running` になり、`worker_id` と `heartbeat_at` が設定される |
| 3 | 2 の実行中に `task list` を連続実行 | `heartbeat_at` が更新され続ける |
| 4 | `task enqueue fail` | `attempts` が増え、`available_at` が未来に設定される |
| 5 | 4 を上限まで放置 | `max_attempts` 到達後に `unavailable` になる |
| 6 | `task enqueue fail_blocked` | **`attempts` が増えない。** `blocked_until` が設定される |
| 7 | 6 の `blocked_until` 経過後 | 自動的に再実行される |
| 8 | `task enqueue sleep`（長め）→ `task cancel` | Runner が停止し `cancelled` になる |
| 9 | 8 の直後に `ps` を確認 | **孤児プロセスが残っていない** |
| 10 | `task enqueue spawn` → `task cancel` → `ps` | **子プロセスも終了し、zombie も残っていない**（`init: true` の効果） |
| 11 | `task enqueue sleep`（長め）→ `docker compose stop worker-network` | タスクが `pending` に戻る。**`attempts` が増えていない** |
| 12 | 11 の後 `docker compose start worker-network` | 同じタスクが再度 claim され実行される |
| 13 | 11 の停止所要時間を計測 | `stop_grace_period` 内に収まり、SIGKILL されていない |
| 14 | `task enqueue sleep` 実行中に `docker kill worker-network`（SIGKILL） | `running` のまま残る |
| 15 | 14 の後、`stale_threshold_sec` 経過を待つ | **`pending` に戻り、`attempts` が +1 される** |
| 16 | network / compute の両方にタスクを投入 | それぞれのワーカーが自分のクラスのみ処理する |
| 17 | `sleep` タスクの進捗を `task show` で追跡 | スロットリングされた頻度で更新される |
| 18 | 進捗タスクの完了直後 | **最終状態が確実に記録されている** |
| 19 | 複数タスクを同時投入 | 二重実行されない |
| 20 | `make test` / `make lint` | 全件パス・クリーン |

**#10、#11、#15 が最重要。** それぞれ `init: true` の効果、graceful shutdown、stale 回収の検証にあたる。

## 14.3 検証後の後片付け

ダミータスクのレコードを削除し、Staging に残ったファイルがあれば掃除すること（**自動削除の仕組みは作らない**。手動で消す）。

---

# 15. レビュー

Phase 4・5 と同じ手順で、`.claude/agents/reviewer.md` のレビュー役による点検を行う。指摘は `docs/reviews/phase6.md` に記録する。

**本フェーズで特に見てほしい観点**をレビュー役への指示に含めること。

- graceful shutdown で状態が失われないか（`pending` に戻す処理の抜け）
- **Staging の中間ファイルが意図せず削除されていないか**（設計原則1）
- `blocked` が `attempts` を消費していないか
- 進捗更新のトランザクションが長くなっていないか
- 既存の `claim_next()` のアトミック性が壊れていないか
- ダミータスクが本番経路に載る可能性がないか
- stale 回収と Phase 2 の起動時処理が衝突していないか

---

# 16. コミット計画

| # | コミット |
|---|---|
| 1 | `chore: checkpoint/step-01 を付与し Zone.Identifier を掃除`（§0.1, §0.2） |
| 2 | `feat: TaskStatus に blocked を追加し task へ制御カラムを追加`（§2） |
| 3 | `chore: compose に init: true と stop_grace_period を追加`（§8, §6.2） |
| 4 | `feat: claim の条件を拡張（available_at / blocked_until / 依存）`（§4） |
| 5 | `feat: ワーカーループとポーリング`（§3） |
| 6 | `feat: リトライとバックオフ`（§5） |
| 7 | `feat: graceful shutdown（実行中タスクを pending へ戻す）`（§6） |
| 8 | `feat: ハートビートと stale 回収`（§7） |
| 9 | `feat: タスクのキャンセル`（§9） |
| 10 | `feat: 進捗の DB 書き込みとスロットリング`（§10） |
| 11 | `feat: 検証用ダミータスクと task CLI`（§11） |
| 12 | `test: Phase 6 のユニットテストを追加`（§13） |
| 13 | `docs: 実機検証の結果と設計判断を記録`（§14） |
| 14 | `docs: レビュー指摘への対応`（§15） |

完了後、`checkpoint/step-06` タグを打つ。push は監査を通して承認を得てから。

---

# 17. 完了条件

1. §0 の是正が完了している
2. `TaskStatus` に `blocked` が追加され、未解決 #2 が解消・記録されている
3. `compose.yaml` に `init: true` が追加され、未解決 #3 が解消・記録されている
4. マイグレーションの `upgrade` → `downgrade` → `upgrade` が通る
5. **Phase 2 の `claim_next()` 並行テストが通り続ける**
6. claim が `available_at` / `blocked_until` / 依存を正しく考慮する
7. 依存先の失敗で後続がキャンセルされる
8. `failed` でバックオフが指数的に伸びる
9. **`blocked` が `attempts` を消費しない**
10. `max_attempts` 到達で `unavailable` になる
11. **SIGTERM で実行中タスクが `pending` に戻り、`attempts` が増えない**
12. **graceful shutdown で Staging の中間ファイルが削除されない**
13. 再起動後に同じタスクが再実行される
14. ハートビートが更新され、stale なタスクが回収される（`attempts` +1）
15. 閾値未満で誤回収されない
16. キャンセルで Runner が停止し、孤児プロセスが残らない
17. **`spawn` タスクのキャンセル後に zombie が残らない**（`init: true` の効果）
18. 進捗がスロットリングされ、**最終状態は必ず記録される**
19. network / compute が自分のクラスのみ処理する
20. ダミータスクが本番経路に載らない仕組みになっている
21. 実機検証（§14.2）の全20項目が通過し、結果が記録されている
22. `make test` / `make lint` がクリーン
23. レビューが実施され、指摘が `docs/reviews/phase6.md` に記録されている
24. `AISTATE.md` が更新され、Phase 7 の着手点が書かれている

---

# 18. ドキュメント更新義務

| ファイル | 内容 |
|---|---|
| `docs/基本設計.md` §2 | `tasks/*` の責務を更新 |
| `docs/基本設計.md` §3 | `task` テーブルへの追加カラム、`TaskStatus` の値を差分表に反映 |
| `docs/基本設計.md` §4 | ワーカーループ、claim、shutdown、stale 回収のフローを追記 |
| `docs/基本設計.md` §7 | D-029 以降として記録。最低限：`TaskStatus` に `blocked` を追加した判断（未解決 #2）、`init: true` の採用（未解決 #3）、shutdown で `pending` に戻す判断、Staging を消さない判断、`available_at` と `blocked_until` を分けた理由、進捗の保存先 |
| `docs/footprint.md` | `init: true` による影響（無いことの確認結果） |
| `docs/変更履歴.md` | 未リリース欄に追加項目と実機検証結果 |
| `docs/reviews/phase6.md` | 新設 |
| `README.md` | `task` CLI を暫定実装として追記 |
| `AISTATE.md` | 全文書き換え。未解決 #2・#3 を解消済みとして削除 |

---

# 19. 実装時の注意（再掲）

- **`claim_next()` のアトミック性を壊さない。** 既存テストが落ちたら実装を疑う
- **`blocked` は `attempts` を消費しない**
- **shutdown で Staging を消さない。** `--continue` で再開できる
- **shutdown で `attempts` を増やさない。** 自分の失敗ではない
- **stale 回収では `attempts` を増やす。** 無限ループ防止
- **進捗の最終状態は必ず書く**
- **進捗更新のトランザクションを短く保つ**
- **ダミータスクを本番経路に載せない**
- **`worker.shutdown_grace_sec` < `stop_grace_period`**
- **`stale_threshold_sec` はハートビート間隔の数倍に**
- ffmpeg SIGSEGV（未解決 #8）は **Phase 7 で扱う。** 本フェーズでは触れない
- 判断に迷ったら要件定義 §1.4 の設計原則に照らし、それでも決まらなければ**確認を取る**
