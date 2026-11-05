.PHONY: dev down migrate test lint worker seed demo recover

dev:
	docker compose up --build

down:
	docker compose down

migrate:
	docker compose run --rm api alembic upgrade head

test:
	pytest

lint:
	ruff check .

worker:
	cpb-worker run --server http://localhost:8000 --token "$${CPB_WORKER_TOKEN}"

seed:
	docker compose exec api cpb seed
	docker compose cp api:/app/seed-credentials.json ./seed-credentials.json

demo:
	python scripts/demo.py

recover:
	docker compose exec api cpb recover-leases
