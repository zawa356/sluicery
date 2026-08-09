# Phase 4 実装指示書（改訂版） — オプション合成・レイアウト戦略・ファイル名方針・最小 CRUD CLI

| 項目 | 内容 |
|---|---|
| 対象 | 要件定義 §20 実装順序 #4（レイアウト戦略・ファイル名方針・最小 CRUD CLI を併合） |
| 前提 | Phase 3.5 完了（VM 実機検証済み、GitHub `zawa356/sluicery` に private で push 済み） |
| 作成日 | 2026-08-09（改訂版） |

本書は `docs/要件定義.md`（特に §9、§11.1、§11.2、§14.2）と `CLAUDE.md` を補完する。**着手前に両方を読むこと。**

Phase 4 は「**yt-dlp をどう呼ぶか**」を確定させるフェーズである。オプション合成、出力先の決定、ファイル名の方針は相互に不可分なため、まとめて扱う。

**本フェーズから、実装完了後に独立したレビュー役による点検を行う（§2）。**

---

# 0. 着手前の是正・確認

## 0.1 Phase 3.5 の締め（優先度：高）

Phase 3.5 のコード・ドキュメント作業は完了しているが、締めの手続きが残っている。

### `checkpoint/step-03.5` タグを打つ

AISTATE には「public 化・設定確認が完了したらタグを打つ」と書かれているが、**タグは実装の到達点を示すものであり、公開可否とは独立している。** public 化を見送った現状でも、Phase 3.5 のコードとドキュメントは完成しているのでタグを打つこと。

### `AISTATE.md` の書き換え

現在の AISTATE は「Phase 3.5 作業中」の状態で止まっている。

| 箇所 | 現状 | 修正 |
|---|---|---|
| 直近の作業 | Phase 3 の内容 | Phase 3.5 の内容に書き換え |
| 次にやること | Phase 3.5 の残作業6項目 | Phase 4 の着手点へ |
| 未解決・保留 | #4 まで | public 化見送りと未確認設定を項目として追加 |

**public 化に関する保留事項は「未解決・保留」表に移すこと。** 作業指示ではなく、判断待ちの状態として管理する。

- public 化は見送り中（判断待ち）
- Issues / Wiki / Projects の要否は未確認
- Dependabot alerts の要否は未確認
- README・`docs/deployment.md` の clone URL は `<repo>` のプレースホルダのまま（public 化時に差し替え）

### VM の残骸について

VM 側に `~/sluicery` / `~/sluicery.bundle` / `~/alt-media` / `/mnt/media` が残っている。**片付けはユーザーの判断**であり、Phase 4 では触らない。AISTATE の環境メモに現状の記載があるので、それを維持すること。

## 0.2 README の未実装コマンド表記（優先度：高）

**公開リポジトリの README に、存在しないコマンドが載っている。**

現在の「運用コマンド」表に以下が含まれているが、いずれも未実装である。

| コマンド | 実装フェーズ |
|---|---|
| `make sync`（`sluicery sync --all`） | Phase 8（二相同期） |
| `make backup`（`sluicery backup --stdout`） | Phase 20 |
| `make restore`（`sluicery restore --stdin`） | Phase 20 |

等価コマンドまで具体的に書かれているため、読者は実行できると受け取る。実際には失敗する。

**対処**：表に「実装状況」列を追加し、未実装のものを明示する。または未実装分を別表に分離する。README の「何ではないか」で開発途上であることは明記されているが、**コマンド表の粒度でも正直に示すこと。**

併せて以下も確認すること。

- `Makefile` に該当ターゲットが存在するか。存在して中身が空、または失敗するだけなら、明確なメッセージ（「Phase X で実装予定」）を出すようにする
- `docs/deployment.md` §5（バックアップとリストア）も同様に未実装である旨を追記する
- `docs/legal.md` の `make backup` への言及は仕様説明として残してよいが、現時点で未実装である旨の一言を添える

## 0.3 実装状況の確認（判断が必要）

以下を実際のコードで確認し、結果を報告してから着手すること。

| # | 確認内容 | 影響 |
|---|---|---|
| 1 | `profile` テーブルの真偽フラグ（`audio_extract` / `embed_metadata` / `embed_thumbnail` / `embed_chapters` / `subtitle_auto` / `subtitle_embed` など）が `NOT NULL` か `nullable` か | `NOT NULL` なら §3 のマイグレーションが必要 |
| 2 | 同じく `format_selector` / `container` / `audio_format` / `audio_quality` などの値フィールドが nullable か | 同上 |
| 3 | `playlist_profile.storage_id` が `NOT NULL` か | §10.3 の判断に影響 |
| 4 | `core/settings.py` の `CODE_DEFAULTS` に `defaults.video.*` / `defaults.music.*` がどこまで定義されているか | §4.4 の実装範囲 |
| 5 | `downloader/protocol.py` のプレフィックス定数の実際の値と、`ytdlp probe` / `fetch` が現在どう引数を組み立てているか | §11 の置き換え範囲 |

