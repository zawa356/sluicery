# sluicery

yt-dlp を用いた自己ホスト向けのプレイリスト同期サーバー。登録したプレイリスト（動画・音楽）の内容を
ローカルまたはネットワークストレージへ取得・保持し、実行のたびに差分だけを追記する。

単一の技術者が私的使用の範囲で運用することを前提としたツールです。詳細は
[docs/要件定義.md](docs/要件定義.md) と [docs/legal.md](docs/legal.md) を参照してください。

## セットアップ

```bash
git clone <repo> sluicery
cd sluicery
cp .env.example .env
$EDITOR .env          # 最低限 SECRET_KEY と保存先パスを設定する
make lock              # requirements.lock を初回生成（要ネットワーク・要 Docker）
make up
```

`SECRET_KEY` を設定しない場合、明確なエラーメッセージを出して起動を拒否します。

初回起動時、`.env` の `ADMIN_USERNAME` / `ADMIN_PASSWORD` で管理者アカウントが作成されます。
`ADMIN_PASSWORD` を空のままにした場合はランダムなパスワードが生成され、起動ログに一度だけ出力されます。

## 運用コマンド

| コマンド | 内容 |
|---|---|
| `make up` | ビルドして起動 |
| `make down` | 停止（データは保持） |
| `make logs` | ログ追跡 |
| `make shell` | `app` コンテナにシェル接続 |
| `make sync` | 全プレイリストの同期を即時実行 |
| `make backup` | DB + 設定 + シークレットを単一アーカイブに書き出し |
| `make restore FILE=...` | バックアップから復元 |
| `make purge` | 削除対象を表示して確認した上でコンテナ・イメージ・volume を削除（bind mount 先の実体は削除しない） |

## ドキュメント

| ファイル | 内容 |
|---|---|
| [AISTATE.md](AISTATE.md) | セッション間の引き継ぎ用（開発者・AI 向け） |
| [docs/要件定義.md](docs/要件定義.md) | 何を作るか |
| [docs/基本設計.md](docs/基本設計.md) | どう作るか、設計判断の記録 |
| [docs/変更履歴.md](docs/変更履歴.md) | 変更履歴 |
| [docs/footprint.md](docs/footprint.md) | ホスト上に作られるものの一覧 |
| [docs/storage.md](docs/storage.md) | ストレージ方式の解説 |
| [docs/legal.md](docs/legal.md) | 利用上の注意 |

## 現在の状態

実装は要件定義 §20 の順序で段階的に進めています。現在地は [AISTATE.md](AISTATE.md) を参照してください。
