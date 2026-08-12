# Phase 7-8 統合指示書 — パイプラインと二相同期

| 項目 | 内容 |
|---|---|
| 対象 | 要件定義 §20 実装順序 #7 と #8 |
| 前提 | Phase 6 完了（Task キュー、ワーカー、所有権付き状態遷移） |
| 作成日 | 2026-08-12 |
| **実行形態** | **夜間の連続自律実行を想定。§0 の停止条件を厳守すること** |

本書は `docs/要件定義.md`（特に §6.2、§8、§11、§14.3）と `CLAUDE.md` を補完する。**着手前に両方を読むこと。**

Phase 7-8 は、これまで作った部品（オプション合成、Storage、Task キュー）が初めて全て繋がるフェーズである。**到達点は「登録したプレイリストが、差分だけ自動で最終保存先に落ちてくる」状態。**

分量が大きいため、**Part A（Phase 7 相当）と Part B（Phase 8 相当）に分け、それぞれの完了時にレビューを行う。**

---

# 0. 自律実行のルール

## 0.1 止まらずに進めてよいこと

以下は判断を仰がず、本書の方針に従って進めること。

- 実装、テスト、リファクタ、コミット
- 実機検証の実施と結果の記録
- 本書に「〜すること」と明記された判断
- 実機確認の結果が想定と異なった場合の**記録**（判断は §0.2 の対象になりうる）
- レビューでの**軽微・中**の指摘への対応
- ffmpeg 静的ビルドに問題があった場合の**差し替え**（§2）

## 0.2 止まって朝の判断を待つこと

以下に該当したら、**それ以上進めず、状況を整理して報告し、待機する。**

1. 要件定義との矛盾が生じ、**要件定義側の変更が必要**になったとき
2. **設計原則1（ローカルのデータを失わない）** に関わる判断が必要になったとき
3. レビューで**重大**の指摘が出て、**対応方針が複数あり得る**とき
   （対応方針が一意に定まる場合は対応してよい）
4. **機密・実環境情報の混入**を検出したとき
5. **push が必要になったとき**（常に承認待ち。夜間に push しない）
6. 本書の指示同士が矛盾していると判断したとき

止まる場合、**そこまでの作業を `wip:` を付けてコミットし、AISTATE に状況を書いてから**待機すること。朝に再開できる状態を残す。

## 0.3 進行順序

```
§1 準備
  ↓
Part A（§2〜§9）実装・検証
  ↓
レビュー①（§10）→ 対応
  ↓
Part B（§11〜§17）実装・検証
  ↓
レビュー②（§18）→ 対応
  ↓
最終確認（§20）
```

**Part A のレビュー①で重大指摘が出た場合、対応してから Part B に進むこと。** Phase 6 で「実処理を載せる前に競合を潰せた」経験を踏まえる。

## 0.4 コミット粒度（Phase 6 の申し送り）

Phase 6 のレビューで「コミット粒度が計画より粗い」と指摘されている。本フェーズでは改善すること。

- **§19 のコミット計画に従う。** まとめない
- **対応するテストと文書更新を、実装と同じコミットに含める**
- 実装だけ先に積んで、テストと文書を後からまとめて1コミット、という形にしない

---

# 1. 準備

## 1.1 実装状況の確認

以下を確認し、結果を記録してから着手する（報告して止まる必要はない）。

| # | 確認内容 |
|---|---|
| 1 | `task` のハンドラ登録機構（Phase 6 のダミータスクがどう登録されているか） |
| 2 | `artifact` テーブルの現在のカラム構成 |
| 3 | `run` テーブルの現在のカラム構成 |
| 4 | `StorageAdapter.publish()` のシグネチャと戻り値 |
| 5 | `build_download_args()` / `build_discover_args()` の戻り値（`BuiltCommand`）の構造 |
| 6 | `target.status` / `item.membership` の Enum 定義 |

## 1.2 検証用 URL の管理（重要）

Part B の実機検証で、実運用に近いプレイリストを使う。

**実際のプレイリスト URL は視聴履歴に相当する個人情報である。以下を厳守すること。**

- URL は `.local/test_playlists.txt`（新設。**`.gitignore` に追加する**）に置く。ユーザーが記入する
- **ドキュメント・コミットメッセージ・レビュー記録に URL を書かない**
- 検証記録では `<PLAYLIST_A>` / `<PLAYLIST_B>` のプレースホルダを使う
- DB に入った URL はコミット対象外（`data/` は ignore 済み）
- 検証後、`docs/公開前チェックリスト.md` の監査を実行する

`.local/test_playlists.txt` が存在しない場合、Part B の実機検証のうち実プレイリストを要する項目はスキップし、**その旨を記録して他を進めること**（止まらない）。

Creative Commons の公開素材（D-022 の Blender Open Movies）は引き続き使用してよく、これはドキュメントに書いてよい。

## 1.3 既存の Staging ファイル