## 0.4 Phase 4 中の push 運用

リポジトリは private だが、`CLAUDE.md` §4.1 の「監査完了＋ユーザー承認後に push」というルールは維持する。

**運用**：フェーズ途中は push しない。Phase 4 完了時に `docs/公開前チェックリスト.md` の監査を通し、結果を報告した上で承認を得てから push する。

private であっても習慣として続けること。public 化のタイミングで一気に監査するより、フェーズ単位で刻む方が確実である。

---

# 1. スコープ

## 1.1 含むもの

1. §0 の是正
2. レビュー役サブエージェントの定義（§2）
3. `profile` テーブルの三状態化とマイグレーション（§3）
4. オプション合成モデル（6層、上書き規則）（§4）
5. 予約引数のガードと最小オプションパーサ（§5）
6. 由来追跡とコマンドラインプレビュー（§6）
7. レイアウト戦略（`flat` / `custom`）と subpath 解決（§7）
8. ファイル名方針（§8）
9. discover / download のコマンド種別分離（§9）
10. Storage / Profile / Playlist の最小 CRUD CLI（§10）
11. `ytdlp probe` / `fetch` の合成経由への置き換え（§11）
12. ユニットテストと実機検証（§12、§13）

## 1.2 含まないもの

- Storage アダプタの実装（Phase 5）。**レコードの CRUD のみ行い、疎通・転送は実装しない**
- Task キュー・ワーカー（Phase 6）
- Staging から Storage への publish（Phase 7）
- Discover の実処理（Phase 8）。**引数の組み立てまで**
- フォーマット検査機能（Phase 14）。合成基盤は本フェーズで用意する
- Web UI（Phase 9 以降）
- **VM での再検証**。Phase 4 の実機検証は開発機で行う（§13.1）

---

# 2. レビュー役サブエージェント

## 2.1 目的

実装役が自分の成果物をレビューすると、書いた本人の思い込みが検出されない。**独立したコンテキストのレビュー役**を立てる。

Phase 3.5 では、README の未実装コマンド（§0.2）が公開直前まで気づかれなかった。この種の見落としがレビュー役の対象である。

## 2.2 定義の配置

`.claude/agents/reviewer.md` として配置する。内容は以下を含むこと。

**役割**

要件定義・設計文書と実装成果物を照合し、齟齬を指摘する。**実装も修正も行わない。指摘のみを出力する。**

**入力として読むもの**

- `docs/要件定義.md`
- `docs/基本設計.md`
- `CLAUDE.md`
- 当該フェーズの指示書
- `docs/変更履歴.md`、`AISTATE.md`
- 当該フェーズの差分（`git diff <前フェーズのタグ>..HEAD`）

**点検の観点**

| 観点 | 具体例 |
|---|---|
| 要件との齟齬 | 要件定義に書かれた挙動と実装が違う。要件にない機能が入っている |
| 設計原則違反 | 要件定義 §1.4 の5原則、特に「ローカルのデータを失わない」「残骸を残さない」 |
| 前フェーズの前提の破壊 | AISTATE の「重要な前提」に反する実装 |
| **ドキュメントと実装の乖離** | **ドキュメントに書かれた機能・コマンドが実在するか**（§0.2 の類型） |
| ドキュメント更新漏れ | CLAUDE.md §2.1 のトリガ表に照らして未更新のファイル |
| 完了条件の未達 | 指示書の完了条件のうち、確認されていない項目 |
| 用語のドリフト | 要件定義の用語と異なる語がドキュメント・コードに使われている |
| コミット粒度 | CLAUDE.md §4.2 に照らして粗すぎないか |
| 未記録の設計判断 | 指示書にない判断をしたのに `docs/基本設計.md` §7 に記録がない |

**出力形式**

```markdown
## 総評
（2〜3行）

## 指摘

### [重大 | 中 | 軽微] 見出し
- 該当箇所: ファイル:行 または セクション
- 内容:
- 根拠: 要件定義 §X / CLAUDE.md §Y
- 提案:
```

**禁止事項**

- ファイルの編集、コミット、テストの実行
- 指摘の自己解決
- 「問題ありません」で済ませること（**観点ごとに確認した結果を明示する**）

## 2.3 運用フロー

```
実装役が Phase 4 を実装・コミット
  ↓
レビュー役を起動し、指摘リストを出力させる
  ↓
指摘を docs/reviews/phase4.md に保存してコミット
  ↓
実装役が対応する（対応内容も同ファイルに追記）
  ↓
判断が必要な指摘はユーザーへエスカレーション
```

