.PHONY: up down logs shell sync backup restore purge lock migrate revision lint test

COMPOSE := docker compose

# Dockerfile の runtime ステージと同じ digest を参照する（要件定義 §4.2、Phase 3.5 指示書 §0.3）。
# Dockerfile 側を更新したらここも合わせて更新すること。
PYTHON_SLIM_DIGEST := python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

shell:
	$(COMPOSE) exec app /bin/bash

# Phase 8 で全プレイリストの同期（discover → download）を実装する。
sync:
	@echo "ERROR: make sync は未実装です（Phase 8 で実装予定）"
	@false

# Phase 20 で DB + config + シークレットのバックアップを実装する。
backup:
	@echo "ERROR: make backup は未実装です（Phase 20 で実装予定）"
	@false

# Phase 20 でバックアップからの復元を実装する。
restore:
	@echo "ERROR: make restore は未実装です（Phase 20 で実装予定）"
	@false

# コンテナ・イメージ・volume・ネットワークを削除する。bind mount 先の
# メディア本体は削除されない。実行前に削除対象を一覧表示し、確認を取る。
purge:
	@echo "以下を削除します（bind mount 先のメディア本体は削除されません）:"
	@$(COMPOSE) ps -a
	@$(COMPOSE) config --volumes
	@echo "sluicery:local-test（存在する場合。make test / make lint が作るテスト用イメージ）"
	@read -p "続行しますか？ [y/N] " ans; \
	if [ "$$ans" != "y" ] && [ "$$ans" != "Y" ]; then echo "中止しました"; exit 1; fi
	$(COMPOSE) down --rmi local --volumes --remove-orphans
	docker rmi sluicery:local-test 2>/dev/null || true

# requirements.in / requirements-dev.in から requirements.lock / requirements-dev.lock を
# 再生成する。ネットワークアクセス可能な環境で実行すること。依存を更新したときのみ実行し、
# 生成物はコミットする（README のセットアップ手順には含めない）。
lock:
	docker run --rm -v "$(CURDIR)":/work -w /work $(PYTHON_SLIM_DIGEST) \
		bash -c "pip install pip-tools \
			&& pip-compile --generate-hashes --output-file requirements.lock requirements.in \
			&& pip-compile --generate-hashes --allow-unsafe --output-file requirements-dev.lock requirements-dev.in"

# 手動でマイグレーションを適用する（AUTO_MIGRATE=false の運用時、または
# app サービス起動前に明示的に適用したい場合に使う）。
migrate:
	$(COMPOSE) exec app python3 -m sluicery.cli db upgrade

# 例: make revision MSG="storage に last_check_result_json を追加"
revision:
	@if [ -z "$(MSG)" ]; then echo "使用法: make revision MSG=\"説明\""; exit 1; fi
	$(COMPOSE) exec app python3 -m sluicery.cli db revision -m "$(MSG)"

# dev 依存込みの test ステージをビルドし、lock ファイル由来の固定バージョンで
# ruff / mypy を実行する（Phase 3.5 指示書 §0.2。requirements-dev.in 直参照だと
# 実行時期でバージョンが変わり再現性が崩れるため）。
lint:
	docker build --target test -t sluicery:local-test .
	docker run --rm --entrypoint ruff sluicery:local-test check src tests
	docker run --rm --entrypoint mypy sluicery:local-test src

# dev 依存込みの test ステージをビルドし、コンテナ内で pytest を実行する
# （本番イメージに dev 依存を焼き込まないため、専用ステージを使う。sluicery:local
# タグとは別名にし、`make up` の本番イメージを上書きしないようにする。Phase 3 指示書 §0.1）。
test:
	docker build --target test -t sluicery:local-test .
	docker run --rm --entrypoint pytest sluicery:local-test
