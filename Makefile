SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

COMPOSE ?= docker compose
PROJECT ?= pawcorder

.PHONY: help install up down restart logs ps pull update admin-logs frigate-logs config tailscale mount-nas password reset test test-py test-shell lint uninstall uninstall-soft uninstall-full uninstall-nuke demo

help: ## Show this help
	@awk 'BEGIN{FS=":.*?## "} /^[a-zA-Z_-]+:.*?## /{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Run the full installer (Docker, packages, .env, bring up stack)
	./install.sh

demo: ## Run admin in demo mode (no Docker, no Frigate, mock data) — http://localhost:8080
	./scripts/run-demo.sh

up: ## Start admin panel + Frigate
	$(COMPOSE) up -d

down: ## Stop the stack (containers removed, data preserved)
	$(COMPOSE) down

restart: ## Restart the stack
	$(COMPOSE) restart

logs: ## Tail combined logs
	$(COMPOSE) logs -f --tail=100

admin-logs: ## Tail admin panel logs
	$(COMPOSE) logs -f --tail=100 admin

frigate-logs: ## Tail Frigate logs
	$(COMPOSE) logs -f --tail=100 frigate

ps: ## Show service status
	$(COMPOSE) ps

pull: ## Pull latest images
	$(COMPOSE) pull

update: pull ## Pull latest images and recreate
	$(COMPOSE) up -d --build

config: ## Re-render Frigate config from template (requires docker)
	$(COMPOSE) exec admin python -c "from app.config_store import render_and_write_if_complete; print('rendered' if render_and_write_if_complete() else 'no cameras configured yet')"

tailscale: ## Install Tailscale on this host (for remote access)
	./scripts/install-tailscale.sh

mount-nas: ## Interactively mount your NAS at the storage path
	./scripts/mount-nas.sh

password: ## Print the current admin password (read from .env)
	@grep '^ADMIN_PASSWORD=' .env | sed -E 's/^ADMIN_PASSWORD="?([^"]*)"?$$/\1/'

test: test-py test-shell ## Run all tests (Python + bash)

test-py: ## Run Python tests with coverage
	cd admin && \
	  ([ -d .venv ] || python3 -m venv .venv) && \
	  .venv/bin/pip install -q -r requirements-dev.txt && \
	  .venv/bin/python -m pytest tests/ --cov=app --cov-report=term-missing

test-shell: ## Lint bash scripts with shellcheck
	@command -v shellcheck >/dev/null 2>&1 || { echo "shellcheck not installed; brew install shellcheck or apt install shellcheck"; exit 1; }
	shellcheck install.sh uninstall.sh scripts/*.sh

lint: test-shell ## Run linters (currently shellcheck only)

uninstall: ## Interactive uninstall — pick a level
	./uninstall.sh

uninstall-soft: ## Stop containers + remove images. Keep settings + recordings.
	./uninstall.sh --soft

uninstall-full: ## Soft + delete the project folder. Keep recordings.
	./uninstall.sh --full

uninstall-nuke: ## Full + delete recordings. Cannot be undone.
	./uninstall.sh --nuke

reset: down ## Stop the stack and delete .env, cameras.yml, rendered config, and storage. DESTRUCTIVE.
	@echo "This will delete .env, config/config.yml, config/cameras.yml, and ./storage."
	@read -rp "Type YES to confirm: " ans; [[ "$$ans" == "YES" ]] || { echo "aborted"; exit 1; }
	rm -f .env config/config.yml config/cameras.yml
	rm -rf storage