`docs/reviews/` ディレクトリを新設し、以後のフェーズでも同じ形式で残す。

## 2.4 CLAUDE.md への追記

§8（作業フロー）に「フェーズ完了時のレビュー」を追加する。フェーズ完了の定義に「レビューを実施し、指摘に対応または記録済みであること」を含めること。

---

# 3. データモデルの変更

## 3.1 `profile` の三状態化

層継承（§4）を成立させるには、Profile の各フィールドが「**未設定**」を表現できる必要がある。`NOT NULL` の真偽値では「未設定」と「明示的に false」を区別できず、下位層の値を継承できない。

`profile` テーブルの以下を **nullable に変更する**（既に nullable なら変更不要）。

- 真偽フラグ：`audio_extract` / `embed_metadata` / `embed_thumbnail` / `embed_chapters` / `subtitle_auto` / `subtitle_embed`
- 値フィールド：`format_selector` / `container` / `audio_format` / `audio_quality` / `subtitle_langs` / `concurrent_fragments`

**nullable にしないもの**（Profile 自身の属性であり、継承の対象ではない）

- `name` / `kind` / `layout_strategy` / `expert_mode` / `allow_exec` / `ytdlp_args` / `output_template` / `postprocess_chain_json`

`expert_mode` と `allow_exec` は**セキュリティ境界なので継承させない**。Profile ごとに明示的に決める。既定は false。

## 3.2 マイグレーション

- Alembic リビジョンを追加する
- **D-008 の既知の制限により、autogenerate 出力に CHECK 制約の偽陽性 diff が混ざる。実際の変更に無関係なものは手で取り除くこと**
- `render_as_batch=True` が効いているため、SQLite でも `ALTER` できる
- `upgrade` → `downgrade` → `upgrade` が通ることを確認する
- 既存データがある場合、`NOT NULL` から nullable への変更でデータは失われないが、**既定値が入っていた行はそのまま「明示的な値」として残る**点に注意。開発環境では既存 Profile が無いはずなので実害はないが、挙動をマイグレーションのコメントに書くこと

## 3.3 要件定義との差分

要件定義 §7.1 は三状態を明示していなかった。**本書での変更として `docs/基本設計.md` §3 の差分表に追記し、§7 に設計判断（D-017 以降）として記録すること。**

---

# 4. オプション合成モデル

`src/sluicery/core/options.py`。

## 4.1 層

| 層 | 内容 | 保持場所 |
|---|---|---|
| L1 | **予約引数**（アプリ注入） | コード |
| L2 | グローバル既定 | `setting`（`download.*`） |
| L3 | 種別既定（video / music） | `setting`（`defaults.video.*` / `defaults.music.*`） |
| L4 | Profile | `profile` |
| L5 | Playlist 個別 | `playlist.ytdlp_args` |
| L6 | 一時上書き | CLI 引数 |

## 4.2 上書き規則

**構造化フィールドと自由文字列を分けて扱う。**

**構造化フィールド**（`format_selector`、`container`、`embed_*` など）

- 各層の値が `None` なら下位層を継承、値があれば上書きする
- L2 → L3 → L4 → L5 → L6 の順に解決する
- 真偽値は三状態：`True` → 有効化の引数、`False` → 無効化の引数（`--no-*`）、`None` → 継承

フィールドから yt-dlp 引数への変換は、**一箇所のテーブルに集約する**こと（`FIELD_TO_ARGS`）。個別に散らばると追跡不能になる。

**自由文字列**（`ytdlp_args`）

- 層順に**連結**する。yt-dlp は同一オプションの後勝ちなので、後段の層を後ろに置けば上位層を打ち消せる
- 連結前に `shlex.split` でトークン化し、§5 のガードにかける

## 4.3 予約引数の位置

L1（予約引数）は**最後に追加する**。yt-dlp の後勝ち規則により、他層からの指定を物理的に上書きできる。

**ただし `expert_mode` で予約引数の指定が許可された場合は例外**とする。その場合、

- L1 の該当引数を**注入しない**（重複させない）
- 「ファイル追跡が壊れる可能性がある」旨を警告として記録し、プレビューに表示する
- `--print` / `--progress-template` が潰された場合、実行時に「進捗・パスの取得ができない」旨を警告する

この扱いを `docs/基本設計.md` の設計判断に記録すること。

## 4.4 種別既定（L3）

Phase 2 で `defaults.video.*` / `defaults.music.*` のキー体系を定めた。Phase 4 で実際に参照する。値は要件定義 §9.5 の表に従う。

未定義のキーがあれば `CODE_DEFAULTS` に追加すること。

## 4.5 `--download-archive` について

**アプリは `--download-archive` を使わない。**

Phase 2 で「`item` / `target` が唯一の状態管理源であり、archive テキストファイルは使わない」と決定している（`docs/phase2_指示書.md` §4.1）。したがって、

