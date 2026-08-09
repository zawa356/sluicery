# Phase 3.5 実装指示書 — ドキュメント整備・VM 実機検証・GitHub 公開

| 項目 | 内容 |
|---|---|
| 対象 | 要件定義 §20 の実装順序には含まれない、独立タスク |
| 前提 | Phase 3（yt-dlp venv 管理・CLI ラッパ）完了済み |
| 作成日 | 2026-08-09 |

Phase 3.5 は機能実装ではない。目的は2つ。

1. **初見者が構築できる状態にする**（README・デプロイ文書・トラブルシューティング）
2. **汚れていない環境で受入条件1を検証し、GitHub で公開する**

**§7 の履歴監査で一度停止し、ユーザーの承認を得るまでプッシュしないこと。**

---

# 0. 着手前の是正タスク（P0）

## 0.1 `AISTATE.md` の更新（優先度：最高）

**Phase 3 完了後に更新されていない。** 内容が Phase 2 時点のままになっている。CLAUDE.md §8.3 の必須手順であり、Phase 3 指示書の完了条件16でもある。

特に以下は**次セッションに誤った前提を与える**ため、確実に直すこと。

| 箇所 | 現状 | 修正 |
|---|---|---|
| 進捗リスト | #3 が未チェック、「← 次の着手点」 | #3 をチェック済みにし、次の着手点を #4 へ |
| 対応コミット | Phase 2 のもの（7618e71） | Phase 3 の最終コミット |
| 直近の作業 | Phase 2 の内容 | Phase 3 の内容に全文書き換え |
| 既知の落とし穴 | 「worker は起動→即終了→再起動を繰り返す。異常ではない」 | **D-013 で修正済み。この記述は誤り。削除すること** |
| 環境メモ | テスト28件、Phase 2 時点の CLI | Phase 3 の実績値と `ytdlp` サブコマンドを反映 |

「重要な前提」に Phase 3 の知見を追加すること。最低限：

- yt-dlp は `LC_ALL=C` で実行する（エラー分類が英語メッセージに依存）
- yt-dlp のプロセスはグループ単位で終了させる（ffmpeg が孤児化する）
- `ytdlp install` は `current` を切り替えない。切替は `ytdlp use`（初回導入時のみ例外。D-012）
- 未知の yt-dlp エラーは `failed` に分類する（D-014）
- venv への書き込みは `app` のみ。worker は読み取り専用

## 0.2 受入試験結果の記録

Phase 3 指示書 §11.2 の20項目について、実行結果が記録されていない（完了条件15）。

`docs/変更履歴.md` または `AISTATE.md` に、**20項目それぞれの通過状況を記録すること。** 特に実機でしか確認できない以下は、確認済みか未確認かを明示すること。

- #12：ネットワーク遮断時に `blocked` に分類される（`failed` ではない）
- #14：タイムアウト後に ffmpeg / yt-dlp の孤児プロセスが残らない
- #17：`current` symlink を壊した状態で `broken` と報告され、自動修復を試みない
- #20：ログでクレデンシャル・Cookie パスがマスクされている

未実施の項目があれば、**Phase 3.5 の VM 検証（§5）と併せて実施する。**

## 0.3 検討事項の AISTATE への登録

`docs/基本設計.md` に「検討事項（未決定）」が2件あるが、§3 と §7 の末尾に散在しており、AISTATE の未解決表に載っていない。Phase 6 着手時に見落とすリスクがある。

AISTATE の「未解決・保留」表に以下を追加すること。

| 内容 | 参照 |
|---|---|
| `TaskStatus` に `blocked` 相当を持たせるかが未決定。Phase 6/7 で Storage 到達不能時の Task 表現を決める | 基本設計 §3 |
| `compose.yaml` に `init: true` を入れるかが未決定。`killpg` 後の孫プロセスが zombie として残る可能性。Phase 6 で評価 | 基本設計 §7 末尾 |