`/data/staging/trailer_1080p.mov` が Phase 3 から残っている。**削除しないこと。**

§8 の孤立ファイル検出を実装した際、**最初の検出対象になる**。想定どおり「検出して報告し、自動削除しない」挙動になるかの良い材料である。

---

# Part A — パイプライン

# 2. ffmpeg 静的ビルドの再評価（未解決 #8）

## 2.1 背景

`--download-sections` の試験で ffmpeg が `-11`（SIGSEGV）で落ちた。機能非対応ならエラー終了するはずであり、**ビルド自体の健全性に疑いがある。**

Phase 7 の verify で ffprobe を使うため、ここで決着させる。

## 2.2 調査手順

1. `ffmpeg -version` / `ffprobe -version` が正常に動くことを確認
2. **ffprobe で既存ファイルのメタデータを取得できることを確認**（verify の中核機能）
3. `--download-sections` の SIGSEGV を再現させ、どの操作で落ちるか特定する
4. 落ちる操作が verify で使う機能と重なるかを判定する

## 2.3 判定と対処

| 状況 | 対処 |
|---|---|
| ffprobe が正常に動き、SIGSEGV は `--download-sections` 特有 | **差し替え不要。** verify を続行し、未解決 #8 を「区間取得の既知の制限」として残す |
| ffprobe も不安定、または広範に SIGSEGV が出る | **差し替える**（§2.4） |

## 2.4 差し替える場合

**自律的に実施してよい。** ただし以下を守ること。

- 差し替え先は**checksum 検証可能なもの**に限る（D-002 の方針を維持）
- Dockerfile の `FFMPEG_URL` / `FFMPEG_SHA256` build-arg を使う（既存の仕組み）
- **差し替え後、Phase 3・5 の実機検証で確認済みの挙動が壊れていないことを確認する**（yt-dlp の fetch、メタデータ・サムネイル埋め込み）
- 差し替えの理由・選定根拠・検証結果を `docs/基本設計.md` に D-036 以降として記録する
- イメージサイズが大きく変わる場合、README の実測値を更新する

**候補が見つからない、または差し替えても解決しない場合は §0.2 に該当しないので、状況を記録して verify を「ffprobe が動く範囲で」実装し、未解決事項として残すこと。** 止まらない。

---

# 3. パイプラインの構造

## 3.1 タスクチェーン

```
download → verify → postprocess → publish → index
```

Phase 6 の `depends_on_task_id` で直列に繋ぐ。

| タスク | worker_class | 内容 |
|---|---|---|
| `download` | network | yt-dlp で Staging に取得 |
| `verify` | compute | ffprobe で健全性確認、メタデータ取得 |
| `postprocess` | compute | **現バージョンは素通し**（§7） |
| `publish` | network | Storage アダプタで最終保存先へ |
| `index` | network | `artifact` レコードを確定、フック発火 |

## 3.2 チェーンの生成

**Target 1件に対してチェーン全体を一度に投入する。** 前段の完了を待ってから次を投入する方式にしない。

理由：Phase 6 の `depends_on_task_id` が既に依存を扱えるため、逐次投入は二重管理になる。また依存失敗の伝播（D-034）も既に実装済みで、そこに乗せられる。

## 3.3 タスク間のデータ受け渡し

前段の結果を後段に渡す必要がある（download が生成したファイルパスを verify が使う、など）。

**方式：`task.payload_json` に書き、後続タスクが依存先の payload を読む。**

- 中間状態を DB 以外に持たない（ワーカーが別コンテナなので、メモリ共有できない）
- 依存先タスクの ID は `depends_on_task_id` で辿れる
- 受け渡す内容は最小限にする（ファイルパス、サイズ、フォーマット情報など）

**payload に秘密情報を入れないこと。** DB には残るが、ログ出力やエラー表示に載る可能性がある。

## 3.4 作業ディレクトリ

Phase 4 で決めた構造を使う。

```
<STAGING_DIR>/<work-id>/<subpath>/<filename>
```

`work-id` は Target 単位で一意にする（チェーン全体で共有）。**チェーンの全タスクが同じ `work-id` を参照する。**

---

# 4. download タスク

## 4.1 処理

1. Target から Playlist / Profile を辿る
2. `build_download_args()` でコマンドを組み立てる（Phase 4）
3. `YtdlpRunner` で実行（Phase 3）
4. 進捗をスロットリングして DB に書く（Phase 6）
5. `--print after_move:` で実ファイルパスを取得
6. payload に結果を記録

## 4.2 状態の写像

**`task.status` と `target.status` は別物である。** ここを混同しないこと。

| yt-dlp の分類（Phase 3） | task.status | target.status |
|---|---|---|
| `ok` | `succeeded` | （チェーン完了まで `downloading` / `processing`） |
| `failed` | `failed`（リトライ） | `failed` |
| `unavailable` | **`unavailable`**（リトライしない） | `unavailable` |
| `blocked` | `blocked`（attempts 不変） | `blocked` |