- 予約引数として**拒否する**
- アプリからも**注入しない**

この理由をコード内コメントに明記すること。将来の実装者が「なぜ archive を使わないのか」を追えるようにする。

---

# 5. ガードとオプションパーサ

## 5.1 最小オプションパーサ

自由文字列に予約引数が紛れていないかを検出するため、最小限のパーサを実装する。

- `shlex.split` でトークン化する
- `--opt value` と `--opt=value` の両形式を認識する
- **エイリアスを解決する**（`-o` / `--output`、`-P` / `--paths`、`-O` / `--print` など）
- yt-dlp の全オプション表は不要。**予約引数と警告対象、およびその別名だけ**のテーブルを持つ

## 5.2 予約引数（拒否対象）

アプリが管理し、上書きされるとファイル追跡が壊れるもの。

```
--paths / -P
--output / -o
--print / -O
--print-to-file
--progress-template
--download-archive
--load-info-json
--dump-json / -j
--dump-single-json
--exec
--exec-before-download
```

`--dump-json` 系は stdout を汚し、§6 のフレーミング規約を壊すため予約に含める。

## 5.3 警告対象（拒否はしないが警告する）

アプリの出力解釈や方針と衝突しうるもの。**Phase 3 の実機検証で判明した知見を含む。**

| 引数 | 理由 |
|---|---|
| `--quiet` / `-q` | 進捗出力を抑制する（Phase 3 の実バグの原因） |
| `--no-progress` | 同上 |
| `--simulate` / `-s` | download で指定されるとファイルが落ちない |
| `--restrict-filenames` | §8 のファイル名方針と衝突する（非 ASCII が潰れる） |
| `--no-windows-filenames` | 同上 |
| `--no-newline` | 出力のパースが壊れる |

警告はプレビューと実行ログの両方に出すこと。

## 5.4 ガードの動作

- 既定：予約引数の指定は**バリデーションエラーとして拒否**する
- Profile の `expert_mode` が有効なら、警告を表示した上で通す（§4.3）
- `--exec` / `--exec-before-download` は**別扱い**。`.env` の `ALLOW_EXEC=true` と Profile の `allow_exec=true` の**両方**が有効な場合にのみ許可する。`expert_mode` だけでは通さない

## 5.5 `--output` の扱い

`--output` は予約引数だが、`layout_strategy = custom` を選択した場合のみ**正規の入力経路として開放する**（`profile.output_template` 経由）。自由文字列での `--output` 指定は、`expert_mode` の有無にかかわらず `custom` の入力経路に誘導するエラーメッセージを出すこと。

---

# 6. 由来追跡とプレビュー

## 6.1 由来追跡

各引数を「引数」「層」「由来（Profile 名、Playlist 名など）」の組で保持する。

- 構造化フィールド由来：フィールド名まで特定できる
- 自由文字列由来：**層と、その層の自由文字列であること**まで示せればよい。トークン単位の由来までは求めない
- 予約引数：L1 として示す

## 6.2 プレビュー

CLI で出す（UI は Phase 9 以降）。

```
sluicery options preview --playlist <id> --profile <id> [--kind discover|download]
```

出力に含めること。

- 最終的に組み立てられたコマンドライン全文
- 各引数の由来層
- 警告（§5.3、`expert_mode` による予約引数の通過など）
- 解決された出力パス（`--paths` と `--output` の組み合わせ結果）

**必ずマスク層（`mask_command_line`）を通すこと。**

`--kind` を省略した場合は download を既定とする。

---

# 7. レイアウト戦略と subpath

`src/sluicery/layout/`。

## 7.1 インターフェース

要件定義 §14.2 の `LayoutStrategy` を実装する。`flat` と `custom` のみ。`jellyfin` / `navidrome` は将来。

## 7.2 `flat`（既定）

要件定義 §11.1 に従う。

```
video : <subpath>/%(upload_date>%Y-%m-%d)s %(title)s [%(id)s].%(ext)s
music : <subpath>/%(playlist_index)03d %(track,title)s [%(id)s].%(ext)s
```

**`upload_date` や `playlist_index` が取得できない場合の挙動を決めること。** yt-dlp は既定で `NA` を埋めるため、`NA タイトル [id].mkv` のようなファイル名になる。

AISTATE 未解決 #4 のとおり、generic extractor 対象の URL では `uploader` / `duration` が取れない。同様に `upload_date` も取れない可能性が高く、**この問題は Phase 4 の実機検証で必ず表面化する。**

対処の選択肢：

- `%(upload_date>%Y-%m-%d|)s` のようにフォールバックを空にする
- `--output-na-placeholder` を設定する
- テンプレート自体を条件分岐させる

**実機で挙動を確認した上で決定し、`docs/基本設計.md` に記録すること。**