## 0.4 軽微

- `docs/footprint.md`：`sluicery:local-test` イメージを `make purge` の削除対象に含めるか、含めない理由を明記する（現在は「手動で `docker rmi` すること」とだけ書かれている）

---

# 1. スコープ

## 1.1 含むもの

1. `README.md` の全面改訂（Quick Start 先頭化、前提条件の整備）
2. `docs/deployment.md` の新設（VM 前提の詳細手順）
3. `docs/troubleshooting.md` の新設
4. **VM への新規構築検証**
5. `LICENSE` の配置
6. `CLAUDE.md` §4.1 の改訂（リモート git 操作の条件付き解禁）
7. `docs/公開前チェックリスト.md` の新設
8. **履歴監査の実行と結果報告（ここで停止）**
9. 承認後の GitHub 公開

## 1.2 含まないもの

- 機能実装（Phase 4 以降）
- LXC 環境への対応（**「未検証」と明記して先送り**）
- CI / GitHub Actions の設定（公開時は Actions を無効化する）
- Docker イメージの配布（ソース公開のみ）

---

# 2. README の全面改訂

## 2.1 構成

初見者の判断順序に合わせて並べ替える。

```
# sluicery
  1〜2行の説明

## Quick Start           ← コピペで動く最短手順
## これは何か / 何ではないか
## 前提条件
## 初期設定
## 運用コマンド
## トラブルシューティング（docs/troubleshooting.md へのリンク）
## ドキュメント
## ライセンス
```

## 2.2 Quick Start

**コピペで動くこと。説明を挟まない。** 詳細は後続セクションに送る。

```bash
git clone <repo> sluicery
cd sluicery
cp .env.example .env

# SECRET_KEY を生成して .env に設定
docker run --rm python:3.12-slim python -c \
  "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

$EDITOR .env
docker compose up -d --build
```

`make` を前提にしないこと。初見者の環境に `make` があるとは限らない。**`make` を使うコマンドは併記に留め、Quick Start は `docker compose` 直で書く。**

起動確認まで含めること（`curl http://localhost:8080/healthz` など）。

## 2.3 「これは何か / 何ではないか」

**制約を早期に伝える。** 読者が5分無駄にする前に、用途が合うかを判断できるようにする。

「何ではないか」に必ず含めること：

- 外部公開・多人数利用は非対応（単一ユーザー）
- DRM 保護コンテンツは対象外
- 配信元での削除に追従してローカルファイルを削除する機能はない（これは仕様であり、意図的な設計）
- メディアサーバー（Jellyfin / Navidrome）との連携は未実装
- トランスコードは未実装

## 2.4 前提条件

以下を網羅すること。

**動作環境**

| 項目 | 内容 |
|---|---|
| OS | Linux x86_64。cgroup v2 |
| Docker | Docker Engine と Compose v2 の最低バージョン（実機で確認した値を書く） |
| ストレージドライバ | `overlay2` であること。確認コマンド `docker info \| grep "Storage Driver"` |
| ポート | 8080（`.env` で変更可）が空いていること |

**容量とリソース**

- イメージのサイズ（**実測値**を書く）
- **Staging 領域の必要量** = 取得する最大ファイルサイズ × 並列度 + 余裕。ここを明示しないと確実に詰まる
- メディア保存先の容量
- メモリの目安（**実測値**を書く）

**ネットワーク**

- 外向き HTTPS：PyPI（yt-dlp の導入・更新）、取得対象サイト
- 時刻同期（cron 式の解釈とタイムゾーンの整合）

**事前に用意するもの**

- `SECRET_KEY`（生成方法を明記）
- `MEDIA_ROOT` ディレクトリの**事前作成**。Docker に作らせると root 所有になる（`docs/footprint.md` 参照）
- `PUID` / `PGID` の確認方法（`id`）

**検証状況**

正直に書くこと。

