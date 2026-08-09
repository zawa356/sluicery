# Phase 3.5 実装指示書 — ドキュメント整備・VM 実機検証・GitHub 公開

| 項目 | 内容 |
|---|---|
| 対象 | 要件定義 §20 の実装順序には含まれない、独立タスク |
| 前提 | Phase 3（yt-dlp venv 管理・CLI ラッパ）完了済み（実機検証20項目通過済み） |
| 作成日 | 2026-08-09（改訂版） |

Phase 3.5 は機能実装ではない。目的は2つ。

1. **初見者が構築できる状態にする**（README・デプロイ文書・トラブルシューティング）
2. **汚れていない環境で受入条件1を検証し、GitHub で公開する**

**§7 の履歴監査で一度停止し、ユーザーの承認を得るまでプッシュしないこと。**

---

# 0. 着手前の是正タスク（P0）

公開前に必ず処理する。**特に §0.1 は機密漏洩に直結する。**

## 0.1 `.gitignore` に `backups/` と Windows 由来ファイルを追加（優先度：最高）

**現在の `.gitignore` に `backups/` が無い。** `make backup` は `./backups/` にアーカイブを書き出し、そのアーカイブには **Storage の認証情報が（暗号化された形で）含まれる**（`docs/legal.md` 記載）。`SECRET_KEY` とセットで漏れれば復号可能であり、公開リポジトリに混入すると実害が出る。

加えて、WSL / Windows 環境でファイルを扱うと `<filename>:Zone.Identifier` という代替データストリーム由来のファイルが生成されることがある。実際に本プロジェクトのファイル群でも発生が確認されている。

以下を追加すること。

```gitignore
# ---- バックアップ（クレデンシャルを含む） ----
/backups/
*.tar.gz

# ---- Windows / WSL 由来 ----
*:Zone.Identifier
```

追加後、**既に履歴に混入していないかを §7 の監査で確認すること。**

## 0.2 `make lint` が lock ファイルを使っていない

```makefile
lint:
	docker run --rm ... bash -c "pip install -r requirements-dev.in && ruff check ..."
```

`requirements-dev.in`（バージョン非固定）からインストールしているため、実行時期によって ruff / mypy のバージョンが変わり、**同じコードで結果が変わる**。要件定義 §1.4 原則5（再現性）に反する。

`make test` と同様に `sluicery:local-test` イメージを使う形に変更すること。

```makefile
lint:
	docker build --target test -t sluicery:local-test .
	docker run --rm --entrypoint ruff sluicery:local-test check src tests
	docker run --rm --entrypoint mypy sluicery:local-test src
```

## 0.3 `make lock` / `make lint` のベースイメージが digest 固定されていない

Dockerfile では `python:3.12-slim` を digest でピン留めしているのに、Makefile 内の `docker run python:3.12-slim` は可変タグを使っている。整合が取れていない。

`make lint` は §0.2 で `sluicery:local-test` に切り替わるため解消する。`make lock` については、Dockerfile と同じ digest を参照する変数を Makefile に定義するか、少なくとも digest を直書きすること。

## 0.4 `make purge` が `sluicery:local-test` を削除しない

`docs/footprint.md` に「手動で `docker rmi` すること」と記載されているが、`make purge` は「残骸を残さない」（要件定義 §1.4 原則3）ためのコマンドである。手動作業を残すのは趣旨に反する。

`make purge` の削除対象に含め、削除対象の事前表示にも出すこと。`docs/footprint.md` の該当記述も更新する。

## 0.5 `.dockerignore` の作成

現在の Dockerfile は個別 `COPY` を使っているため機密がイメージに入ることはないが、ビルドコンテキストには `.git` / `backups/` / `data/` などが送られており、ビルドが無駄に遅くなる。

```
.git
.gitignore
backups/
data/
staging/
media/
logs/
docs/
*.md
!README.md
__pycache__/
.venv/
.pytest_cache/
.mypy_cache/
.ruff_cache/
```

`docs/footprint.md` に `.dockerignore` の存在を追記すること。

## 0.6 `AISTATE.md` の「重要な前提」の更新

現在「push などのリモート git 操作は禁止」と記載されているが、§6.2 で `CLAUDE.md` §4.1 を改訂するため、これと矛盾する。**改訂後の内容に合わせて書き換えること**（条件付き許可であること、監査と承認が前提であること）。