## 7.3 `custom`

`profile.output_template` を使う。バリデーションで以下を強制する。

- **末尾（拡張子の直前）に `[%(id)s]` を含むこと。** relink がこれに依存している（要件定義 §11.3）
- 絶対パス指定を含まないこと（`--paths` と衝突する）
- パス区切りを含む場合は、`--paths home` からの相対として解釈されることを警告する

## 7.4 subpath の解決

`playlist_profile.subpath` はテンプレートである（既定 `{playlist.folder_name}`）。

- 利用可能な変数を定義し、ドキュメント化すること（最低限 `{playlist.folder_name}`、`{playlist.name}`、`{profile.name}`、`{profile.kind}`）
- **yt-dlp のテンプレート構文（`%(...)s`）とは別物**であることを明確にする。混同されるとデバッグ不能になるので、記法を分ける現在の方針を維持する
- 解決結果にパス traversal（`..`）が含まれないことを検証する

## 7.5 Staging 内のレイアウト（重要）

**Staging 内に、最終的な相対パス（`subpath` + ファイル名）をそのまま再現する。**

```
<STAGING_DIR>/<work-id>/<subpath>/<filename>
```

こうしておくと、Phase 7 の publish は「Staging の作業ルートからの相対パスを、Storage の同じ相対パスへ移す」だけになり、パス決定ロジックが二箇所に分かれない。

`--paths home:<STAGING_DIR>/<work-id>` と `--paths temp:` を指定し、`--output` には subpath 以下の相対パスを渡す構成とする。

実ファイルパスは `--print after_move:` で取得する（Phase 3 の `protocol.py` の規約に従う）。

---

# 8. ファイル名方針

## 8.1 方針の変更（要件定義 §11.2 からの見直し）

要件定義 §11.2 は独自のサニタイズ実装を想定していたが、**yt-dlp の機能に寄せる。**

| 手段 | カバー範囲 |
|---|---|
| `--windows-filenames` | Windows / SMB で禁止される文字、末尾のドット・空白など |
| `--trim-filenames <N>` | ファイル名長の上限 |
| 独自実装 | 上記でカバーされない残り（Windows 予約語 `CON` `NUL` `COM1` 等） |

**`--restrict-filenames` は使わない。** 非 ASCII を潰すため、日本語タイトルが失われる。

独自実装を減らすほど、yt-dlp 側の挙動変化に強くなる。

## 8.2 実機での確認（必須）

`--windows-filenames` が**実際に何をカバーし、何を残すか**を実機で確認すること。

確認方法の例：タイトルに `\ / : * ? " < > |`、末尾ドット、末尾空白、予約語（`CON` など）、極端に長い文字列、絵文字を含む素材を用意するか、`--print filename` でシミュレートする。

**確認結果を `docs/基本設計.md` に記録し、独自実装の範囲を確定すること。**

## 8.3 `core/naming.py`

上記で確定した「yt-dlp がカバーしない部分」のみを実装する。適用対象は以下。

- `playlist.folder_name`（ユーザー入力）
- `subpath` の解決結果

yt-dlp が生成するファイル名部分については、`--windows-filenames` に委ねる（アプリ側で二重にサニタイズしない）。

## 8.4 Unicode 正規化

`folder_name` と `subpath` は NFC に正規化すること。macOS 由来の NFD 文字列が混ざると、SMB 越しに同名ディレクトリが2つできる事故が起きる。

---

# 9. コマンド種別の分離

## 9.1 インターフェース

```python
def build_discover_args(playlist: Playlist, *, overrides: ... = None) -> BuiltCommand: ...
def build_download_args(target: Target, *, overrides: ... = None) -> BuiltCommand: ...
```

`BuiltCommand` は引数リスト、由来情報、警告、解決済みの出力パスを持つ。

## 9.2 discover 用

- `--flat-playlist`
- `--print` でエントリの識別情報を JSON として出力（`protocol.py` の規約に従う）
- `--simulate` 相当（ダウンロードしない）
- `ytdlp.discover_timeout_sec` を適用する
- **レイアウト・ファイル名関連の引数は不要**

## 9.3 download 用

- 単一の `source_url` に対して実行する
- `--paths` / `--output` / `--print after_move:` / `--progress-template`
- 構造化フィールド由来の引数一式
- `ytdlp.idle_timeout_sec` / `ytdlp.absolute_timeout_sec` を適用する

## 9.4 共通

レート制御（`--sleep-requests` / `--limit-rate` / `--retries` など）は両方に適用する。`LC_ALL=C` の固定は `YtdlpRunner` 側の責務（Phase 3）であり、ここでは扱わない。

---

# 10. 最小 CRUD CLI

合成の検証には Profile / Playlist / playlist_profile のレコードが必要。Web UI（Phase 9 以降）までの暫定として CLI を用意する。