| 環境 | 状況 |
|---|---|
| 一般的な Linux VM | 検証済み（バージョンを明記） |
| Proxmox LXC | **未検証**。`nesting=1,keyctl=1` が必要になる見込み |
| WSL2 | 未検証 |
| NAS（Synology / QNAP 等） | 未検証 |

## 2.5 その他

- 運用コマンド表は現行のものを維持しつつ、`make` 無し環境向けの等価コマンドを併記する
- 「現在の状態」セクションは維持する。公開リポジトリでは開発途上であることを明示するのが誠実

---

# 3. `docs/deployment.md` の新設

README には要点のみを置き、詳細はこちらへ。

## 3.1 内容

1. **一般的な Linux VM への構築手順**（本命）
 - OS の準備、Docker のインストール
 - ディスクレイアウトの推奨（root と データ用を分ける）
 - `MEDIA_ROOT` の事前作成と所有者設定
 - 起動、初回ログイン、yt-dlp の導入確認
2. **`.env` の全項目リファレンス**（`.env.example` のコメントと重複してよい）
3. **Staging 容量の見積もり方**
4. **リバースプロキシ経由での公開**（HTTPS 終端はアプリの責務外。要件 §12）
5. **バックアップとリストア**
6. **アンインストール**（`make purge` の挙動、残るもの・残らないもの）
7. **LXC 環境について**：現時点で未検証である旨と、必要になる設定の見込み（`nesting=1,keyctl=1`、非特権では UID オフセット +100000、`mount` kind は利用不可）を**参考情報として**記載する。検証していないことを明記すること

## 3.2 記載しないこと

- 検証していない内容を、検証済みであるかのように書かない
- 特定の NAS 製品固有の手順（未検証のため）

---

# 4. `docs/troubleshooting.md` の新設

VM 検証（§5）で実際に踏んだ問題を、そのまま初見者向けの記述に落とす。**想像で書かない。**

想定される項目（実際に踏んだものだけ書く）：

- `SECRET_KEY` 未設定で起動しない
- `MEDIA_ROOT` の権限で書き込めない（root 所有で自動作成された場合）
- ポート競合
- yt-dlp が導入されない（ネットワーク、PyPI 到達性）
- `ytdlp status` が `broken` になる
- ストレージドライバが `vfs` でビルドが遅い
- マイグレーションが適用されない（`AUTO_MIGRATE=false` の場合）

各項目は「症状 → 原因 → 対処」の形式で書くこと。

---

# 5. VM への新規構築検証

**Phase 3.5 の実質的な本体。** 開発機は既に環境が整ってしまっているため、初見者と同じ条件で通るかはここでしか分からない。

## 5.1 前提

- ユーザーが用意した VM（Debian 12/13 または Ubuntu 24.04 LTS 想定）
- **開発機からファイルをコピーしない。** `git clone`（または GitHub 公開後の clone）から始める
- 公開前の段階では、リポジトリを VM へ持ち込む手段はユーザーと相談する

## 5.2 検証項目

| # | 手順 | 期待結果 |
|---|---|---|
| 1 | 環境情報を採取（OS、カーネル、Docker バージョン、ストレージドライバ、cgroup バージョン） | **README の前提条件に実測値として反映する** |
| 2 | `git clone` → `cp .env.example .env` → `SECRET_KEY` 設定 → `docker compose up -d --build` | **受入条件1。追加手順なしで起動すること** |
| 3 | ビルド時間とイメージサイズを計測 | README に実測値として記載 |
| 4 | `curl http://localhost:8080/healthz` | 200 |
| 5 | 3コンテナの状態を確認 | app が healthy、worker が待機状態（**restart ループでない**） |
| 6 | `sluicery config check` | 全項目の検証結果が表示され、シークレットがマスクされている |
| 7 | `sluicery ytdlp status` | `not_installed` または自動導入により `ready` |
| 8 | `sluicery ytdlp install` → `status` | `ready` |
| 9 | `sluicery ytdlp probe <D-015 の URL>` | メタデータが取得できる |
| 10 | `sluicery ytdlp fetch <D-015 の URL>` | Staging にファイルが生成され、進捗が表示される |
| 11 | Phase 3 受入試験のうち §0.2 で未実施だった項目 | 全て通過 |
| 12 | メモリ使用量を計測 | README に実測値として記載 |
| 13 | `docker compose down` → `up -d` | 状態が保持されている |
| 14 | `MEDIA_ROOT` を既定以外に設定して再構築 | 起動する（D-010 の回帰確認。LXC/VM で bind mount の段数が変わっても問題ないこと） |
| 15 | 検証中に踏んだ問題を記録 | `docs/troubleshooting.md` へ反映 |

