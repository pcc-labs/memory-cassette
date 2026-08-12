# uv resolves the toolchain per-invocation, so there is no venv to create or
# activate and no requirements file to drift from the Dockerfile.
UV := uv run --quiet --with fastapi --with httpx --with pytest --with uvicorn

.PHONY: help test run up down logs
.DEFAULT_GOAL := help

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-8s %s\n", $$1, $$2}'

test: ## Run the test suite
	$(UV) pytest -q

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