**`task.status=unavailable` は「タスクがリトライ上限に達した／回復不能」、`target.status=unavailable` は「コンテンツが回復不能」を意味する。同名だが意味が違う。**

この対応表を `docs/基本設計.md` に明記すること（Phase 8 の実装者への申し送りでもある）。

## 4.3 失敗時の Staging

**いかなる失敗でも Staging の中間ファイルを削除しない**（設計原則1、D-031）。

yt-dlp の `--continue` により、リトライ時に部分ファイルから再開できる。

## 4.4 再取得（overwrite）

同じ Target を再取得する場合（`missing` からの復帰など）、Staging に前回の完成ファイルが残っている可能性がある。

**方針：`work-id` が同じなら再利用を試み、`--continue` に任せる。** 完成済みファイルがあれば yt-dlp がスキップする。

---

# 5. verify タスク

## 5.1 目的

**ダウンロードが破損していないことの確認と、`artifact` に記録するメタデータの取得。**

## 5.2 検証内容

| 項目 | 方法 |
|---|---|
| ファイルが存在する | パス確認 |
| サイズが 0 でない | stat |
| **メディアとして読める** | `ffprobe -v error -show_format -show_streams -of json` |
| 想定した長さがある | ffprobe の `duration`。Item のメタデータと突き合わせ可能なら比較 |
| コーデック・コンテナ | ffprobe の出力を `artifact` 用に取得 |

**ffprobe が非ゼロ終了、または JSON をパースできない場合は破損とみなす。**

## 5.3 長さの突合について

ffprobe の `duration` と、discover で得た `duration` を比較して大きく乖離していたら異常、という判定は魅力的だが、**Phase 7 では実装しない。**

理由：generic extractor では `duration` が取れない（Phase 4 の知見）。ライブ配信や可変長コンテンツもある。誤検知でファイルを破損扱いにするリスクの方が高い。

**ffprobe から得た `duration` は記録するに留め、判定には使わない。** 判定に使うかは Phase 13（整合性チェック）で再検討する旨を記録すること。

## 5.4 失敗時

- Staging のファイルは**削除しない**
- `task.status=failed`（リトライ対象）
- リトライしても同じなら上限で `unavailable` に落ちる
- **破損の理由（ffprobe の stderr）を payload とログに残す**

## 5.5 ffprobe の実行

- `BaseRunner`（Phase 5）を使う。プロセスグループ終了とタイムアウトの恩恵を受ける
- タイムアウトは短くてよい（`pipeline.verify_timeout_sec`、既定 60）
- **大きなファイルでも ffprobe はメタデータだけ読むので速い。** 遅い場合は指定が間違っている

---

# 6. publish タスク

## 6.1 処理

1. verify の payload から Staging のファイルパスを取得
2. `playlist_profile` から Storage と subpath を解決
3. Storage の到達性と空き容量を確認
4. `StorageAdapter.publish()` を呼ぶ（Phase 5、一時名 → 検証 → rename）
5. 結果を payload に記録

## 6.2 Storage が使えない場合

Phase 5 の分類を `task.status` に写像する。

| Storage 分類 | task.status | 備考 |
|---|---|---|
| `ok` | `succeeded` | |
| `failed` | `failed` | リトライ |
| `unreachable` | **`blocked`** | **attempts 不変** |
| `no_space` | **`blocked`** | **attempts 不変** |
| `auth_failed` | `failed` | 設定ミスなのでリトライしても直らないが、上限で `unavailable` に落ちる。**ユーザーが気づけるようログとエラーメッセージを明確にする** |
| `permission_denied` | `failed` | 同上 |

`auth_failed` / `permission_denied` を `blocked` にしないのは、**永久に再試行し続けてしまう**ため。上限に達して `unavailable` になり、ユーザーの目に触れる方が良い。

この判断を記録すること。

## 6.3 空き容量

`free_space()` が `None` を返す場合、**容量チェックをスキップして続行する**（D-027）。

閾値を下回っている場合、`blocked` にして `blocked_until` を設定する。

## 6.4 publish 成功後の Staging

**成功したら Staging の該当ファイルを削除してよい。**

これは設計原則1に反しない。最終保存先に確実に配置された後の中間ファイルであり、`artifact` レコードで追跡されている。

ただし：

- **publish の検証（一時名 → 検証 → rename）が完了してから削除する**
- 削除は publish タスクではなく **index タスクの後**に行う（`artifact` レコード確定後）
- 削除に失敗しても、それを理由にタスクを失敗させない（ログに残す）

**`work-id` ディレクトリ全体を削除するのではなく、publish したファイルのみを削除すること。** 同じ Target に複数ファイルがある場合（サムネイル、字幕の別ファイルなど）を考慮する。

---

# 7. postprocess タスク（空実装）