## 5.3 記録

環境情報と実測値を `docs/deployment.md` に「検証環境」として記録すること。バージョンを明記し、いつ時点の検証かが分かるようにする。

---

# 6. 公開準備

## 6.1 LICENSE

**着手前にユーザーへライセンスを確認すること。** 未指定のリポジトリは「全権利留保」扱いになり、公開する意味が薄れる。

参考情報として、周辺の依存は yt-dlp が Unlicense、rclone が MIT。ソース配布のみであれば ffmpeg の GPL/LGPL の影響は受けない（Docker イメージの配布を始めると話が変わる）。

確定後、`LICENSE` をリポジトリルートに配置し、README にライセンスセクションを追加する。

## 6.2 `CLAUDE.md` §4.1 の改訂

現在の §4.1 はリモート git 操作を全面禁止している。以下の方針に置き換える。

**引き続き禁止するもの**

- `git push --force` を含む一切の force 操作
- 外部リポジトリからの `git clone` / `fetch` / `pull`（自リポジトリを除く）
- `git reset --hard` / `git clean -fdx` / コミット済み履歴の `rebase`（ユーザーの明示的な指示がある場合のみ可）

**条件付きで許可するもの**

- `git push`：**`docs/公開前チェックリスト.md` の監査を完了し、ユーザーの明示的な承認を得た後にのみ実行する**
- `gh repo create` / `gh repo edit`：同上

改訂の根拠として、本書へのリンクを §4.1 に残すこと。

## 6.3 `docs/公開前チェックリスト.md` の新設

§7 の監査手順を手順書化する。**以後のプッシュでも毎回使う**前提で書くこと。

---

# 7. 履歴監査

## 7.1 原則

**GitHub にプッシュした時点で履歴全体が公開される。** 作業ツリーがクリーンでも、過去のコミットに機密が含まれていれば公開される。フォーク、クローン、各種アーカイブサービスに残るため、後から消すことは実質的に不可能。

**したがって監査は作業ツリーではなく履歴全体に対して行う。**

## 7.2 監査対象

**機密**

- `.env`（`SECRET_KEY`、`ADMIN_PASSWORD`）
- Fernet 鍵、その指紋
- Storage の認証情報
- Cookie ファイル
- SQLite DB 本体（クレデンシャルが暗号化済みでも、鍵とセットで漏れれば復号可能）

**個人情報・環境情報**

- 実際のホスト名、内部 IP、NAS の共有名
- **実際に取得したプレイリスト URL や動画 URL**（視聴履歴に相当する）
- ホームディレクトリのパス（ユーザー名が含まれる）
- **受入試験のログや実行結果**（Phase 3 §11、Phase 3.5 §5 で実機ログを扱っている）
- AISTATE・変更履歴・基本設計に書かれた環境固有の記述

**見落としやすいもの**

- `.env.example` に実値が書き込まれていないか
- テストフィクスチャに実 URL が入っていないか
- ドキュメント内のコマンド例に実パス・実ホスト名が残っていないか
- `docs/phase*_指示書.md` と `AISTATE.md` を公開してよいか（内容自体に問題がなくても、公開の是非はユーザーの判断）

