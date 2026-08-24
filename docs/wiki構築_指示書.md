# Wiki 構築指示書 — 利用者向けドキュメントと public 化

| 項目 | 内容 |
|---|---|
| 対象 | GitHub Wiki の構築、および前提となる public 化の準備 |
| 前提 | Phase 1-20 完了（`checkpoint/step-20`）、push 未実施 |
| 作成日 | 2026-08-22 |

**本作業は公開を伴う。§0 の停止条件を厳守すること。**

---

# 0. 実行ルール

## 0.1 進行順序

```
§1  public 化前の整理（本体リポジトリ）
  ↓
§2  Wiki の執筆（ローカルの下書きディレクトリ）
  ↓
§3  監査
  ↓
【停止】ユーザーの判断と手作業を待つ
  ↓
§4  public 化と Wiki の初期化（ユーザー作業 + 承認）
  ↓
§5  push（本体 + Wiki）
  ↓
§6  維持のルール整備
```

## 0.2 止まって判断を待つこと

1. **§3 の監査完了後は必ず停止する。** 承認なしに public 化・push しない
2. `AISTATE.md` と `docs/phase*_指示書.md` の公開可否（レビュー④が「利用者が判断」としている）
3. 機密・実環境情報を検出したとき
4. 本書の指示と `CLAUDE.md` が矛盾すると判断したとき

## 0.3 やってはいけないこと

- **承認前の `git push`、`gh repo edit --visibility public`**
- Wiki に仕様を書くこと（§2.2）
- 実 URL・ホスト名・認証情報の記載
- 未実装・未検証のものを実装済み・検証済みと書くこと

---

# 1. public 化前の整理（本体リポジトリ）

Wiki より先に、本体側を公開できる状態にする。

## 1.1 clone URL の置換

README と `docs/deployment.md` の `<repo>` プレースホルダを、実際の URL に置き換える。

```
https://github.com/zawa356/sluicery.git
```

未解決 #5 の一部。

## 1.2 公開可否の判断が必要なファイル

**以下は判断を仰ぐ対象。勝手に削除も追加もしない。** §3 の報告に含めること。

| ファイル | 内容 | 論点 |
|---|---|---|
| `AISTATE.md` | 開発の内部状態、未解決事項、環境メモ | 実害はないが、開発プロセスの内情が読める |
| `docs/phase*_指示書.md` | 各フェーズの指示 | 開発手法が公開される。見せて良いものだが判断は分かれる |
| `docs/reviews/*.md` | レビュー記録 | 過去の欠陥と修正履歴が読める |
| `docs/受け入れ条件確認.md` | 受け入れ条件の判定結果 | 未達項目が公開される |

`docs/phase13-20_指示書.md` は現在**未追跡**である（利用者指定）。他の指示書は追跡されているため、扱いが不揃いになっている。**この不整合も判断対象として報告すること。**

## 1.3 README の最終確認

公開時に最初に読まれる。以下を確認する。

- 未実装表記が残っていないか（Phase 20 で更新済みのはず）
- **検証状況の表が正確か**（`mount` は未検証、外部 VM 検証は Phase 3.5 時点のもの）
- Quick Start がコピペで動くか
- Wiki へのリンクを追加する（§2.5）

## 1.4 GitHub の設定（判断対象）

未解決 #5。§3 の報告で確認を求めること。

| 設定 | 推奨 |
|---|---|
| Actions | **無効のまま**（意図しないワークフロー実行を防ぐ） |
| Wiki | **有効化する**（本作業に必要） |
| Issues | 判断を仰ぐ。単独開発なら不要 |
| Projects | 判断を仰ぐ。不要と思われる |
| Dependabot alerts | **有効を推奨**。依存の脆弱性を通知してくれる |

---

# 2. Wiki の執筆

## 2.1 作業場所

**`.wiki-draft/` に下書きする。** `.gitignore` に追加すること（本体リポジトリにコミットしない）。

Wiki は別リポジトリ（`sluicery.wiki.git`）であり、初期化されるまで clone できない。初期化はユーザーの手作業（§4.2）。

## 2.2 棲み分けの原則（最重要）

**Wiki と `docs/` は必ず乖離する。** Wiki は別リポジトリで、コード変更時に PR レビューを通らないためである。

これを避けるため、以下を厳守する。

| 置き場所 | 内容 |
|---|---|
| `docs/` | **仕様の正。** 要件定義、基本設計、変更履歴、reviews、footprint |
| Wiki | **読み物。** 導入ガイド、運用レシピ、FAQ |

**Wiki に仕様を書かない。** 設定項目の一覧、状態遷移の定義、アーキテクチャの詳細などは書かず、**`docs/` の該当箇所へリンクする。**

