PY ?= .venv/bin/python
PIP ?= .venv/bin/pip

.PHONY: venv install test lint doctor up down status clean dev dev-start dev-stop dev-restart dev-status dev-logs

venv:
	python3 -m venv .venv
	$(PIP) install --upgrade pip

install:
	$(PIP) install -e ".[dev]"

test:
	$(PY) -m pytest

doctor:
	$(PY) -m clams.cli doctor

up:
	docker compose up -d

down:
	docker compose down

status:
	$(PY) -m clams.cli status

clean:
	rm -rf .pytest_cache build dist *.egg-info

# Local dev stack (backend API + Vite frontend). See scripts/dev.sh.
dev:
	./scripts/dev.sh start

dev-start:
	./scripts/dev.sh start

dev-stop:
	./scripts/dev.sh stop

dev-restart:
	./scripts/dev.sh restart

dev-status:
	./scripts/dev.sh status

dev-logs:
	./scripts/dev.sh logs