## 10.1 コマンド

```
sluicery storage  add|list|show|edit|remove
sluicery profile  add|list|show|edit|remove
sluicery playlist add|list|show|edit|remove
sluicery playlist attach-profile <playlist> <profile> [--storage ...] [--subpath ...]
sluicery playlist detach-profile <playlist> <profile>
```

## 10.2 方針

- **暫定実装であり、Phase 9 以降の Web UI で置き換えられる**ことをコード内コメントに明記する
- 入力バリデーションはリポジトリ層ではなく CLI 層で行う（状態遷移ロジックを持たせない原則を維持）
- 削除時、**レコード削除がファイル削除を引き起こさないこと**を確認する（設計原則1）。Playlist 削除時の `item` の扱いは要件定義 §13.2 でユーザー選択とされているため、CLI では `--keep-items` / `--delete-items` を明示的に要求する
- **README の運用コマンド表に追加する際は、暫定実装であることを明記する**（§0.2 の再発防止）

## 10.3 Storage の扱い

Storage アダプタは Phase 5 だが、`playlist_profile.storage_id` を埋めるためにレコードは必要。

- `sluicery storage add --kind local --name <名前> --path <パス>` の最小形を実装する
- **疎通テスト・空き容量取得・認証情報の暗号化保存は Phase 5**。ここではレコード作成のみ
- `kind=remote` は Phase 5 まで受け付けない（明確なエラーメッセージを出す）

## 10.4 出力

`list` は表形式、`show` は詳細表示。**シークレットを含む項目は必ずマスクする**（Phase 5 以降で `credentials_encrypted` が入るため、今のうちに経路を通しておく）。

---

# 11. 暫定実装の置き換え

## 11.1 `ytdlp probe` / `fetch`

Phase 3 で固定オプションを直接指定していた箇所を、`core/options.py` 経由に置き換える（Phase 3 指示書 §9.1 の約束）。

- `_cmd_ytdlp_probe` / `_build_fetch_args` などの暫定コードを削除する
- `fetch` は Playlist / Profile を指定できるようにする。指定がない場合は、種別既定（L3）のみで組み立てる暫定モードを残してよい
- **`--print` が暗黙に `--quiet` を付与する問題（Phase 3 の知見）を、合成側で恒久的に処理すること。** `--progress` の明示が必要な条件を `FIELD_TO_ARGS` またはビルダに組み込む

## 11.2 AISTATE 未解決 #4 の再確認

`ytdlp probe` が generic extractor 対象の URL では `uploader` / `duration` を取得できない件を、**実運用に近い素材で再確認する**（§13）。結果によっては §7.2 の `NA` フィールドの扱いに影響する。

---

# 12. ユニットテスト

| 対象 | 内容 |
|---|---|
| 層解決 | L2〜L6 の上書き順。`None` による継承。三状態の真偽値（True / False / None） |
| 自由文字列 | 層順の連結。後段の層が前段を打ち消せること |
| オプションパーサ | `--opt value` / `--opt=value` / エイリアス / 短縮形の解決 |
| 予約引数ガード | 各予約引数の検出。`expert_mode` off で拒否、on で警告付き通過 |
| `--exec` の二重ゲート | `ALLOW_EXEC` と `allow_exec` の4通りの組み合わせ |
| `--download-archive` | 常に拒否され、注入もされないこと |
| 警告対象 | `--quiet` 等の指定で警告が出ること |
| `custom` テンプレート | 末尾 `[%(id)s]` の検証。絶対パスの拒否 |
| subpath | テンプレート展開。`..` を含む結果の拒否。NFC 正規化 |
| 由来追跡 | 各引数に正しい層が紐づくこと |
| マスク | プレビュー出力にシークレットが現れないこと |
| discover / download | それぞれ必要な引数が含まれ、不要な引数が含まれないこと |
| マイグレーション | `upgrade` → `downgrade` → `upgrade` |
| CRUD CLI | 作成・更新・削除。削除がファイル操作を伴わないこと |

**yt-dlp の実行を伴うテストは Phase 3 と同様に擬似スクリプトでモックし、実ネットワークを使わない。**

---

# 13. 実機検証

## 13.1 実施環境

**開発機で実施する。** Phase 4 はオプション合成の検証であり、クリーンな環境を必要としない。VM での再検証は Phase 5（Storage アダプタ）以降、必要になった時点で行う。

**CLI でファイルを生成する操作には `--user "$(id -u):$(id -g)"` を付けること**（Phase 3.5 の知見。付けないと生成物が root 所有になる）。

## 13.2 試験素材

**Phase 3 の D-015（Blender の直リンク）だけでは不十分。** generic extractor 経由の単一ファイルであり、プレイリスト展開・フォーマット選択・字幕・メタデータ埋め込みの経路を一切通らない。

