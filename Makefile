PY ?= .venv/bin/python
PIP ?= .venv/bin/pip

.PHONY: venv install test lint doctor up down status clean

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
