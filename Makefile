PY ?= .venv/bin/python
PIP ?= .venv/bin/pip

.PHONY: venv install test test-live test-redis migrate status up down clean

venv:
	python3 -m venv .venv
	$(PIP) install --upgrade pip

install:
	$(PIP) install -e ".[dev]"

test:
	$(PY) -m pytest

test-redis:
	CLAMS_REDIS_URL=redis://127.0.0.1:6379/0 $(PY) -m pytest

test-live:
	$(PY) -m pytest tests/contract -m live

migrate:
	$(PY) -m clams.cli migrate

status:
	$(PY) -m clams.cli status

up:
	./clamsctl start

down:
	YES=1 ./clamsctl down

clean:
	rm -rf .pytest_cache **/__pycache__ build dist *.egg-info