## 7.1 スコープ

**インターフェースと素通しハンドラまでを実装する。** 実際の変換処理は実装しない（要件定義 §14.3）。

## 7.2 実装内容

- 要件定義 §14.3 の `PostProcessor` インターフェースを定義する
- `profile.postprocess_chain_json` を読む（現バージョンは常に空）
- チェーンが空なら**入力をそのまま出力として次段に渡す**
- worker_class は `compute`

## 7.3 空でもタスクを作るか

**作る。** チェーンが空でも `postprocess` タスクを生成し、素通しさせる。

理由：将来チェーンが入ったときに、タスク生成ロジックを変えずに済む。空チェーンの実行コストはほぼゼロ。

## 7.4 将来の拡張点

- 出力は `role=derived` の `artifact` になる（現バージョンでは発生しない）
- 元ファイルを保持するか置換するかを、後処理定義ごとに指定できる構造にしておく
- **実装しないが、インターフェースがこれを表現できることを確認する**

---

# 8. index タスクと artifact

## 8.1 処理

1. publish の payload から最終保存先のパスを取得
2. **`artifact` レコードを作成する**
3. `target.status = downloaded` に更新する
4. Staging の該当ファイルを削除する（§6.4）
5. フックを発火する（§8.4）

## 8.2 artifact レコードの確定タイミング

**publish が成功し、最終保存先にファイルが存在することを確認した後（= index タスク）に作成する。**

publish タスク内で作らない理由：publish 後 index 前に落ちた場合、レコードだけあってファイルが無い、あるいはその逆、という不整合が起きうる。**index を独立させ、そこで確定させる**ことで、`artifact` の存在＝publish 完了の保証になる。

記録する内容（Phase 2 のスキーマに従う）：

- `role=source`（現バージョンでは常に）
- `storage_id`、`relative_path`
- `container` / `format_id` / `video_codec` / `audio_codec`（verify の ffprobe 結果から）
- `filesize`、`duration`
- `produced_by_task_id`
- `verified_at`

`checksum` は現バージョンでは**計算しない**（大容量ファイルで高コスト。Phase 13 で必要になったら再検討する旨を記録）。

## 8.3 target.status の更新

`downloaded` に更新する。**これがチェーン全体の完了を意味する。**

チェーンの途中で失敗した場合、`target.status` は失敗した段階で更新される（D-034 の依存失敗伝播により後続はキャンセルされる）。

## 8.4 フック発火

要件定義 §14.1 のイベントを発火する。Phase 18 でフック機構を作る予定だが、**`index` タスクから発火する箇所だけ用意しておく。**

現時点では `event_log` テーブルへの記録のみ。発火するイベント：`target_downloaded`、`artifact_published`。

**フックの失敗が本体処理に影響しないこと**（要件定義 §14.1）。

## 8.5 Staging の孤立ファイル検出

要件定義 §6.3 に従い、**対応する Task が存在しない Staging のファイルを検出する仕組み**を実装する。

- **自動削除しない。** 検出して一覧を返すのみ
- CLI から実行できるようにする（`sluicery staging orphans`）
- `/data/staging/trailer_1080p.mov`（Phase 3 由来）が検出されることを確認する（§1.3）

---

# 9. Part A の検証

## 9.1 ユニットテスト

| 対象 | 内容 |
|---|---|
| チェーン生成 | 5タスクが正しい依存関係で生成される |
| payload 受け渡し | 後段が前段の結果を読める |
| 状態の写像 | yt-dlp / Storage の各分類が正しい `task.status` になる |
| **`blocked` の attempts 不変** | `unreachable` / `no_space` で attempts が増えない |
| `auth_failed` | `blocked` ではなく `failed` になる |
| verify | ffprobe 失敗で破損と判定される。JSON パース失敗も同様 |
| verify の duration | 記録されるが判定に使われない |
| postprocess | 空チェーンが素通しする |
| artifact | **index タスクでのみ作成される** |
| Staging 削除 | **index 後にのみ削除される。失敗時は削除されない** |
| 孤立ファイル検出 | 検出はするが削除しない |
| 依存失敗の伝播 | download 失敗で後続4タスクがキャンセルされる |

## 9.2 実機検証（Part A）

D-015 の Blender 直リンクと D-022 の Open Movies を使う。**この段階では実プレイリストを使わない。**

