# Contributing

Thank you for considering contributing! This project is a modular messaging runtime with a Telegram adapter, FastAPI backend, and a plugin system. We welcome bug reports, docs, examples, and features.

## Getting started

- Python 3.10+ recommended (3.13 OK)
- Create a virtualenv and install deps:
  ```bash
  make venv
  make install
  ```
- Run the Telegram bot:
  ```bash
  make run-bot
  ```
- Run the FastAPI backend (for demo endpoints and OpenAPI):
  ```bash
  make run-api
  # Docs: http://127.0.0.1:8090/docs  ReDoc: http://127.0.0.1:8090/redoc
  ```

Environment variables live in `messaging_service/.env` (see `messaging_service/.env.example`).

## Project layout

- `messaging_service/main.py` — app bootstrap (loads config, registers adapter and plugins, starts runtime)
- `messaging_service/api_server.py` — FastAPI demo/backing endpoints (OpenAPI, ReDoc)
- `messaging_service/adapters/telegram/` — Telegram adapter
- `messaging_service/plugins/` — feature plugins (e.g., `admin_tools`, `analytics`, `product_catalog`, `ton_watcher`)
- `messaging_service/core/` — core services (analytics, event bus, user store, group meter)
- `messaging_service/config/` — YAML config and JSON stores (users, groups, etc.)

## Development workflow

- Format and lint:
  ```bash
  make fmt
  make lint
  ```
- Add tests under `tests/` (pytest). Placeholder `make test` is ready.
- Open a PR that:
  - Explains the problem/feature and approach
  - Includes tests or examples where applicable
  - Updates docs/README if behavior changes

## Configuration: enable/disable plugins

Plugins are configured in `messaging_service/config/config.yaml` under `plugins.enabled`.

```yaml
plugins:
  enabled:
    - echo
    - questionnaire
    - admin_tools
    - analytics
# remove entries above to disable example plugins
```

Many plugins also have their own sections for settings (e.g., `admin_tools`, `products`, `crypto`, `group_activation`).

## Extending: adding functions from other sources

You can integrate external logic in several ways:

- Plugin pattern (recommended):
  - Create a new module in `messaging_service/plugins/<your_plugin>.py` implementing a `handle(message, service)` coroutine.
  - Register it in `main.py` or add a small loader that auto-discovers by config.
  - Add it to `plugins.enabled` in `config.yaml`.

- Event bus hooks:
  - Use `core/event_bus.py` topics to react to events without touching core code. Configure handlers in `config/config.yaml` under `events.handlers` using built-in actions `send_message`, `http_request`, or `shell`.

- HTTP callbacks:
  - Call out to your APIs from plugins using `aiohttp`, or configure declarative HTTP in plugins that support it (e.g., `product_catalog.delivery.http`).

- Replace example logic:
  - Disable example plugins by removing them from `plugins.enabled`.
  - Add your plugin and route commands via `StartRouter` or your own command parsing.

## API docs

When `make run-api` is running:

- Swagger UI: http://127.0.0.1:8090/docs
- ReDoc: http://127.0.0.1:8090/redoc

Endpoints are defined in `messaging_service/api_server.py`. Use `examples/api/*.sh` for ready-to-run curl scripts.

## Security

- Do not commit real secrets. `.gitignore` excludes `messaging_service/.env` and local JSON stores.
- Admin registration uses a shared secret; keep it strong.
- Demo API endpoints support bearer auth via `Authorization: Bearer <token>`.

## Coding style

- Python: black + isort + flake8 (see Makefile)
- Keep imports at top, avoid side-effects on import.
- Prefer small, focused plugins.

## Reporting issues

- Include reproduction steps, logs, expected vs actual behavior.
- Attach config snippets if relevant (redact secrets).

## License

- Please open an issue to discuss the preferred license (MIT/Apache-2.0) if you intend to contribute significant code. We’ll add a LICENSE file accordingly.