---

# 1. スコープ

## 1.1 含むもの

1. §0 の是正
2. `README.md` の全面改訂（Quick Start 先頭化、前提条件の整備）
3. `docs/deployment.md` の新設（VM 前提の詳細手順）
4. `docs/troubleshooting.md` の新設
5. **VM への新規構築検証**
6. `LICENSE` の配置
7. `CLAUDE.md` §4.1 の改訂（リモート git 操作の条件付き解禁）
8. `docs/公開前チェックリスト.md` の新設
9. **履歴監査の実行と結果報告（ここで停止）**
10. 承認後の GitHub 公開

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

`make` を前提にしないこと。初見者の環境に `make` があるとは限らない。**Quick Start は `docker compose` 直で書き、`make` は併記に留める。**

```bash
git clone <repo> sluicery
cd sluicery
cp .env.example .env

# SECRET_KEY を生成
docker run --rm python:3.12-slim python -c \
  "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

$EDITOR .env          # SECRET_KEY を貼り付ける
docker compose up -d --build

# 起動確認
curl http://localhost:8080/healthz
```

yt-dlp の導入確認まで含めること（`ytdlp status` が `ready` になるまで）。

## 2.3 「これは何か / 何ではないか」

**制約を早期に伝える。** 読者が5分無駄にする前に、用途が合うかを判断できるようにする。

「何ではないか」に必ず含めること：

- 外部公開・多人数利用は非対応（単一ユーザー）
- DRM 保護コンテンツは対象外
- 配信元での削除に追従してローカルファイルを削除する機能はない（**仕様であり、意図的な設計**）
- メディアサーバー（Jellyfin / Navidrome）との連携は未実装
- トランスコードは未実装
- **現在は開発途上であり、Web UI は未実装**（Phase 9 以降）。CLI のみで操作する段階であることを正直に書く

## 2.4 前提条件

**動作環境**

| 項目 | 内容 |
|---|---|
| OS | Linux x86_64。cgroup v2 |
| Docker | Docker Engine と Compose v2 の最低バージョン（**VM 検証で確認した実測値**を書く） |
| ストレージドライバ | `overlay2` であること。確認コマンド `docker info \| grep "Storage Driver"` |
| ポート | 8080（`.env` で変更可）が空いていること |

**容量とリソース**

- イメージのサイズ（**実測値**）
- **Staging 領域の必要量** = 取得する最大ファイルサイズ × 並列度 + 余裕。ここを明示しないと確実に詰まる
- メディア保存先の容量
- メモリの目安（**実測値**）

**ネットワーク**

- 外向き HTTPS：PyPI（yt-dlp の導入・更新）、取得対象サイト
- 時刻同期（cron 式の解釈とタイムゾーンの整合）

**事前に用意するもの**

- `SECRET_KEY`（生成方法を明記。紛失時の影響も1行で）
- `MEDIA_ROOT` ディレクトリの**事前作成**。Docker に作らせると root 所有になる
- `PUID` / `PGID` の確認方法（`id`）

**検証状況**

正直に書くこと。

| 環境 | 状況 |
|---|---|
| 一般的な Linux VM | 検証済み（ディストリビューションとバージョンを明記） |
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
   - ディスクレイアウトの推奨（root とデータ用を分ける）
   - `MEDIA_ROOT` の事前作成と所有者設定
   - 起動、yt-dlp の導入確認
2. **`.env` の全項目リファレンス**
3. **Staging 容量の見積もり方**
4. **リバースプロキシ経由での公開**（HTTPS 終端はアプリの責務外。要件定義 §12）
5. **バックアップとリストア**（`backups/` にクレデンシャルが含まれる点を明記）
6. **アンインストール**（`make purge` の挙動、残るもの・残らないもの）
7. **検証環境の記録**（§5.3）
8. **LXC 環境について**：現時点で未検証である旨と、必要になる設定の見込み（`nesting=1,keyctl=1`、非特権では UID オフセット +100000、`mount` kind は利用不可）を**参考情報として**記載する。**検証していないことを明記すること**

## 3.2 記載しないこと

- 検証していない内容を、検証済みであるかのように書かない
- 特定の NAS 製品固有の手順（未検証のため）