| # | 手順 | 期待結果 |
|---|---|---|
| 1 | ffmpeg / ffprobe の動作確認（§2） | 判定結果が記録される |
| 2 | Target を1件作り、チェーンを投入 | 5タスクが生成され、順に実行される |
| 3 | 完了後の最終保存先を確認 | ファイルが配置されている |
| 4 | `artifact` レコードを確認 | codec / duration / filesize が記録されている |
| 5 | Staging を確認 | publish したファイルが削除されている |
| 6 | `target.status` | `downloaded` |
| 7 | Storage を到達不能にして publish | **`blocked` になり attempts が増えない** |
| 8 | 7 の復旧後 | 自動的に再開し、完了する |
| 9 | 存在しない URL で download | `unavailable` になり、後続がキャンセルされる |
| 10 | download 中に worker を停止 | Staging の中間ファイルが残る |
| 11 | 10 の再開後 | `--continue` で途中から再開する |
| 12 | verify を強制的に失敗させる（破損ファイルを置く） | 破損と判定され、**Staging が削除されない** |
| 13 | `staging orphans` | `trailer_1080p.mov` が検出される。**削除されない** |
| 14 | 音楽プロファイルで実行 | opus が生成され、タグ・サムネイルが埋め込まれる |
| 15 | 1 URL に2プロファイル | 2つの `artifact` が別々の Storage / subpath に作られる |
| 16 | SMB Storage への publish | 成功する |
| 17 | `make test` / `make lint` | クリーン |

---

# 10. レビュー①

`.claude/agents/reviewer.md` のレビュー役による点検を行う。記録は `docs/reviews/phase7.md`。

**特に見てほしい観点**をレビュー役への指示に含めること。

- Staging の削除が index 後に限られているか。失敗時に削除される経路がないか
- `blocked` が attempts を消費していないか
- `artifact` が publish 前に作られる経路がないか
- Phase 6 の所有権付き状態遷移が壊れていないか
- チェーンの途中失敗で `target.status` が正しく更新されるか
- ffmpeg 差し替えを行った場合、Phase 3・5 の検証済み挙動が維持されているか
- payload に秘密情報が入っていないか

**重大指摘が出た場合、§0.2 の3に該当するかを判断する。** 対応方針が一意なら対応して進む。複数あり得るなら止まって報告する。

---

# Part B — 二相同期

# 11. discover タスク

## 11.1 処理

1. `build_discover_args()` でコマンドを組み立てる（Phase 4）
2. `--flat-playlist` で実行
3. エントリの一覧を取得
4. **取得結果が空またはエラーなら、以降の処理を中止する**（§11.3）
5. Item を upsert し、`last_seen_at` を更新
6. 新規 Item に対して Target を作成（有効な `playlist_profile` ごと）
7. 今回現れなかった `active` な Item を `delisted` に遷移
8. Run の統計を記録

**discover はファイルを一切操作しない。**

## 11.2 Item の upsert

`UNIQUE(playlist_id, source_id)` に対する upsert。

記録する内容：`title` / `uploader` / `duration` / `upload_date` / `playlist_index` / `metadata_json`。

**generic extractor では `uploader` / `duration` / `upload_date` が欠損する**（Phase 4 の知見）。欠損を許容し、`NA` を入れないこと（D-019）。

既存 Item のメタデータが変わっていた場合（タイトル変更など）、**更新する。** ただし `first_seen_at` は保持する。

## 11.3 空振り判定（重要）

要件定義 §8.1 は「取得結果が空またはエラーの場合、以降の処理を中止する」と定めている。誤った `delisted` 判定を防ぐためである。

**ただし、プレイリストが本当に空になった場合と区別できない。**

**方針：区別しない。空なら常にスキップする。**

- 空の結果で `delisted` にするリスク（大量の誤判定）の方が、空を検出できないリスクより大きい
- ローカルファイルは削除されないので、誤判定しても実害は「レポートに出ない」だけ
- **空だった旨を Run の統計とログに記録する**。ユーザーが気づける状態にする

この判断を `docs/基本設計.md` に記録すること。

## 11.4 delisted への遷移

- `membership = delisted`、`delisted_at` を設定
- **Artifact に一切影響しない。ファイルを削除しない**（設計原則1）
- 対応する Target の `status` も変更しない

## 11.5 再登場

`delisted` な Item が再びプレイリストに現れたら `active` に戻す。`delisted_at` はクリアする。

**未取得（`pending`）のままだった Target があれば、そのまま取得対象に戻る。**

## 11.6 タイムアウト

`ytdlp.discover_timeout_sec`（既定300秒、Phase 3）を適用する。

**大きなプレイリストで足りない可能性がある。** 実機検証で確認し、必要なら既定値を調整して記録すること（自律判断でよい）。

---

# 12. download フェーズの起動

## 12.1 対象の選択

```
target.status = 'pending'
  OR (target.status = 'failed' AND retry_count < 上限)
```

`unavailable` / `ignored` / `downloaded` は対象外。`blocked` は原因解消後に `pending` に戻る。

## 12.2 順序

`item.playlist_index` の昇順を基本とする。プレイリストの並び順で取得される方が自然。

`task.priority` は Phase 12（スケジューラ）で使う想定なので、Phase 8 では既定値でよい。

## 12.3 Storage の事前確認

チェーンを投入する前に、対象 Storage の到達性と空き容量を確認する。

