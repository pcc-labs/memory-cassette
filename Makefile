# Dependencies live in pyproject.toml and are pinned by uv.lock; uv run syncs
# the environment (including the dev group) before every invocation.
UV := uv run --quiet
COGNEE_TEST_DIR := $(CURDIR)/.cognee-test

.PHONY: help test test-cognee lint fmt hooks run up down logs
.DEFAULT_GOAL := help

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-12s %s\n", $$1, $$2}'

test: ## Run the suite (Cognee-backed cases skip)
	$(UV) pytest -q

# The whole contract, run against a real Cognee engine on its file-based
# defaults (SQLite, LanceDB, Kuzu) in a throwaway directory — no Postgres and
# no compose. COGNEE_SKIP_CONNECTION_TEST is what keeps this free: ingesting
# and reviewing need no LLM, only accepting does, so the store contract can be
# verified end to end without a credential or a bill.
test-cognee: ## Run the suite against a real Cognee engine, including the durability cases
	@rm -rf $(COGNEE_TEST_DIR)
	-@COGNEE_ENABLED=true \
		COGNEE_STORAGE_DIR=$(COGNEE_TEST_DIR) \
		COGNEE_SKIP_CONNECTION_TEST=true \
		uv run --quiet --extra cognee pytest -q --no-cov -p no:warnings
	@rm -rf $(COGNEE_TEST_DIR)

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

up: ## Bring up postgres + tapes + this cassette (needs LLM_API_KEY)
	@if [ -z "$$LLM_API_KEY" ] && [ "$${COGNEE_ENABLED:-true}" != "false" ]; then \
		echo "LLM_API_KEY is not set. Cognee calls an LLM when an entry is accepted."; \
		echo "  export LLM_API_KEY=sk-...            # then: make up"; \
		echo "  COGNEE_ENABLED=false make up         # volatile store, no credential"; \
		exit 1; \
	fi
	docker compose up --build -d
	@echo "tapes:    http://localhost:8082/v1/cassettes"
	@echo "cassette: http://localhost:9998/ping"

down: ## Tear it down (add ARGS=-v to drop the database volume)
	docker compose down $(ARGS)

logs: ## Follow the cassette's logs
	docker compose logs -f memory