**Creative Commons ライセンスで公開されている、プレイリストを含む素材**を選定すること。選定した URL と選定理由を `docs/基本設計.md` に記録する。

著作権上明確に問題のない素材に限ること。判断に迷う場合はユーザーに確認する。

D-015 の素材も、generic extractor 経路の回帰確認として引き続き使うこと。

## 13.3 検証項目

| # | 手順 | 期待結果 |
|---|---|---|
| 1 | `storage add` / `profile add` / `playlist add` / `attach-profile` | レコードが作成される |
| 2 | `options preview --kind download` | コマンドライン全文と各引数の由来層が表示される |
| 3 | Profile の値を変更して再度 preview | 該当引数が L4 由来として変化する |
| 4 | Playlist の `ytdlp_args` に何か指定して preview | L5 由来として後段に現れ、L4 の指定を打ち消せる |
| 5 | `ytdlp_args` に `--output` を指定 | 拒否され、`custom` への誘導メッセージが出る |
| 6 | `expert_mode` を有効にして予約引数を指定 | 警告付きで通り、L1 の該当引数が注入されない |
| 7 | `ALLOW_EXEC=false` で `allow_exec` を有効にし `--exec` を指定 | 拒否される |
| 8 | 動画プロファイルで `fetch` | Staging に `<subpath>/<filename>` の構造で生成される |
| 9 | 音楽プロファイルで `fetch` | opus が抽出され、メタデータとサムネイルが埋め込まれる |
| 10 | 同一 URL に2プロファイルを適用 | **2つのファイルが別々の subpath に生成される** |
| 11 | 禁止文字・末尾ドット・長いタイトルを含む素材で `--print filename` | `--windows-filenames` の実効範囲が判明する。**結果を基本設計に記録** |
| 12 | 日本語タイトルの素材 | 日本語が保持される（`--restrict-filenames` が効いていない） |
| 13 | `upload_date` が取れない素材（D-015 の直リンク等） | §7.2 で決めた挙動どおりになる。`NA` がファイル名に混入しない |
| 14 | `layout_strategy=custom` で末尾 `[%(id)s]` 無しのテンプレート | バリデーションエラーになる |
| 15 | プレイリスト URL で `probe` | 複数エントリが取得でき、`uploader` / `duration` が埋まる（未解決 #4 の再確認） |
| 16 | preview と `fetch` の実行ログ | シークレットがマスクされている |
| 17 | 生成されたファイルの所有者を確認 | `--user` 指定時にホストユーザー所有になる |
| 18 | `make test` / `make lint` | 全件パス・クリーン |

---

# 14. コミット計画

| # | コミット |
|---|---|
| 1 | `docs: Phase 3.5 を締め、AISTATE を Phase 4 着手点に更新`（§0.1） |
| 2 | `docs: README の運用コマンド表に実装状況を明記`（§0.2） |
| 3 | `chore: レビュー役サブエージェントを定義`（§2） |
| 4 | `feat: profile の設定フィールドを nullable 化（層継承のため）`（§3） |
| 5 | `feat: 最小オプションパーサと予約引数ガード`（§5） |
| 6 | `feat: オプション合成モデル（6層の解決と由来追跡）`（§4, §6） |
| 7 | `feat: レイアウト戦略（flat / custom）と subpath 解決`（§7） |
| 8 | `feat: ファイル名方針を yt-dlp のオプションに寄せる`（§8） |
| 9 | `feat: discover / download のコマンドビルダ`（§9） |
| 10 | `feat: CLI に options preview を追加`（§6.2） |
| 11 | `feat: CLI に storage / profile / playlist の最小 CRUD を追加`（§10） |
| 12 | `refactor: ytdlp probe / fetch をオプション合成経由に置き換え`（§11） |
| 13 | `test: Phase 4 のユニットテストを追加`（§12） |
| 14 | `docs: 実機検証の結果と設計判断を記録`（§13） |
| 15 | `docs: レビュー指摘への対応`（§2.3） |

**#1 の前に `checkpoint/step-03.5` タグを打つこと**（§0.1）。

Phase 4 完了後、`checkpoint/step-04` タグを打つ。

**push は Phase 4 完了時、監査を通して承認を得てからまとめて行う**（§0.4）。

---

# 15. 完了条件