```markdown
<!-- 良い例 -->
Storage には `local` と `remote` の2種類があります。詳しくは
[docs/storage.md](https://github.com/zawa356/sluicery/blob/main/docs/storage.md) を参照してください。

<!-- 悪い例 -->
Storage の kind は local / remote / mount の3種類で、それぞれ...（仕様の再掲）
```

**判断基準：「コードを変更したときに、この記述も直す必要があるか」。** 必要ならそれは仕様であり、`docs/` に置くべき。

## 2.3 ページ構成

ファイル名は **ASCII**（URL が安定する）、見出しは日本語とする。

```
Home.md                     トップページ
_Sidebar.md                 サイドバー（全ページ共通のナビゲーション）
_Footer.md                  フッター

Getting-Started.md          導入
First-Setup.md              初期設定
Storage-Setup.md            保存先の設定
Profile-Guide.md            Profile の作り方
Scheduling.md               スケジュール運用
File-Management.md          ファイル整理と relink
Backup-Restore.md           バックアップと復元
Updating-ytdlp.md           yt-dlp の更新
Retention.md                古いファイルの削除
Using-Cookies.md            Cookie を使う場合
FAQ.md                      よくある質問
Glossary.md                 用語
```

**初版に画像を含めない。** スクリーンショットは後日ユーザーが追加する前提とし、必要な箇所に `<!-- TODO: スクリーンショット -->` を残す。

## 2.4 各ページの内容

### Home.md

- sluicery が何をするか（2〜3行）
- **何ではないか**（README の該当セクションを要約。「配信元での削除に追従しない」は必ず含める）
- 主要ページへの導線
- **リポジトリ本体と `docs/` へのリンク**
- 開発途上ではなく完成状態であること、ただし `mount` は未検証である旨

### Getting-Started.md

- 前提条件（README の要約 + リンク）
- インストール手順（Quick Start の噛み砕き版）
- **つまずきやすい点を先に書く**：`MEDIA_ROOT` の事前作成、`SECRET_KEY` の生成と紛失時の影響
- 起動確認、初回ログイン
- 詳細は `docs/deployment.md` へリンク

### First-Setup.md

初回セットアップの通し手順。**これが Wiki の中心。**

```
1. 管理者としてログイン
2. Storage を登録する（まず local で試すのが安全）
3. 接続テストを実行する
4. Profile を作る（video / music）
5. Playlist を登録する
6. Playlist に Profile を割り当て、保存先を指定する
7. 手動で discover を実行し、Item が検出されることを確認する
8. 手動で download を実行し、1件取得できることを確認する
9. スケジュールを設定する
```

**各ステップで「何が起きるか」「うまくいかないときの確認点」を書く。**

### Storage-Setup.md

- `local` の設定（PUID/PGID の話を含む）
- `remote`（SMB）の設定手順
- **接続テストの4段階の読み方**（どこで失敗したかで原因が分かる）
- `mount` は**実装済みだが実機未検証**である旨を明記し、rclone remote を推奨する
- 詳細は `docs/storage.md` へリンク

### Profile-Guide.md

- Profile とは何か（複数の Playlist で共有できるテンプレート）
- video / music の作り分け
- **三状態（継承 / 有効 / 無効）の意味**。ここは利用者が最も混乱する箇所
- コマンドラインプレビューの読み方（どの層由来かが分かる）
- フォーマット検査の使い方
- 1つの Playlist に複数 Profile を割り当てると何が起きるか
- **Playlist の自由引数が Profile より後勝ち**である点（AISTATE の「既知の落とし穴」より）

### Scheduling.md

- discover と download を分ける意味（検出は頻繁に、取得は夜間に、など）
- cron 式の書き方（簡単な例をいくつか）
- 実行可能時間帯の設定
- ジッターの意味
- 一時停止
- **worker 設定は再起動が必要、scheduler 設定は60秒で反映**という違い

### File-Management.md

**設計思想を説明するページ。** 利用者が最も驚く挙動を扱う。

- **配信元で消えてもローカルは消えない**（これは意図的な設計）
- `delisted` は記録のみで、ファイルには触れない
- エクスプローラーからファイルを移動・リネームしてよい。整合性チェックが追従する
- **`[<source_id>]` を消してリネームすると追従できない**。その場合は手動リンク画面を使う
- `missing` の3つの扱い（放置 / 再取得 / 無視）
- 整合性チェックは読み取り専用である

### Backup-Restore.md

- 何がバックアップされ、何がされないか
- **`SECRET_KEY` はバックアップに含まれない。** 別途保管が必要である旨を強調
- 復元手順
- 復元時に鍵が一致しないとどうなるか

### Updating-ytdlp.md