## 7.3 監査手順

```bash
# 1. 履歴に一度でも追加された全ファイルパス
git log --all --pretty=format: --name-only --diff-filter=A | sort -u

# 2. 危険なファイル名が履歴にあるか
git log --all --pretty=format: --name-only | sort -u \
  | grep -Ei '\.env$|\.db$|cookie|credential|\.key$|\.pem$|\.sqlite'

# 3. 全履歴の全差分から機密パターンを検索
git log --all -p | grep -nEi 'SECRET_KEY=|PASSWORD=|BEGIN .*PRIVATE KEY|token=|api[_-]?key'

# 4. 環境固有情報の検索（実ホスト名・IP・ホームパス）
git log --all -p | grep -nE '192\.168\.|10\.[0-9]+\.|/home/[a-z]+/|\.local\b'

# 5. 現在の作業ツリーで除外が効いているか
git status --ignored
git check-ignore -v .env data/sluicery.db
```

加えて **`gitleaks` を1回通すこと。** 目視だけでは必ず漏れる。

```bash
docker run --rm -v "$(pwd):/repo" zricethezav/gitleaks:latest detect --source=/repo --verbose
```

## 7.4 結果の報告と停止

**監査結果をユーザーに報告し、そこで停止すること。** 承認なしにプッシュしない。

報告に含めること：

- 実行した監査手順と、それぞれの結果
- 検出された項目（あれば）と、その深刻度の判断
- 履歴のコミット数、タグ一覧
- 公開してよいか判断が必要な項目（`AISTATE.md` や指示書の公開可否など）

## 7.5 混入が見つかった場合

**判断はユーザーが行う。勝手に履歴を書き換えない。**

以下の判定を提示すること。

| ケース | 対応 |
|---|---|
| 環境固有情報のみ（ホスト名、パス、実 URL） | `git filter-repo` で該当箇所を除去。**コミット数と構造は保たれる**。ハッシュが変わるためタグは付け直す |
| 実際の秘密（`SECRET_KEY`、パスワード、トークン） | まず**その秘密を破棄する**ことが最優先。検証環境の `SECRET_KEY` なら再生成すれば実害はない（ローテーション非対応のため Storage 認証情報の再入力が必要）。その上で `filter-repo` で除去 |
| 履歴が広範に汚染されている | 履歴を捨てて現在の状態から新規リポジトリとして作り直す。**最後の手段** |

**コミットポイントは可能な限り残す方針。** 作り直しは `filter-repo` で処理しきれない場合に限る。

---

# 8. GitHub 公開手順

**§7 の承認を得た後にのみ実行する。**

## 8.1 手順

```bash
# 1. 認証確認
gh auth status

# 2. private で作成し、remote を追加（この時点ではプッシュしない）
gh repo create sluicery --private --source=. --remote=origin \
  --description "yt-dlp を用いた自己ホスト型のプレイリスト同期サーバー"

# 3. プッシュ
git push -u origin main
git push origin --tags

# 4. GitHub 上で内容を目視確認（README の表示、ファイル一覧、Actions が無効か）

# 5. 問題なければ public 化
gh repo edit --visibility public --accept-visibility-change-consequences
```

**private で作成する理由**：見落としがあった場合の逃げ道を残すため。public 化はワンクリックなのでコストはほぼゼロ。

## 8.2 リポジトリ設定

- **Actions を無効化する**（意図しないワークフロー実行を防ぐ）
- Issues / Wiki / Projects の要否をユーザーに確認する
- Dependabot alerts の要否をユーザーに確認する
- デフォルトブランチが `main` であることを確認する

## 8.3 公開後

- README の `git clone <repo>` を実際の URL に差し替える
- `docs/deployment.md` の clone 手順も同様
- 差し替え後のコミットをプッシュする

---

# 9. コミット計画