問題があれば**チェーンを投入せず**、Target を `blocked` にする。無駄なタスクを大量生成しない。

## 12.4 投入量の制御

大きなプレイリストで数千の Target がある場合、一度に全チェーンを投入すると `task` テーブルが膨れる。

**方針：1回の download フェーズで投入する Target 数に上限を設ける。**

| 設定キー | 既定 |
|---|---|
| `sync.max_targets_per_run` | 50 |

上限に達したら、残りは次回の実行で処理する。**Run の統計に「残り件数」を記録する。**

---

# 13. Run

## 13.1 生成

`sluicery sync` の実行、または Phase 12 のスケジューラから生成される。Phase 8 では**手動実行のみ。**

`run.kind` は `discover` / `download` を分ける（要件定義 §10.2 の分離スケジュールに対応するため）。

## 13.2 統計

`stats_json` に記録する内容：

```
new_items          今回新規に検出された Item 数
delisted_items     今回 delisted になった Item 数
targets_queued     チェーンを投入した Target 数
targets_remaining  上限により見送った Target 数
downloaded         完了した Target 数
failed             失敗した Target 数
blocked            保留された Target 数
empty_result       discover が空だったか（§11.3）
```

## 13.3 Run の完了判定

**discover は同期的に完了する**（タスクが1つなので）。

**download は非同期である。** チェーンを投入した時点で Run を「完了」とするか、全チェーンの終了を待つかを決める必要がある。

**方針：投入完了時点で Run を `succeeded` とし、個々の Target の結果は Target 側で追跡する。**

理由：全チェーンの完了を待つと、数時間かかる Run が生まれ、キャンセルや進捗表示が複雑になる。Run は「投入の記録」と位置づける。

この判断を記録すること。Phase 11（Run 履歴の UI）で表示方法を検討する際の前提になる。

## 13.4 失敗の扱い

要件定義 N-2：「1 Playlist 内の全件失敗は Run を `failed` とする」。

download の Run は投入時点で完了するため、この判定は**投入時に Storage が使えない等でチェーンを1件も投入できなかった場合**に適用する。

---

# 14. sync CLI

## 14.1 コマンド

```
sluicery sync discover [--playlist <name|id>] [--all]
sluicery sync download [--playlist <name|id>] [--all]
sluicery sync run      [--playlist <name|id>] [--all]     discover → download を続けて実行
```

`make sync` を実装済みにする（README の実装状況表を更新）。

## 14.2 出力

- 実行した Run の ID
- 統計（§13.2）
- **空振りだった場合は明示する**

## 14.3 ドライラン

`--dry-run` を用意する。discover を実行して**増減を表示するが、DB を更新しない。**

これは要件定義 §13.2 の「差分レポート」の CLI 版として機能する。

---

# 15. 状態遷移の実装

## 15.1 一覧

要件定義 §7.2 の遷移を実装する。**リポジトリ層ではなく `core/` に置く**（既存の前提）。

`item.membership`：

```
(新規)     → active
active     → delisted    discover で現れなかった
delisted   → active      再登場
```

`target.status`：

```
(新規)      → pending
pending     → queued      チェーン投入
queued      → downloading download 開始
downloading → processing  verify 以降
processing  → downloaded  index 完了
任意        → failed       一時的失敗
failed      → pending      リトライ
任意        → unavailable  回復不能 / 上限到達
任意        → blocked      外的要因
blocked     → pending      原因解消
downloaded  → missing      Phase 13 で使用（Phase 8 では遷移させない）
任意        → ignored      ユーザー操作（Phase 8 では CLI から）
```

## 15.2 不正な遷移

**遷移の妥当性を検証し、不正な遷移を拒否すること。** 例外を投げるか、明確なエラーを返す。

黙って通すと、Phase 11 以降で「なぜこの状態になったか分からない」バグを生む。

## 15.3 所有権

Phase 6 のレビューで検出された競合を踏まえ、**Target の状態更新も条件付き単一 UPDATE で行う。**

読み取り → 判定 → 更新の間に他のワーカーが入る余地を作らない。

---

# 16. Part B の検証

## 16.1 ユニットテスト

| 対象 | 内容 |
|---|---|
| Item upsert | 新規作成、既存更新、`first_seen_at` の保持 |
| **空振り判定** | **空の結果で delisted 判定が行われないこと** |
| delisted | 遷移する。**Artifact に影響しない** |
| 再登場 | `active` に戻り、`delisted_at` がクリアされる |
| Target 生成 | 有効な `playlist_profile` ごとに1件 |
| download 対象の選択 | `pending` / `failed` のみ。`unavailable` を含まない |
| 投入上限 | 上限を超えず、残数が記録される |
| Storage 事前確認 | 使えない場合にチェーンを投入せず `blocked` にする |
| Run 統計 | 各カウントが正しい |
| 状態遷移 | 正当な遷移が通り、**不正な遷移が拒否される** |
| 所有権 | 条件付き UPDATE で競合しない |
| ドライラン | DB が更新されない |