- yt-dlp が壊れやすい理由（配信サイトの仕様変更）
- 自動更新の仕組みとスモークテスト
- 失敗時の自動ロールバック
- 手動更新・手動ロールバック
- **Deno（JS ランタイム）がイメージに同梱されている**こと。更新で要件が変わった場合の対処は `docs/troubleshooting.md` へリンク

### Retention.md

**削除を伴う唯一の機能。注意喚起を厚くする。**

- 既定で無効であること
- ドライランが必須であること
- 件数上限・割合ガードがあること
- **削除は取り消せない**
- 差分レポート（delisted の一覧）とは別機能であること

### Using-Cookies.md

- 既定で無効であること
- **アカウント停止のリスク**（要件定義 §9.2、`docs/legal.md`）
- Playlist 単位のオプトインであること
- Cookie は暗号化して保存され、実行時のみ tmpfs に展開されること
- **§1.1 の 403 切り分け結果に応じて記述を調整する**（Deno だけで足りるなら「通常は不要」と明記する）

### FAQ.md

想定質問（実際に踏んだ問題を優先）：

- 配信元で消えた動画がローカルに残っているのはなぜ？
- HTTP 403 が出る
- Web UI にログインできない
- worker が何もしていないように見える
- ダウンロードが `blocked` で止まっている
- Staging の容量が足りない
- 同じ動画が複数のフォルダにある
- ファイル名に `[xxxxx]` が付いているのはなぜ？（**消さないでほしい理由**）
- プレイリストを削除したらファイルも消える？（消えない）
- 複数のプロファイルを1つのプレイリストに割り当てたい
- Jellyfin / Navidrome と連携したい（**未実装**、フック機構の拡張点があることを説明）

**トラブルシューティングの詳細は `docs/troubleshooting.md` へリンクし、重複させない。**

### Glossary.md

利用者向けに噛み砕いた用語説明。

Item / Target / Artifact / Run / Task / Playlist / Profile / Storage / Staging / discover / download / delisted / blocked / missing

**正式な定義は `docs/要件定義.md` §2 へリンクする。** ここは「読み方」に留める。

### _Sidebar.md

全ページからのナビゲーション。カテゴリ分けする。

```markdown
**はじめに**
- [ホーム](Home)
- [導入](Getting-Started)
- [初期設定](First-Setup)

**ガイド**
- [保存先の設定](Storage-Setup)
...
```

### _Footer.md

- リポジトリへのリンク
- ライセンス（MIT）
- **「仕様の正は docs/ です」という一文**

## 2.5 本体側からのリンク

README の「ドキュメント」表に Wiki へのリンクを追加する。

```markdown
| [Wiki](https://github.com/zawa356/sluicery/wiki) | 導入ガイド・運用レシピ・FAQ |
```

## 2.6 執筆時の禁止事項

- **実プレイリスト URL、ホスト名、IP、共有名、ユーザー名を書かない**（プレースホルダを使う）
- **未実装機能を実装済みと書かない**（Jellyfin 連携、トランスコードなど）
- **未検証を検証済みと書かない**（`mount`）
- 仕様を再掲しない（§2.2）
- `docs/` の内容をコピーしない。リンクする

---

# 3. 監査

## 3.1 対象

**本体リポジトリと Wiki 下書きの両方。**

`docs/公開前チェックリスト.md` の手順を実行する。Wiki 下書きは未追跡なので、**ファイル内容に対して直接**同じパターン検査を行う。

```bash
# Wiki 下書きに対する検査
grep -rnE '/(home|Users)/[^/[:space:]"'\'']+/' .wiki-draft/
grep -rnE '([0-9]{1,3}\.){3}[0-9]{1,3}' .wiki-draft/
grep -rnEi 'SECRET_KEY=|PASSWORD=|token=|api[_-]?key' .wiki-draft/
grep -rniE '\.local\b|\.lan\b|\.internal\b' .wiki-draft/
```

本体リポジトリには `gitleaks` を再実行する。

## 3.2 報告して停止する

以下を報告し、**承認を得るまで進まない。**

1. 監査結果（本体・Wiki 下書きそれぞれ）
2. **§1.2 の公開可否判断が必要なファイルの一覧と、それぞれの内容の要約**
3. `docs/phase13-20_指示書.md` が未追跡である不整合
4. §1.4 の GitHub 設定の推奨と、判断が必要な項目
5. Wiki の下書きページ一覧と、各ページの概要
6. public 化の手順（§4）

---

# 4. public 化と Wiki の初期化

**承認後に実施する。**

## 4.1 public 化

```bash
gh repo edit --visibility public --accept-visibility-change-consequences
```

**その前に本体を push する**（§5.1）。public 化してから push すると、push の瞬間まで空のリポジトリが公開される。

## 4.2 Wiki の初期化（ユーザー作業）

