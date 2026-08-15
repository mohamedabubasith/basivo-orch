.DEFAULT_GOAL := help
.PHONY: help setup up down api web dev migrate revision test lint build clean logs

API := apps/api
WEB := apps/web

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Install everything and prepare the database
	cd $(API) && uv sync
	cd $(WEB) && npm install
	@test -f $(WEB)/.env || cp $(WEB)/.env.example $(WEB)/.env
	$(MAKE) up
	@sleep 3
	$(MAKE) migrate
	@echo "\nReady. Run 'make dev' in one terminal, or 'make api' and 'make web' in two."

up: ## Start Postgres, Redis and Mailpit
	docker compose up -d

down: ## Stop them
	docker compose down

logs: ## Tail the container logs
	docker compose logs -f

api: ## Run the API on :8000
	cd $(API) && uv run uvicorn basivo_orch.main:app --reload --port 8000 --no-server-header

web: ## Run the web app on :5173
	cd $(WEB) && npm run dev

worker: ## Run the run worker (executes runs and fires schedules)
	cd $(API) && uv run python -m basivo_orch.worker

dev: ## Run API, worker and web together
	@$(MAKE) api & sleep 2; $(MAKE) worker & sleep 1; $(MAKE) web

migrate: ## Apply migrations
	cd $(API) && uv run alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add pipelines"
	cd $(API) && uv run alembic revision --autogenerate -m "$(m)"

test: ## Run the API test suite
	cd $(API) && uv run pytest -q

lint: ## Lint and typecheck both apps
	cd $(API) && uv run ruff check . && uv run ruff format --check .
	cd $(WEB) && npx tsc --noEmit && npx oxlint src

build: ## Production build of the web app
	cd $(WEB) && npm run build

clean: ## Remove build output and caches
	rm -rf $(WEB)/dist $(WEB)/node_modules/.vite
	find $(API) -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
