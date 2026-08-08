.PHONY: up down logs shell sync backup restore purge lock lint test

COMPOSE := docker compose
BACKUP_DIR := ./backups
TIMESTAMP := $(shell date +%Y%m%d-%H%M%S)

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

shell:
	$(COMPOSE) exec app /bin/bash

# 全プレイリストの同期を即時実行する（discover → download をキューに投入）。
sync:
	$(COMPOSE) exec app python3 -m sluicery.cli sync --all

# DB + config + シークレットを単一アーカイブに書き出す。
# シークレットを含むため、バックアップの保管場所には注意すること（docs/legal.md 参照）。
backup:
	mkdir -p $(BACKUP_DIR)
	$(COMPOSE) exec -T app python3 -m sluicery.cli backup --stdout > $(BACKUP_DIR)/sluicery-$(TIMESTAMP).tar.gz
	@echo "backup written to $(BACKUP_DIR)/sluicery-$(TIMESTAMP).tar.gz"

# 例: make restore FILE=backups/sluicery-20260101-000000.tar.gz
restore:
	@if [ -z "$(FILE)" ]; then echo "使用法: make restore FILE=<backup file>"; exit 1; fi
	$(COMPOSE) exec -T app python3 -m sluicery.cli restore --stdin < $(FILE)

# コンテナ・イメージ・volume・ネットワークを削除する。bind mount 先の
# メディア本体は削除されない。実行前に削除対象を一覧表示し、確認を取る。
purge:
	@echo "以下を削除します（bind mount 先のメディア本体は削除されません）:"
	@$(COMPOSE) ps -a
	@$(COMPOSE) config --volumes
	@read -p "続行しますか？ [y/N] " ans; \
	if [ "$$ans" != "y" ] && [ "$$ans" != "Y" ]; then echo "中止しました"; exit 1; fi
	$(COMPOSE) down --rmi local --volumes --remove-orphans

# requirements.in / requirements-dev.in から requirements.lock を再生成する。
# ネットワークアクセス可能な環境で実行すること。
lock:
	docker run --rm -v "$(CURDIR)":/work -w /work python:3.12-slim \
		bash -c "pip install pip-tools && pip-compile --generate-hashes --output-file requirements.lock requirements.in"

lint:
	docker run --rm -v "$(CURDIR)":/work -w /work python:3.12-slim \
		bash -c "pip install -r requirements-dev.in && ruff check src tests && mypy src"

test:
	$(COMPOSE) exec app pytest
