.PHONY: help setup up down logs migrate test test-web demo clean fresh

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Create .env with a freshly generated credential master key
	@# One shell for the whole guard. Make runs each recipe LINE in its own shell, so
	@# an `exit 0` on the test line does not stop the lines below it - which meant a
	@# second `make setup` silently regenerated the master key and made every stored
	@# credential permanently unreadable.
	@if [ -f .env ]; then \
	  echo ".env already exists - not overwriting."; \
	  echo "To rotate the key deliberately, delete .env first and re-enter your provider keys."; \
	else \
	  cp .env.example .env; \
	  KEY=$$(python3 -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"); \
	  sed -i.bak "s|^CREDENTIAL_MASTER_KEY=.*|CREDENTIAL_MASTER_KEY=$$KEY|" .env && rm -f .env.bak; \
	  echo "Wrote .env with a new CREDENTIAL_MASTER_KEY."; \
	  echo "Back it up: lose it and every stored provider key becomes unreadable."; \
	fi

up: ## Start the stack (db, redis, api, web)
	docker compose up -d --build
	@echo "web  http://localhost:$${WEB_PORT:-3000}"
	@echo "api  http://localhost:$${API_PORT:-8000}/health"

down: ## Stop the stack, keep data
	docker compose down

logs: ## Tail all logs
	docker compose logs -f --tail=100

migrate: ## Apply migrations to an ALREADY-initialised database
	@for f in db/migrations/*.sql; do \
	  echo "-> $$f"; \
	  docker compose exec -T db psql -v ON_ERROR_STOP=1 -U coldstack -d coldstack -f - < $$f || exit 1; \
	done

test: ## Run the python test suite
	cd services/worker && python3 -m pytest -q

test-web: ## Typecheck and build the web app
	cd apps/web && npx tsc --noEmit && npx next build

demo: ## Build a list with the fake providers - no keys, no spend
	cd services/worker && python3 -m coldstack.cli build --demo --limit 200 --only-valid --out ../../leads.csv

clean: ## Stop and delete the database volume
	docker compose down -v

fresh: clean up ## Wipe and restart from scratch
