.PHONY: up down test lint seed e2e screenshots

COMPOSE ?= docker compose

up:
	$(COMPOSE) up --build -d

down:
	$(COMPOSE) down

# Тесты идут против настоящих Postgres и Redis из compose (отдельные БД: crewboard_test, redis db=1)
test:
	$(COMPOSE) run --rm --no-deps -e ENV=test api sh -c "pytest --cov=app --cov-report=term-missing"

lint:
	$(COMPOSE) run --rm --no-deps api sh -c "ruff check . --output-format concise && ruff format --check . && mypy app"
	$(COMPOSE) run --rm --no-deps web sh -c "npm install --no-audit --no-fund && npm run lint && npm run typecheck"

seed:
	$(COMPOSE) exec api python -m app.seed

e2e:
	cd frontend && npx playwright test

screenshots:
	cd frontend && npx playwright test --config playwright.screenshots.config.ts
