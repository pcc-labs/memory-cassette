# Dependencies live in pyproject.toml and are pinned by uv.lock; uv run syncs
# the environment (including the dev group) before every invocation.
UV := uv run --quiet
TEST_DB := memory-cassette-test-db
TEST_DSN := postgres://postgres:test@127.0.0.1:55432/postgres

.PHONY: help test test-pg lint fmt hooks run up down logs
.DEFAULT_GOAL := help

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-8s %s\n", $$1, $$2}'

test: ## Run the suite (Postgres-backed cases skip)
	$(UV) pytest -q

test-pg: ## Run the suite against a throwaway Postgres, including the durability cases
	@docker rm -f $(TEST_DB) >/dev/null 2>&1 || true
	@docker run -d --rm --name $(TEST_DB) -e POSTGRES_PASSWORD=test \
		-p 55432:5432 postgres:17-alpine >/dev/null
	@printf 'waiting for postgres'
	@until docker exec $(TEST_DB) pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1; \
		do printf '.'; sleep 0.5; done; echo
	-@TAPES_DATABASE_URL=$(TEST_DSN) $(UV) pytest -q
	@docker rm -f $(TEST_DB) >/dev/null

lint: ## Ruff lint + format check
	$(UV) ruff check .
	$(UV) ruff format --check .

fmt: ## Auto-fix lint issues and reformat
	$(UV) ruff check --fix .
	$(UV) ruff format .

hooks: ## Install the pre-commit hook (ruff + tests with coverage)
	git config core.hooksPath .githooks
	chmod +x .githooks/*

run: ## Serve the cassette alone on :9998 (no tapes; nothing will fetch /openapi)
	$(UV) uvicorn main:app --host 127.0.0.1 --port 9998 --reload

up: ## Bring up postgres + tapes + this cassette
	docker compose up --build -d
	@echo "tapes:    http://localhost:8082/v1/cassettes"
	@echo "cassette: http://localhost:9998/ping"

down: ## Tear it down (add ARGS=-v to drop the database volume)
	docker compose down $(ARGS)

logs: ## Follow the cassette's logs
	docker compose logs -f memory