## 16.2 実機検証（Part B）

**`.local/test_playlists.txt` の URL を使う。無い場合は D-022 の Open Movies で代替し、実プレイリスト特有の項目はスキップして記録する。**

| # | 手順 | 期待結果 |
|---|---|---|
| 1 | 実プレイリストを登録し `sync discover` | Item が作成される |
| 2 | `sync discover --dry-run` を再実行 | 増減0。**DB が更新されない** |
| 3 | `sync download` | チェーンが投入され、順に完了する |
| 4 | 完了後、最終保存先を確認 | ファイルが配置されている |
| 5 | **`sync run` を再実行** | **新規0件。重複ダウンロード0件** |
| 6 | 投入上限を小さく設定して `sync download` | 上限までしか投入されず、残数が記録される |
| 7 | 6 を繰り返す | 残りが処理される |
| 8 | プレイリストから1件外して `sync discover` | **当該 Item が delisted になり、ファイルは残る** |
| 9 | 8 の Item を戻して `sync discover` | `active` に戻る |
| 10 | 存在しないプレイリスト URL で `discover` | 空振りとして記録され、**既存 Item が delisted にならない** |
| 11 | 大きめのプレイリスト（100件以上）で `discover` | タイムアウトせずに完了する。所要時間を記録 |
| 12 | 音楽プレイリストで `sync run` | opus が生成され、メタデータが埋まる |
| 13 | 1 Playlist に video / music の2プロファイル | 2系統の artifact が生成される |
| 14 | **実運用規模での連続実行** | 複数プレイリストを `sync run --all` で回し、完走する |
| 15 | 14 の実行中に worker を再起動 | タスクが `pending` に戻り、再開する |
| 16 | 14 の完了後、`target.status` の分布を確認 | `downloaded` が大半。`failed` / `unavailable` の理由が追える |
| 17 | ログを確認 | **実 URL がマスクされているか、または記録が適切な範囲に留まっている** |
| 18 | `staging orphans` | 完了分は削除済み。孤立は Phase 3 のファイルのみ |
| 19 | `make test` / `make lint` | クリーン |

**#5、#8、#10 が最重要。** それぞれ冪等性、削除の非伝播、空振り判定の検証にあたる。

## 16.3 実運用規模の検証について

#14 は夜間の長時間実行になる。以下に注意すること。

- **レート制御の既定値を変えないこと。** 早く終わらせようとして並列度を上げない
- 途中でエラーが多発した場合、**続行せず記録して停止する**（サイトへの負荷を避ける）
- 帯域と容量を監視し、Staging 容量が閾値に達したら停止する（`blocked` になるはず）
- **実行結果の要約を記録する。** 何件成功し、何件失敗し、失敗理由の内訳はどうだったか

---

# 17. 設定パラメータ

`CODE_DEFAULTS` に追加する。

| キー | 既定 |
|---|---|
| `pipeline.verify_timeout_sec` | 60 |
| `sync.max_targets_per_run` | 50 |
| `sync.delete_staging_after_index` | true |

`sync.delete_staging_after_index` を設定可能にするのは、デバッグ時に中間ファイルを残したい場合があるため。

---

# 18. レビュー②

Part B 完了時に実施する。記録は `docs/reviews/phase8.md`。

**特に見てほしい観点**：

- **空振り判定が確実に効いているか**（誤 delisted の経路がないか）
- **delisted が Artifact に影響していないか**
- 冪等性（再実行で重複しないか）
- 状態遷移の不正遷移が拒否されるか
- Target 更新の所有権競合
- 投入上限が効いているか
- **実 URL がドキュメント・コミット・ログに混入していないか**
- Run の完了判定が §13.3 の方針どおりか

---

# 19. コミット計画

**Phase 6 の反省を踏まえ、この粒度を守ること。テストと文書更新は対応する実装と同じコミットに含める。**

## Part A

| # | コミット |
|---|---|
| 1 | `chore: 検証用 URL ファイルを gitignore に追加`（§1.2） |
| 2 | `fix: ffmpeg 静的ビルドの健全性を再評価`（§2。差し替えた場合は `fix:`、不要なら `docs:`） |
| 3 | `feat: パイプラインのチェーン生成と payload 受け渡し`（§3） |
| 4 | `feat: download タスクハンドラ`（§4） |
| 5 | `feat: verify タスクハンドラ（ffprobe）`（§5） |
| 6 | `feat: postprocess タスクハンドラ（素通し）`（§7） |
| 7 | `feat: publish タスクハンドラ`（§6） |
| 8 | `feat: index タスクと artifact レコードの確定`（§8.1-8.3） |
| 9 | `feat: フック発火点の設置`（§8.4） |
| 10 | `feat: Staging の孤立ファイル検出`（§8.5） |
| 11 | `docs: Part A の実機検証結果と設計判断を記録`（§9.2） |
| 12 | `docs: レビュー①の指摘への対応`（§10） |

