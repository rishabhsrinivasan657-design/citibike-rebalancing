.PHONY: install lint fmt test test-live test-integration up down logs

install:
	uv sync

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

fmt:
	uv run ruff check --fix src tests
	uv run ruff format src tests

test:
	uv run pytest

test-live:
	uv run pytest -m live

test-integration:
	uv run pytest -m integration

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f collector lake-sink