**GitHub の仕様上、Wiki は最初の1ページを Web UI で作成しないと clone できない。**

ユーザーに以下を依頼すること。

1. リポジトリの Settings → Features → Wiki を有効化
2. Wiki タブ → 「Create the first page」→ 任意の内容で保存

初期化後、`https://github.com/zawa356/sluicery.wiki.git` が clone 可能になる。

## 4.3 Wiki の clone と配置

```bash
cd ..
git clone https://github.com/zawa356/sluicery.wiki.git
cp -r <本体>/.wiki-draft/* sluicery.wiki/
cd sluicery.wiki
git add -A
git commit -m "docs: 利用者向けドキュメントを追加"
```

**push は §5.2。**

---

# 5. push

## 5.1 本体リポジトリ

**承認済みであること。**

```bash
git push -u origin main
git push origin --tags
```

タグ：`checkpoint/step-01` 〜 `checkpoint/step-20`。

push 後、GitHub 上で目視確認する。

- README の表示
- ファイル一覧に機密がないこと
- Actions のワークフローが0件であること

## 5.2 Wiki リポジトリ

**Wiki への push も `CLAUDE.md` §4.1 の対象である。** 承認を得てから実行する。

```bash
cd ../sluicery.wiki
git push origin master
```

Wiki のデフォルトブランチは `master` であることが多い。`git branch` で確認してから push すること。

push 後、Wiki を目視確認する。

- サイドバーが表示されるか
- ページ間リンクが機能するか
- `docs/` へのリンクが機能するか

---

# 6. 維持のルール整備

**Wiki を作ると必ず乖離する。** これを防ぐルールを入れる。

## 6.1 `CLAUDE.md` への追記

§1（ドキュメント体系）の表に Wiki を追加する。

| ファイル | 役割 | 性質 |
|---|---|---|
| Wiki（別リポジトリ） | 利用者向けの導入ガイド・運用レシピ・FAQ | **仕様を書かない。`docs/` へリンクする** |

§2.1（更新トリガ）に追加する。

| 出来事 | 更新するファイル |
|---|---|
| **利用者から見える挙動を変えた**（UI、CLI、既定値、手順） | **Wiki の該当ページ** |

さらに、以下のルールを明記する。

> - Wiki は別リポジトリのため、本体のコミットに含まれない。**利用者から見える変更をした場合、Wiki の更新が必要かを毎回確認する**
> - Wiki に仕様を書かない。仕様は `docs/` が正であり、Wiki からはリンクする
> - Wiki への push も §4.1 の承認対象とする

## 6.2 `docs/公開前チェックリスト.md` への追記

Wiki リポジトリも監査対象である旨を追加する。本体とは別リポジトリなので、別途実行が必要。

## 6.3 `AISTATE.md`

Wiki の存在と、乖離防止のルールを「重要な合意」に1行加える。

---

# 7. 完了条件

## §1（public 化前の整理）

1. clone URL の `<repo>` が実際の URL に置換されている
2. README の検証状況が正確（`mount` 未検証を含む）
3. README に Wiki へのリンクがある

## §2（Wiki 執筆）

4. `.wiki-draft/` が `.gitignore` に追加されている
5. §2.3 の13ページが作成されている
6. **仕様の再掲がなく、`docs/` へリンクしている**
7. 実 URL・ホスト名・認証情報が含まれていない
8. 未実装・未検証のものを実装済み・検証済みと書いていない
9. サイドバーとフッターが機能する構成になっている

## §3（監査）

10. 本体と Wiki 下書きの両方を監査した
11. 公開可否の判断が必要なファイルを一覧して報告した
12. **報告して停止した**

## §4-5（公開）

13. 承認を得てから public 化・push した
14. 本体を push してから public 化した（順序）
15. Wiki が初期化され、内容が push されている
16. GitHub 上で目視確認した

## §6（維持）

17. `CLAUDE.md` に Wiki の位置づけと更新トリガが追記されている
18. `docs/公開前チェックリスト.md` に Wiki の監査が追記されている
19. `AISTATE.md` が更新されている

---

# 8. 実装時の注意（再掲）

- **§3 の監査後に必ず停止する。** 承認なしに public 化・push しない
- **本体を push してから public 化する**
- **Wiki に仕様を書かない。** 「コード変更時に直す必要があるか」で判断する
- **Wiki への push も承認対象**
- **実 URL・ホスト名・認証情報を書かない**
- **未実装・未検証を正直に書く**（`mount`、Jellyfin 連携、トランスコード）
- **`docs/` の内容をコピーせずリンクする**
- 画像は初版に含めない。`<!-- TODO: スクリーンショット -->` を残す
- 判断に迷ったら要件定義 §1.4 の設計原則に照らす