---

# 4. `docs/troubleshooting.md` の新設

VM 検証（§5）で実際に踏んだ問題を、そのまま初見者向けの記述に落とす。**想像で書かない。**

Phase 3 の実機検証で既に判明している以下は、初見者も踏みうるので含めてよい（AISTATE の「既知の落とし穴」から転記）。

- `ytdlp status` が `broken` になる（→ `ytdlp install --force`）
- `docker compose down -v` で volume が消える

VM 検証で新たに踏んだものを追加すること。想定される項目：

- `SECRET_KEY` 未設定で起動しない
- `MEDIA_ROOT` の権限で書き込めない（root 所有で自動作成された場合）
- ポート競合
- yt-dlp が導入されない（ネットワーク、PyPI 到達性）
- ストレージドライバが `vfs` でビルドが遅い
- マイグレーションが適用されない（`AUTO_MIGRATE=false` の場合）

各項目は「症状 → 原因 → 対処」の形式で書くこと。

---

# 5. VM への新規構築検証

**Phase 3.5 の実質的な本体。** 開発機は既に環境が整ってしまっているため、初見者と同じ条件で通るかはここでしか分からない。

## 5.1 前提

- ユーザーが用意した VM（Debian 12/13 または Ubuntu 24.04 LTS 想定）
- **開発機からファイルをコピーしない。** `git clone` から始める
- 公開前の段階でリポジトリを VM へ持ち込む手段は、ユーザーと相談する

## 5.2 検証項目

| # | 手順 | 期待結果 |
|---|---|---|
| 1 | 環境情報を採取（OS、カーネル、Docker バージョン、ストレージドライバ、cgroup バージョン、メモリ、ディスク） | **README / deployment.md に実測値として反映する** |
| 2 | `git clone` → `cp .env.example .env` → `SECRET_KEY` 設定 → `docker compose up -d --build` | **受入条件1。追加手順なしで起動すること** |
| 3 | ビルド時間とイメージサイズを計測 | 実測値を記録 |
| 4 | `curl http://localhost:8080/healthz` | 200 |
| 5 | 3コンテナの状態を確認 | app が healthy、worker が待機状態（**restart ループでない**） |
| 6 | `sluicery config check` | 全項目の検証結果が表示され、シークレットがマスクされている |
| 7 | `sluicery ytdlp status` | 自動導入により `ready`（または `not_installed` から `install` で `ready`） |
| 8 | `sluicery ytdlp probe <D-015 の URL>` | メタデータ・フォーマットが取得できる |
| 9 | `sluicery ytdlp fetch <D-015 の URL>` | Staging にファイルが生成され、**進捗が表示される** |
| 10 | メモリ使用量を計測（起動直後、ダウンロード中） | 実測値を記録 |
| 11 | `docker compose down` → `docker compose up -d` | 状態が保持されている。yt-dlp の再導入が発生しない |
| 12 | `MEDIA_ROOT` を既定以外に設定して再構築 | 起動する（D-010 の回帰確認） |
| 13 | `make test` | コンテナ内で全件パス |
| 14 | `make lint` | §0.2 の修正後、lock ファイル由来のバージョンで実行される |
| 15 | `make purge` → 削除対象の表示を確認 | `sluicery:local-test` を含む（§0.4）。`MEDIA_ROOT` 配下が残る |
| 16 | `docker images` / `docker volume ls` で残骸確認 | `docs/footprint.md` の記載と一致する |
| 17 | 検証中に踏んだ問題を記録 | `docs/troubleshooting.md` へ反映 |

**#15・#16 が重要です。** 要件定義 §1.4 原則3（残骸を残さない）を、実際にクリーンな環境で検証できるのはここだけです。

## 5.3 記録

環境情報と実測値を `docs/deployment.md` に「検証環境」として記録すること。バージョンと検証日を明記する。

---

# 6. 公開準備

## 6.1 LICENSE

**着手前にユーザーへライセンスを確認すること。** 未指定のリポジトリは「全権利留保」扱いになり、公開する意味が薄れる。

参考情報として、周辺の依存は yt-dlp が Unlicense、rclone が MIT。ソース配布のみであれば ffmpeg の GPL/LGPL の影響は受けない（Docker イメージの配布を始めると話が変わる）。