| # | コミット |
|---|---|
| 1 | `docs: Phase 3 完了に伴い AISTATE と受入試験結果を更新`（§0.1, §0.2, §0.3） |
| 2 | `docs: README を全面改訂（Quick Start 先頭化、前提条件の整備）`（§2） |
| 3 | `docs: deployment.md を新設`（§3） |
| 4 | `docs: CLAUDE.md のリモート git 操作ルールを改訂`（§6.2） |
| 5 | `docs: 公開前チェックリストを新設`（§6.3） |
| 6 | `chore: LICENSE を配置`（§6.1） |
| 7 | `docs: VM 実機検証の結果を反映（実測値・troubleshooting）`（§5, §4） |
| 8 | （必要なら）`fix: VM 検証で判明した問題を修正` |

**§7 の監査はコミットを伴わない。** 監査後、承認を得てからプッシュする。

完了後、`checkpoint/step-03.5` タグを打つ。

---

# 10. 完了条件

1. §0 の是正タスクがすべて完了している（特に AISTATE の更新）
2. README の Quick Start が、`make` 無しでコピペ実行できる
3. README に「何ではないか」が上部に記載されている
4. 前提条件に**実測値**（イメージサイズ、メモリ、ビルド時間）が入っている
5. 検証状況の表に、未検証環境が正直に記載されている
6. `docs/deployment.md` / `docs/troubleshooting.md` が存在する
7. `docs/troubleshooting.md` の内容が、**実際に踏んだ問題**に基づいている
8. VM 上で `git clone` → `cp .env.example .env` → `SECRET_KEY` 設定 → `docker compose up -d --build` が**追加手順なしで**成功する
9. VM 上で `ytdlp fetch` が成功し、Staging にファイルが生成される
10. `LICENSE` が配置され、README にライセンスセクションがある
11. `CLAUDE.md` §4.1 が改訂されている
12. `docs/公開前チェックリスト.md` が存在する
13. **履歴監査が実行され、結果が報告されている**
14. ユーザーの承認後、GitHub に private で作成・プッシュされている
15. Actions が無効化されている
16. public 化されている
17. README の clone URL が実際のものに差し替えられている
18. `AISTATE.md` が更新され、Phase 4 の着手点が書かれている

---

# 11. ドキュメント更新義務

| ファイル | 内容 |
|---|---|
| `README.md` | 全面改訂 |
| `docs/deployment.md` | 新設 |
| `docs/troubleshooting.md` | 新設 |
| `docs/公開前チェックリスト.md` | 新設 |
| `CLAUDE.md` §4.1 | リモート git 操作ルールの改訂 |
| `CLAUDE.md` §1 | ドキュメント体系の表に新設ファイルを追加 |
| `docs/基本設計.md` §7 | 公開に関する設計判断（ライセンス選定、private→public の手順）を D-016 以降として記録 |
| `docs/変更履歴.md` | 未リリース欄に追加項目と VM 検証結果 |
| `docs/storage.md` | LXC 環境について「未検証」の注記（§3.1 の 7 と整合させる） |
| `docs/footprint.md` | §0.4 の修正 |
| `LICENSE` | 新設 |
| `AISTATE.md` | 全文書き換え（着手前と完了後の2回） |

---

# 12. 実装時の注意（再掲）

- **監査は履歴全体に対して行う。** 作業ツリーだけ見ても意味がない
- **監査後に停止し、承認を得るまでプッシュしない**
- **private で作成してから public 化する**
- **履歴に混入があっても勝手に書き換えない。** 判断を仰ぐ
- **コミットポイントは可能な限り残す。** 作り直しは最後の手段
- **troubleshooting は実際に踏んだ問題だけ書く。** 想像で書かない
- **README の実測値は実際に計測した値を書く。** 推測値を書かない
- **未検証の環境を検証済みのように書かない**
- 判断に迷ったら要件定義 §1.4 の設計原則に照らし、それでも決まらなければ**確認を取る**