Part A 完了時に `checkpoint/step-07` タグを打つ。

## Part B

| # | コミット |
|---|---|
| 13 | `feat: Item / Target の状態遷移`（§15） |
| 14 | `feat: discover タスクと空振り判定`（§11） |
| 15 | `feat: download フェーズの起動と投入上限`（§12） |
| 16 | `feat: Run の生成と統計`（§13） |
| 17 | `feat: sync CLI とドライラン`（§14） |
| 18 | `docs: Part B の実機検証結果と設計判断を記録`（§16.2） |
| 19 | `docs: レビュー②の指摘への対応`（§18） |

Part B 完了時に `checkpoint/step-08` タグを打つ。

---

# 20. 完了条件

## Part A

1. ffmpeg / ffprobe の健全性が判定され、記録されている
2. 5タスクのチェーンが依存関係付きで生成される
3. payload で前段の結果が後段に渡る
4. yt-dlp / Storage の分類が正しい `task.status` に写像される
5. **`blocked` が attempts を消費しない**
6. `auth_failed` / `permission_denied` が `failed` になる（`blocked` ではない）
7. verify が ffprobe で破損を検出する
8. verify の `duration` が記録されるが判定に使われない
9. postprocess が空チェーンを素通しする
10. **`artifact` が index タスクでのみ作成される**
11. **Staging が index 後にのみ削除され、失敗時は削除されない**
12. 孤立ファイルが検出され、**自動削除されない**
13. 依存失敗で後続がキャンセルされる
14. Part A 実機検証17項目が通過し、記録されている
15. レビュー①が実施され、`docs/reviews/phase7.md` に記録されている

## Part B

16. Item の upsert が正しく動く
17. **空の discover 結果で delisted 判定が行われない**
18. **delisted が Artifact に影響しない**
19. 再登場で `active` に戻る
20. download 対象が正しく選択される
21. 投入上限が効き、残数が記録される
22. Storage が使えない場合にチェーンを投入しない
23. Run の統計が正しい
24. **不正な状態遷移が拒否される**
25. Target 更新に所有権競合がない
26. ドライランで DB が更新されない
27. **再実行で新規0件・重複ダウンロード0件**
28. Part B 実機検証19項目が通過し、記録されている
29. **実 URL がドキュメント・コミット・ログに混入していない**
30. レビュー②が実施され、`docs/reviews/phase8.md` に記録されている

## 共通

31. `make test` / `make lint` がクリーン
32. マイグレーションが必要な場合、`upgrade` → `downgrade` → `upgrade` が通る
33. コミット粒度が §19 に従っている
34. `AISTATE.md` が更新され、Phase 9 の着手点が書かれている
35. **push していない**（承認待ち）

---

# 21. ドキュメント更新義務

| ファイル | 内容 |
|---|---|
| `docs/基本設計.md` §2 | `tasks/handlers/*`、`core/sync.py` の責務 |
| `docs/基本設計.md` §4 | パイプラインと二相同期のシーケンス |
| `docs/基本設計.md` §5 | `PostProcessor` の実装状況 |
| `docs/基本設計.md` §7 | D-036 以降。最低限：ffmpeg の判定結果、`task.status` と `target.status` の対応表、空振り判定の方針、Run の完了判定、artifact の確定タイミング、Staging 削除のタイミング、checksum 非計算の判断、投入上限 |
| `docs/変更履歴.md` | 追加項目と実機検証結果 |
| `docs/reviews/phase7.md` / `phase8.md` | 新設 |
| `README.md` | `sync` を実装済みに更新。`staging orphans` を追記 |
| `docs/deployment.md` | Staging 容量の見積もりに実測値を反映 |
| `docs/troubleshooting.md` | 検証で踏んだ問題 |
| `.gitignore` | `.local/` を追加 |
| `AISTATE.md` | 全文書き換え（Part A 完了時と Part B 完了時の2回） |

---

# 22. 実装時の注意（再掲）

- **§0.2 の停止条件に該当したら止まる。** それ以外は進める
- **Staging は index 後にのみ削除。** 失敗時は削除しない
- **`blocked` は attempts を消費しない**
- **`artifact` は index タスクでのみ作成する**
- **空の discover 結果で delisted 判定をしない**
- **delisted はファイルに一切影響しない**
- **実プレイリスト URL をドキュメント・コミット・ログに書かない**
- **`task.status=unavailable` と `target.status=unavailable` は意味が違う**
- **状態更新は条件付き単一 UPDATE**（Phase 6 の教訓）
- **レート制御の既定値を変えない。** 並列度を上げない
- **コミット粒度を守る。** テストと文書を実装と同じコミットに
- **夜間に push しない**
- 判断に迷ったら要件定義 §1.4 の設計原則に照らす