確定後、`LICENSE` をリポジトリルートに配置し、README にライセンスセクションを追加する。

## 6.2 `CLAUDE.md` §4.1 の改訂

現在の §4.1 はリモート git 操作を全面禁止している。以下に置き換える。

**引き続き禁止するもの**

- `git push --force` を含む一切の force 操作
- 外部リポジトリからの `git clone` / `fetch` / `pull`（自リポジトリを除く）
- `git reset --hard` / `git clean -fdx` / コミット済み履歴の `rebase`（ユーザーの明示的な指示がある場合のみ可）

**条件付きで許可するもの**

- `git push`：**`docs/公開前チェックリスト.md` の監査を完了し、ユーザーの明示的な承認を得た後にのみ実行する**
- `gh repo create` / `gh repo edit`：同上

改訂の根拠として、本書と `docs/公開前チェックリスト.md` へのリンクを §4.1 に残すこと。

**`AISTATE.md` の「重要な前提」も併せて更新すること**（§0.6）。

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
- SQLite DB 本体
- **`backups/` 配下のアーカイブ**（§0.1。クレデンシャルを含む）

**個人情報・環境情報**

- 実際のホスト名、内部 IP、NAS の共有名
- **実際に取得したプレイリスト URL や動画 URL**（視聴履歴に相当する）
- ホームディレクトリのパス（ユーザー名が含まれる）
- **実機検証のログや実行結果**（Phase 3 §11、本書 §5 で実機ログを扱っている）
- AISTATE・変更履歴・基本設計に書かれた環境固有の記述

**見落としやすいもの**

- `.env.example` に実値が書き込まれていないか
- テストフィクスチャに実 URL が入っていないか
- ドキュメント内のコマンド例に実パス・実ホスト名が残っていないか
- **`*:Zone.Identifier` ファイル**（§0.1）
- `docs/phase*_指示書.md` と `AISTATE.md` を公開してよいか（内容自体に問題がなくても、公開の是非はユーザーの判断）

## 7.3 監査手順