1. `checkpoint/step-03.5` タグが打たれている
2. `AISTATE.md` が Phase 3.5 完了・Phase 4 着手の状態に書き換えられ、public 化関連が「未解決・保留」に整理されている
3. README の運用コマンド表に実装状況が明記され、未実装コマンドが実行可能であるかのような記載がない
4. §0.3 の確認結果が報告されている
5. `.claude/agents/reviewer.md` が存在し、§2.2 の内容を満たす
6. `profile` の設定フィールドが nullable になり、マイグレーションの `upgrade` → `downgrade` → `upgrade` が通る
7. 6層の上書きが仕様どおり動作する（三状態を含む）
8. 自由文字列が層順に連結され、後段が前段を打ち消せる
9. 予約引数が既定で拒否され、`expert_mode` で警告付きに通る
10. `--exec` が `ALLOW_EXEC` と `allow_exec` の二重ゲートで制御される
11. `--download-archive` が常に拒否され、注入もされない
12. `options preview` がコマンドライン全文と**各引数の由来層**を表示する
13. プレビュー・ログにシークレットが平文で現れない
14. `flat` / `custom` の両方でパスが解決され、`custom` の末尾 `[%(id)s]` が強制される
15. subpath がテンプレート展開され、`..` を含む結果が拒否される
16. Staging 内に `<subpath>/<filename>` の構造が再現される
17. `--windows-filenames` の実効範囲が実機で確認され、記録されている
18. 日本語タイトルが保持される
19. `NA` がファイル名に混入しない（§7.2 の方針どおり）
20. 同一 URL に2プロファイルを適用すると、2ファイルが別々の subpath に生成される
21. `storage` / `profile` / `playlist` の最小 CRUD が動作し、**削除がファイル操作を伴わない**
22. `ytdlp probe` / `fetch` が合成経由に置き換わり、暫定コードが削除されている
23. `--print` による進捗抑制が合成側で恒久的に処理されている
24. `make test` / `make lint` がクリーン
25. 実機検証（§13.3）の全18項目が通過し、結果が記録されている
26. **レビュー役による点検が実施され、指摘が `docs/reviews/phase4.md` に記録されている**
27. 指摘への対応が完了しているか、未対応の理由が記録されている
28. 監査を通した上で push の承認を求めている（§0.4）
29. `AISTATE.md` が更新され、Phase 5 の着手点が書かれている

---

# 16. ドキュメント更新義務

| ファイル | 内容 |
|---|---|
| `docs/基本設計.md` §2 | `core/options.py`、`core/naming.py`、`layout/*` の責務を更新 |
| `docs/基本設計.md` §3 | `profile` の nullable 化を差分表に追記 |
| `docs/基本設計.md` §4 | オプション合成のフロー、レイアウト解決のフローを追記 |
| `docs/基本設計.md` §5 | `LayoutStrategy` の実装状況を更新 |
| `docs/基本設計.md` §6 | ファイル名方針（`--windows-filenames` の実効範囲）を追記 |
| `docs/基本設計.md` §7 | D-017 以降として設計判断を記録。最低限：三状態化、`expert_mode` 時の L1 非注入、ファイル名方針の yt-dlp 寄せ、`NA` フィールドの扱い、試験素材の選定 |
| `docs/変更履歴.md` | 未リリース欄に追加項目と実機検証結果 |
| `docs/reviews/phase4.md` | 新設。レビュー指摘と対応 |
| `CLAUDE.md` §8 | フェーズ完了時のレビュー手順を追加 |
| `CLAUDE.md` §1 | ドキュメント体系の表に `docs/reviews/` を追加 |
| `README.md` | 運用コマンド表に実装状況を明記（§0.2）。最小 CRUD CLI の追加（暫定である旨を明記） |
| `docs/deployment.md` | §5 のバックアップ/リストアが未実装である旨を追記 |
| `docs/legal.md` | `make backup` が未実装である旨を一言添える |
| `AISTATE.md` | 全文書き換え（着手前と完了後の2回） |

---

# 17. 実装時の注意（再掲）

- **構造化フィールドと自由文字列を分けて扱う。** 混ぜると層解決が破綻する
- **三状態を潰さない。** `None` を「未設定」として扱い、`False` と区別する
- **`FIELD_TO_ARGS` を一箇所に集約する。** 散らばると追跡不能になる
- **`--download-archive` は使わない。** 理由をコメントに残す
- **`--restrict-filenames` を使わない。** 日本語が失われる
- **独自サニタイズは最小限に。** yt-dlp に寄せるほど変化に強い
- **`--print` は `--quiet` を暗黙付与する。** 合成側で恒久対処する
- **Staging に最終相対パスを再現する。** publish（Phase 7）のロジックを単純に保つ
- **CRUD CLI は暫定。** Phase 9 以降で置き換わることをコメントに残す
- **ドキュメントに未実装機能を実行可能であるかのように書かない**（§0.2 の再発防止）
- **レコード削除がファイル削除を引き起こす実装をしない**
- **レビュー役は実装も修正もしない。** 指摘のみ
- **実機検証の CLI 実行には `--user` を付ける**
- 判断に迷ったら要件定義 §1.4 の設計原則に照らし、それでも決まらなければ**確認を取る**
