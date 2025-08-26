PY=messaging_service/.venv/bin/python
PIP=messaging_service/.venv/bin/pip
UVICORN=messaging_service/.venv/bin/uvicorn
BLACK=messaging_service/.venv/bin/black
ISORT=messaging_service/.venv/bin/isort
FLAKE8=messaging_service/.venv/bin/flake8

.PHONY: help venv install run-bot run-api dev fmt lint test examples cli init

help:
	@echo "Common targets:"
	@echo "  make venv        # create virtualenv"
	@echo "  make install     # install dependencies"
	@echo "  make run-bot     # run Telegram bot"
	@echo "  make run-api     # run FastAPI backend on :8090"
	@echo "  make dev         # run API with auto-reload"
	@echo "  make cli         # run CLI (typer)"
	@echo "  make init        # initialize .env and plugins (CLI)"
	@echo "  make fmt         # format code (black, isort)"
	@echo "  make lint        # lint code (flake8)"
	@echo "  make test        # placeholder for tests"
	@echo "  make examples    # show example curl commands"

venv:
	python3 -m venv messaging_service/.venv

install: venv
	$(PIP) install -r messaging_service/requirements.txt

run-bot:
	$(PY) messaging_service/main.py

run-api:
	$(UVICORN) messaging_service.api_server:app --host 127.0.0.1 --port 8090

# Auto-reload during API development
dev:
	$(UVICORN) messaging_service.api_server:app --host 127.0.0.1 --port 8090 --reload

fmt:
	$(BLACK) messaging_service || true
	$(ISORT) messaging_service || true

lint:
	$(FLAKE8) messaging_service || true

test:
	@echo "No tests yet. Add pytest and tests/ when ready."

examples:
	@echo "Docs: Swagger http://127.0.0.1:8090/docs  ReDoc http://127.0.0.1:8090/redoc"
	@echo "Health: curl -sS http://127.0.0.1:8090/"
	@echo "Deliver: AUTH=dev-demo-bearer bash examples/api/deliver.sh"
	@echo "Group upgrade: AUTH=dev-demo-bearer bash examples/api/group_upgrade.sh"
	@echo "TON verify: AUTH=dev-demo-bearer bash examples/api/ton_verify.sh"

cli:
	$(PY) -m messaging_service.cli --help || $(PY) messaging_service/cli.py --help

init:
	$(PY) -m messaging_service.cli init || $(PY) messaging_service/cli.py init