```bash
# 1. 履歴に一度でも追加された全ファイルパス
git log --all --pretty=format: --name-only --diff-filter=A | sort -u

# 2. 危険なファイル名が履歴にあるか
git log --all --pretty=format: --name-only | sort -u \
  | grep -Ei '\.env$|\.db$|cookie|credential|\.key$|\.pem$|\.sqlite|\.tar\.gz$|Zone\.Identifier'

# 3. 全履歴の全差分から機密パターンを検索
git log --all -p | grep -nEi 'SECRET_KEY=|PASSWORD=|BEGIN .*PRIVATE KEY|token=|api[_-]?key'

# 4. 環境固有情報の検索（実ホスト名・IP・ホームパス）
git log --all -p | grep -nE '192\.168\.|10\.[0-9]+\.|172\.(1[6-9]|2[0-9]|3[01])\.|/home/[a-z]+/|/mnt/c/'

# 5. 現在の作業ツリーで除外が効いているか
git status --ignored
git check-ignore -v .env backups/ data/sluicery.db
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

| ケース | 対応 |
|---|---|
| 環境固有情報のみ（ホスト名、パス、実 URL、`Zone.Identifier`） | `git filter-repo` で該当箇所を除去。**コミット数と構造は保たれる**。ハッシュが変わるためタグは付け直す |
| 実際の秘密（`SECRET_KEY`、パスワード、バックアップアーカイブ） | まず**その秘密を破棄する**ことが最優先。検証環境の `SECRET_KEY` なら再生成すれば実害はない（ローテーション非対応のため Storage 認証情報の再入力が必要）。その上で `filter-repo` で除去 |
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
| 1 | `fix: .gitignore に backups/ と Zone.Identifier を追加`（§0.1） |
| 2 | `fix: make lint を lock ファイル由来のバージョンで実行するよう変更`（§0.2, §0.3） |
| 3 | `fix: make purge の削除対象に test イメージを追加`（§0.4） |
| 4 | `chore: .dockerignore を追加`（§0.5） |
| 5 | `docs: README を全面改訂（Quick Start 先頭化、前提条件の整備）`（§2） |
| 6 | `docs: deployment.md を新設`（§3） |
| 7 | `docs: CLAUDE.md のリモート git 操作ルールを改訂`（§6.2, §0.6） |
| 8 | `docs: 公開前チェックリストを新設`（§6.3） |
| 9 | `chore: LICENSE を配置`（§6.1） |
| 10 | `docs: VM 実機検証の結果を反映（実測値・troubleshooting）`（§5, §4） |
| 11 | （必要なら）`fix: VM 検証で判明した問題を修正` |

**§7 の監査はコミットを伴わない。** 監査後、承認を得てからプッシュする。

完了後、`checkpoint/step-03.5` タグを打つ。

---

# 10. 完了条件

1. §0 の是正タスク6件がすべて完了している
2. `.gitignore` に `backups/` と `*:Zone.Identifier` が含まれている
3. `make lint` が lock ファイル由来のバージョンで実行される
4. `make purge` が `sluicery:local-test` も削除する
5. README の Quick Start が、`make` 無しでコピペ実行できる
6. README に「何ではないか」が上部に記載され、**Web UI 未実装であることが明記**されている
7. 前提条件に**実測値**（イメージサイズ、メモリ、ビルド時間）が入っている
8. 検証状況の表に、未検証環境が正直に記載されている
9. `docs/deployment.md` / `docs/troubleshooting.md` が存在する
10. `docs/troubleshooting.md` の内容が、**実際に踏んだ問題**に基づいている
11. VM 上で `git clone` → `cp .env.example .env` → `SECRET_KEY` 設定 → `docker compose up -d --build` が**追加手順なしで**成功する
12. VM 上で `ytdlp fetch` が成功し、Staging にファイルが生成される
13. VM 上で `make purge` 後の残骸が `docs/footprint.md` の記載と一致する
14. `LICENSE` が配置され、README にライセンスセクションがある
15. `CLAUDE.md` §4.1 と `AISTATE.md` の記述が整合している
16. `docs/公開前チェックリスト.md` が存在する
17. **履歴監査が実行され、結果が報告されている**
18. ユーザーの承認後、GitHub に private で作成・プッシュされている
19. Actions が無効化されている
20. public 化されている
21. README の clone URL が実際のものに差し替えられている
22. `AISTATE.md` が更新され、Phase 4 の着手点が書かれている

---

# 11. ドキュメント更新義務

| ファイル | 内容 |
|---|---|
| `README.md` | 全面改訂 |
| `docs/deployment.md` | 新設 |
| `docs/troubleshooting.md` | 新設 |
| `docs/公開前チェックリスト.md` | 新設 |
| `CLAUDE.md` §1 | ドキュメント体系の表に新設ファイルを追加 |
| `CLAUDE.md` §4.1 | リモート git 操作ルールの改訂 |
| `CLAUDE.md` §5 | `.gitignore` の初期内容に §0.1 の追加分を反映 |
| `docs/基本設計.md` §7 | 公開に関する設計判断（ライセンス選定、private→public の手順）を D-016 以降として記録 |
| `docs/変更履歴.md` | 未リリース欄に追加項目と VM 検証結果 |
| `docs/storage.md` | LXC 環境について「未検証」の注記（§3.1 の 8 と整合させる） |
| `docs/footprint.md` | `.dockerignore` の追記、`make purge` の対象更新（§0.4, §0.5） |
| `LICENSE` | 新設 |
| `AISTATE.md` | 全文書き換え（着手前と完了後の2回） |

---

# 12. 実装時の注意（再掲）

- **`backups/` の除外を最優先で入れる。** クレデンシャルを含むアーカイブが公開されると実害が出る
- **監査は履歴全体に対して行う。** 作業ツリーだけ見ても意味がない
- **監査後に停止し、承認を得るまでプッシュしない**
- **private で作成してから public 化する**
- **履歴に混入があっても勝手に書き換えない。** 判断を仰ぐ
- **コミットポイントは可能な限り残す。** 作り直しは最後の手段
- **troubleshooting は実際に踏んだ問題だけ書く。** 想像で書かない
- **README の実測値は実際に計測した値を書く。** 推測値を書かない
- **未検証の環境を検証済みのように書かない**
- 判断に迷ったら要件定義 §1.4 の設計原則に照らし、それでも決まらなければ**確認を取る**
