# Loomr — Modular Messaging Service
Loomr is a modular, event-driven messaging runtime for building bots and automations across channels. Start with Telegram today; add more platforms via adapters and plugins. Configure flows in YAML, integrate via HTTP or SDKs, and run anywhere (Docker, CLI, or code).

## Why Loomr

- **Multi-channel by design**: Start with Telegram; add more via adapters.
- **Modular**: Drop-in plugins for features (admin tools, analytics, menus, products, support, ticker, etc.).
- **Flows in YAML**: Describe steps, prompts, validations, branches without code.
- **Event-driven**: Built-in bus to trigger HTTP or shell actions on events (payments, roles, deliveries).
- **First-class API**: FastAPI server with OpenAPI/Swagger for delivery and integrations.
- **Ship fast**: Use Docker one-liners or the CLI, customize when needed.

## Quickstart
[![Docker](https://img.shields.io/badge/Docker-ready-0db7ed?logo=docker&logoColor=white)](Dockerfile)

- Docker (API):
  ```bash
  docker build -t loomr/messaging .
  docker run --rm -p 8090:8090 -e MODE=api loomr/messaging
  # Open http://127.0.0.1:8090/docs
  ```

- Docker (Bot):
  ```bash
  docker run --rm --env-file messaging_service/.env -e MODE=bot loomr/messaging
  ```

- CLI (local):
  ```bash
  python3 -m venv messaging_service/.venv
  messaging_service/.venv/bin/pip install -r messaging_service/requirements.txt
  messaging_service/.venv/bin/python -m messaging_service.cli init
  messaging_service/.venv/bin/python -m messaging_service.cli run-api
  # or
  messaging_service/.venv/bin/python -m messaging_service.cli run-bot
  ```

## Local LLM via Ollama (optional)

Run Loomr with a local model through [Ollama](https://ollama.com/). The built-in plugin `ollama_assistant` exposes two commands:

- `/ask <question>` — ask the local model a question.
- `/coach <user reply>` — evaluate if a user's reply answers the current question and get a clearer re-ask.

### Setup

1) Install and pull a model

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3:8b
curl -sS http://127.0.0.1:11434/api/tags
```

2) Environment

Copy `.env.example` to `.env` and set at least your Telegram token. Optional Ollama vars:

```env
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_MODEL=llama3:8b
QUESTIONARY_PATH=messaging_service/config/questionary.md
```

3) Enable plugin

`messaging_service/config/config.yaml` already includes `ollama_assistant` under `plugins.enabled` and an `ollama:` section.

4) Run

```bash
make venv && make install
make run-bot
```

### Usage examples

DM or Group (with BotFather privacy ON, the bot only sees commands):

```text
/ask What plugins are enabled?
/ask Summarize the last 5 messages.
/coach Why should I choice ?
```

Group privacy OFF allows the bot to receive normal messages, but by default this plugin only responds to `/ask` and `/coach`.

## API overview

FastAPI app in `messaging_service/api_server.py`.

- Docs: `GET /docs`, `GET /redoc`
- Product delivery: `POST /deliver`
- TON verify (example): `POST /ton/verify`
- Group upgrade (example): `POST /group/upgrade`

Auth: set `DELIVER_BEARER` in `.env` and include `Authorization: Bearer <token>` for `/deliver`.

## Roadmap

- Additional adapters (WhatsApp/Instagram/etc.)
- SDKs from OpenAPI (TypeScript + Python)
- More built-in plugins and flow blocks
- Docker image publish on tags (GHCR/Docker Hub)

## License

Source-available and free for non‑commercial use under the Prosperity Public License 3.0.0.

- See: `LICENSE` (Prosperity-3.0.0)
- Commercial licensing: `COMMERCIAL_LICENSE.md`
- Third‑party notices: `THIRD-PARTY-NOTICES.md`

## Contributing

See `CONTRIBUTING.md`. PRs and plugins welcome!

## Community & Support

- Telegram Support Group: https://t.me/+i1RDBKJv0U01OTQ0
- Author: Kai Gartner — LinkedIn: https://linkedin.com/in/kaigartner — Instagram: https://instagram.com/kaigartner
