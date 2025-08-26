# Loomr — Modular Messaging Service

[![Telegram Support](https://img.shields.io/badge/Telegram-Support%20Group-26A5E4?logo=telegram&logoColor=white)](https://t.me/+i1RDBKJv0U01OTQ0)
[![License](https://img.shields.io/badge/License-MIT%20or%20Apache--2.0-blue.svg)](../LICENSE-MIT)
[![Docker](https://img.shields.io/badge/Docker-ready-0db7ed?logo=docker&logoColor=white)](../Dockerfile)

— Created by [Kai Gartner](https://linkedin.com/in/kaigartner) · [Instagram](https://instagram.com/kaigartner)

A generic, extensible messaging runtime for building bots and automations across multiple channels. Starts with a Telegram adapter and is architected to add Instagram/WhatsApp/etc. via adapters and a unified capability layer.

## Core Ideas

- **Unified Core + Adapters**: `core/` defines capabilities (send, edit, typing). `adapters/<provider>/` maps them to each platform.
- **YAML Flows**: Design conversational flows with YAML state machines (prompts, validation, branching, loaders).
- **Plugins**: Drop-in handlers that implement features (Echo, Questionnaire, StartRouter, Payments, etc.).
- **Deep Links**: Start specific flows or products via `/start <payload>` from `t.me/<bot>?start=<payload>`.
- **Payments Options**: Use Stripe Checkout links (simple) or Telegram Payments (native).

## Quickstart

Prereqs: Python 3.10+ (3.13 OK), Telegram bot token.

1) Create venv and install:
```bash
python3 -m venv messaging_service/.venv
messaging_service/.venv/bin/pip install -r messaging_service/requirements.txt
```

2) Configure env in `messaging_service/.env`:
```
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_BOT_NAME="Your Bot Name"          # optional
TELEGRAM_BOT_ID=@your_bot_username          # optional
ADMIN_REGISTER_SECRET=change-me-strong-secret  # optional, for admin self-registration
ADMIN_CHAT_ID=123456789                        # optional, for event handler notifications
```

3) Run:
```bash
messaging_service/.venv/bin/python messaging_service/main.py
```

Send `/questionnaire` to test the onboarding flow, or `/echo Hello`.

## Config Overview (`config/config.yaml`)

```yaml
telegram:
  token: ${TELEGRAM_BOT_TOKEN}
  keyboard:
    resize: true
    one_time: true
  menu:
    commands:
      - command: start
        description: Start or restart onboarding
      - command: help
        description: Show help
      - command: admin
        description: Admin help

plugins:
  enabled:
    - echo
    - questionnaire
    - start_router
    - admin_tools
  questionnaire:
    flow_path: config/flows/onboarding.yaml
start_router:
  default_flow: onboarding

admin_tools:
  admins: ["123456789"]
  user_store_path: config/users.json
  register_secret: ${ADMIN_REGISTER_SECRET}

# payments (optional)
# payments:
#   stripe:
#     payment_links:
#       pro1: https://buy.stripe.com/...
#   # or telegram providers
#   provider_token: ${TELEGRAM_PROVIDER_TOKEN}
#   currency: EUR
#   products:
#     sku_001:
#       title: "Pro Access"
#       description: "30 days"
#       amount: 499
#       flow_after_payment: onboarding_pro
```

## YAML Flow Example (`config/flows/onboarding.yaml`)

```yaml
name: onboarding
start: ask_name
reveal:
  enabled: true
  type: typewriter
  mode: new
keyboard:
  enabled: true
  columns: 2
  remove_when_no_options: true
steps:
  ask_name:
    prompt: "Hi! I'm Weavre, using Weave A.I. What's your name? (type /cancel to exit)"
    var: name
    validate: "len(text) >= 2"
    error: "Please enter at least 2 characters."
    next: ask_nickname
    loader: true
  ask_nickname:
    prompt: "Nice to meet you, {name}! What should I call you? (use /back to go back)"
    var: nickname
    validate: "len(text) >= 2"
    error: "Nickname too short."
    next: summary
  summary:
    prompt: "All set, {name}! I'll call you {nickname}."
    end: true
triggers:
  commands: ["/start", "/onboard", "/questionnaire"]
```

Supports loader animation (edits same message), input validation, variables, and templating in prompts. Upcoming: conditional routing, inline menus, back/cancel.

## Keyboard configuration

- **Global defaults per flow**: under `keyboard` at the top of a flow YAML.
- **Per-step override**: add a `keyboard` block inside a step.
- Keys:
  - `enabled`: true/false to show buttons built from `options`.
  - `columns`: number of buttons per row.
  - `remove_when_no_options`: remove the reply keyboard when no options.
- Adapter-level Telegram defaults in `telegram.keyboard` control `resize` and `one_time`.

## Bot menu (Telegram)

- Configure commands in `telegram.menu.commands`. The adapter applies them at startup.
- Admin can refresh from chat: `/menu_refresh`.

## Admin tools plugin

Provides admin operations and user tracking stored in `config/users.json`.

- Public:
  - `/iam` — shows your Telegram ID and username
  - `/admin` — shows admin help
- Admin only:
  - `/broadcast <text>` — send to all known chats
  - `/notify role <role> <text>` — send to users with a role
  - `/notify user <id|@username> <text>` — send to a specific user
  - `/setrole <user_id> <role1,role2>` — set roles
  - `/roles <user_id>` — show roles
  - `/admin_list` — list admin IDs
  - `/menu_refresh` — reapply bot menu

### Admin self-registration

Option 1 — via shared secret:

1) Set `ADMIN_REGISTER_SECRET` in `.env` and ensure `admin_tools.register_secret: ${ADMIN_REGISTER_SECRET}` in config.
2) Restart the app.
3) In Telegram, send `/admin_register <your-secret>` to the bot.
4) Verify with `/admin_list`.

Option 2 — static admin list:

1) Put your numeric Telegram user ID under `admin_tools.admins` in `config/config.yaml`.
2) Restart the app.
3) Verify with `/admin_list`.

## Analytics & Admin Stats

Centralized async analytics using SQLite to track inbound/outbound messages, bytes, jobs, and events.

- Storage: `config/analytics.db` (created on startup)
- Module: `core/analytics.py`
- Admin plugin: `plugins/analytics.py`

### Enable

1) Ensure the plugin is enabled in `config/config.yaml`:

```yaml
plugins:
  enabled:
    - analytics
```

2) Dependencies (already added): `aiosqlite` in `requirements.txt`.

### Admin commands

- `/stats [days]` — totals across all groups for the last N days (default 1).
- `/stats_group <chat_id> [days]` — metrics for a specific chat.
- `/stats_top [days]` — top groups by message count.

All above are admin-only (admins are defined in `admin_tools.admins` or via `/admin_register`).

### What is logged

- Messages: direction (in/out), bytes, optional tokens, type (text/photo/etc.), chat_id, user_id, platform.
- Jobs: simple job run records with status and optional metadata.
- Events: generic events with JSON metadata.

### Notes

- Timestamps currently use the event loop time for efficient relative windows; suitable for daily/weekly queries while the bot runs. If you need wall-clock grouping, switch to `time.time()` in `core/analytics.py`.
- Telegram adapter is instrumented at receive (`_on_text`, commands) and send (`send_message` for text/photo) paths.

### Troubleshooting

- If `/stats` errors on first run, check the app logs for "Analytics init failed"; the DB is initialized best-effort at startup in `main.py`.
- Ensure your admin user ID is present in `config/config.yaml` under `admin_tools.admins`.

## Job Board plugin

Role-targeted jobs with first-claim reservation, submissions, and credits. Uses SQLite (`data/jobs.db`).

### Enable and configure

In `config/config.yaml` under `plugins.enabled` add `job_board` and configure:

```yaml
job_board:
  roles: ["influencer"]           # roles that receive job broadcasts
  posters_roles: []                # optional roles allowed to post jobs
  verification: manual             # manual | ig_post (future)
  credits: 50                      # default payout credits
  db_path: data/jobs.db            # SQLite file
  broadcast_template: |
    New job: {title}
    {desc}
    Reward: {credits} credits
    Job ID: {job_id}
    Claim with /claim {job_id}
```

Requires `AdminTools` to manage roles and track users.

### Commands

- Public:
  - `/jobs` — list open jobs
  - `/job <job_id>` — job details
  - `/claim <job_id>` — first claimant reserves the job (atomic)
  - `/submit <job_id> <url>` — submit proof
  - `/my_jobs` — your claimed/submitted jobs
  - `/credits` — view credit balance
- Posting & admin:
  - `/job_post "<title>" "<desc>" [hashtag=#brand] [credits=50] [type=manual]`
    - Admins can always post; optionally allow by `posters_roles`.
    - Broadcasts to `roles` via AdminTools user store.
  - `/job_approve <job_id>` — approve submission and grant credits
  - `/job_reject <job_id> <reason>` — reject submission
  - `/job_list [status]` — list jobs (admin)

### Typical workflow

1) Admin (or allowed poster) runs:
   `/job_post "IG shoutout" "Post a story with #brand today" hashtag=#brand credits=50 type=manual`
2) Users with the target role receive the broadcast and claim with `/claim <job_id>`.
3) Claimer submits a link with `/submit <job_id> <url>`.
4) Admin approves with `/job_approve <job_id>`; credits are added to the user.

Auto-verification (e.g., Instagram scraping) can be added later.

### Crypto-enabled jobs (optional)

You can post jobs that require an on-chain payment from the job placer (seller flow) and let the worker supply a wallet.

- Extend `/job_post` with flags:
  - `crypto_allowed=true` `chain=<BTC|ETH|XRP|XLM|LTC|DOGE>` `token=<optional>`
  - `min=<amount>` `confirmations=<n>` `pay_from=<address>` (optional sender filter)
  - Example:
    `/job_post "Shoutout post" "Post and get paid in BTC" credits=0 crypto_allowed=true chain=BTC min=0.0002 confirmations=1`

- Worker wallet commands:
  - `/wallet_set <chain> <address>` — save wallet for a chain
  - `/wallet_get [chain]` — list or show wallet
  - After claim, the saved wallet is attached to the claim if the chain matches.

- Check payment:
  - `/check_pay <job_id>` — on-demand monitoring. Currently supports BTC, LTC, DOGE, ETH via BlockCypher; XRP via Ripple Data API; XLM via Horizon.
  - If a qualifying payment is found (>= min amount, confirmations met, optional sender matches), the claim is marked with `payment_txid` and `payment_status` (detected/confirmed). Admin can then `/job_approve` as usual.

- Optional environment keys to improve reliability/rate limits (put in `.env` and referenced by config later if needed):
  - `BLOCKCYPHER_API_TOKEN`
  - `ETHERSCAN_API_KEY`, `POLYGONSCAN_API_KEY`, `BSCSCAN_API_KEY` (for future EVM expansions)
  - `ALCHEMY_API_KEY` or `MORALIS_API_KEY` (future)

Note: Background polling and auto-approve-on-payment can be enabled in a future iteration.

## Invites plugin (Invite-only access)

Lock the bot to invite-only and grant roles upon redeeming invite codes.

### Enable and configure

In `config/config.yaml`:

```yaml
plugins:
  enabled:
    - invites

invites:
  access:
    locked: false
    allowed_when_locked: [/start, /help, /join]
  enabled: true
  store_path: config/invites.json
  default_role: member
  code_length: 8
  expire_days_default: 7
  max_uses_default: 1
  bind_to_user_by_default: true     # new: codes are single-use and bound to a user by default
  daily_create_limit: 5             # new: per-creator daily limit
  # Optional app deep-link button in admin responses
  # Examples: myapp://join?code={code}&user={user_id} or https://your.app/invite?code={code}&user={user_id}
  app_link_template: ""
  # QR code for Telegram deep link when creating bound invites
  qr:
    enabled: true
    box_size: 6
    border: 2
  on_redeem:
    flow: onboarding
    welcome_template: |
      Welcome, {first_name}! You joined with code {code}.
      Role: {role}
```

- When `access.locked: true`, only admins and the allowlisted commands are permitted until a user redeems an invite.
- On redeem, if the invite defines `role` it is assigned; otherwise `default_role` is used (if set).
- If `on_redeem.flow` is set, that flow is triggered after joining; `welcome_template` is rendered and sent.
- Default behavior: invites are bound to a specific user (`bind_to_user_by_default: true`) and become single-use.
- Daily creation cap per creator (`daily_create_limit`).
- Deep links supported: users can tap `https://t.me/<bot>?start=join-<CODE>` which maps to `/join <CODE>`.
- Admin responses for bound invites include inline buttons:
  - "Open in Telegram" (deep link)
  - "Open in App" (if `app_link_template` set)
- QR image is included (if enabled) so you can forward the message and have invitees scan to open.
- Referral tracking is recorded: who created the invite and who redeemed it with a timestamp.

### Commands

- Public:
  - `/join <code>` — redeem invite
- Admin:
  - `/invite_create <code> [role=<role>] [max=<n>] [expires=<YYYY-MM-DD>] [flow=<flow>] [notes="..."]`
  - `/invite_for <user_id|@username> [role=<role>] [expires=<YYYY-MM-DD>]` — generates a single-use, user-bound code and returns a deep link, inline buttons, and a QR image
  - `/invite_list [active|all]`
  - `/invite_revoke <code>`

Environment tip for deep links: set `TELEGRAM_BOT_ID` (e.g., `@your_bot_username`) in `.env` so deep links include your bot username.

### Inline buttons API (Telegram adapter)

`TelegramAdapter.send_message()` now supports:

```python
await service.send_message(
  chat_id=chat_id,
  text="...",
  inline_buttons=[[ ["Label 1", "https://..."], ["Label 2", "myapp://..."] ]]
)
```

To send an image (e.g., QR) with caption and buttons:

```python
await service.send_message(
  chat_id=chat_id,
  text="...",  # fallback
  caption="Scan to join",
  photo_bytes=b"...PNG...",
  inline_buttons=[[ ["Open", "https://..."] ]]
)
```

## Product Catalog plugin

Browse products with inline-button pagination and deliver digital goods via HTTP/CLI hooks or deep-link verification.

### Enable and configure

In `config/config.yaml` under `plugins.enabled` add `product_catalog`, then configure `products`:

```yaml
plugins:
  enabled:
    - product_catalog

products:
  source: file
  file_path: config/products.json
  page_size: 5
  # Optional: a generic buy URL template (fallback to per-item url)
  # buy_link_template: "https://shop.example/checkout?id={id}&uid={user_id}"
  delivery:
    mode: disabled           # http | cli | disabled
    http:
      url: "https://api.example.com/deliver"
      method: POST
      headers:
        Authorization: "Bearer ${DELIVERY_API_TOKEN}"
      body_template: |
        {"action":"{action}","user_id":"{user_id}","chat_id":"{chat_id}","product_id":"{product_id}","token":"{token}"}
    cli:
      command_template: "license-issuer --user {user_id} --product {product_id} --token {token}"
```

Sample catalog file: `config/products.json`.

### Commands and callbacks

- `/products [#category]` — list products with pagination.
- Callbacks (handled via inline buttons):
  - `prod:list:page=<n>[:cat=...]` — switch page.
  - `prod:detail:<id>[:page=<n>][:cat=...]` — detail view with Buy/Back.
  - `prod:back:page=<n>[:cat=...]` — back to list.

### Delivery flows

- HTTP: POST to your API with a templated body; bot DMs the returned license/link.
- CLI: run a configurable command; stdout is sent to the user.
- Deep-link verification: after payment, redirect to `t.me/<bot>?start=deliver-<token>`; the plugin verifies via your API and delivers.

Requires `aiohttp` for HTTP mode (already in `requirements.txt`).

### Inline keyboards and edits

- The Telegram adapter supports inline buttons with `url` or `callback_data` and editing messages with updated keyboards.
- See `adapters/telegram/adapter.py` and examples in the Invites section.

## Non-text routing (files, media, location, contact)

The bot now routes non-text Telegram messages to plugins via a unified core model (`core/message.py`).

- Message types supported: `IMAGE`, `DOCUMENT`, `VIDEO`, `AUDIO`, `STICKER`, `LOCATION`, `CONTACT`.
- The Telegram adapter maps Telegram updates to `MessageType` and populates:
  - For media: `Message.content = file_id` (primary file_id)
  - For location: `Message.content = "lat,lon"`
  - For contact: `Message.content = "Full Name phone"`

Enable the generic FileRouter to emit events and/or forward to your API:

```yaml
plugins:
  enabled:
    - file_router

file_router:
  mode: emit_only  # or http
  # http:
  #   url: ${FILE_FORWARD_URL}
  #   method: POST
  #   headers:
  #     Content-Type: application/json
  #     Authorization: Bearer ${FILE_FORWARD_TOKEN}
  #   # Optional, override request body; {json} contains the default JSON string
  #   # body_template: "{json}"

events:
  handlers:
    - topic: message.file_received
      action: send_message
      params:
        to: "${ADMIN_CHAT_ID}"
        template: "File received type={type} file_id={file_id} from {user_id} in {chat_id}"
    - topic: message.location_received
      action: send_message
      params:
        to: "${ADMIN_CHAT_ID}"
        template: "Location received lat={lat} lon={lon} from {user_id} in {chat_id}"
    - topic: message.contact_received
      action: send_message
      params:
        to: "${ADMIN_CHAT_ID}"
        template: "Contact received text={text} from {user_id} in {chat_id}"
```

If you need actual file bytes, extend the adapter to resolve `file_id` → download URL and stream to your service (multipart/form-data or direct proxy).

## Location Demo plugin

Proximity features using per-chat last known user locations persisted in `data/locations.json` by `plugins/file_router.py`.

### Enable and configure

In `config/config.yaml` under `plugins.enabled` add `location_demo` (already enabled by default in this repo). Configuration:

```yaml
location_demo:
  default_radius_m: 200
  max_age_s: 43200   # ignore older than 12h
  max_results: 10
```

### Commands

- `/nearby [radius_m]` — list users near you within radius (meters)
- `/friend_find <@username|user_id> [radius_m]` — distance to a specific person
- `/radar [limit]` — closest N users
- `/myloc` — show your last stored location + Google Maps link

Notes:
- Share a location first so your own coordinates are stored.
- Results are scoped per chat (e.g., group vs. DM).

## Telegram WebApp Dashboard

Open a Web App inside Telegram via inline `web_app` button.

### Enable and configure

1) Ensure `dashboard` is enabled under `plugins.enabled` in `config/config.yaml`.
2) Set `web_app.url` to your Web App page (local default is the built-in page served by the webhook server):

```yaml
web_app:
  # Replace with your public HTTPS tunnel URL for mobile Telegram
  url: http://localhost:8081/webapp
```

3) Use `/dashboard` in Telegram, then tap "Open Dashboard" to launch the Web App.

The server in `main.py` serves a minimal page at `GET /webapp` and includes `telegram-web-app.js` to render user context from `initData`.

### HTTPS requirement and tunneling

- Telegram requires HTTPS/WSS for Web Apps in production. For local dev:
  - Use a tunnel (Cloudflare Tunnel/ngrok/Tailscale) to expose `http://localhost:8081` as HTTPS.
  - Update `web_app.url` to the public `https://.../webapp` address.

## Voice notes and stickers acknowledgments

- Voice notes are treated as `AUDIO` and acknowledged by `plugins/file_router.py` when `demo.ack_files` is enabled.
- Stickers are acknowledged similarly with file_id info.
- Config: see `file_router.demo` in `config/config.yaml`.

## Quick test checklist

1) Media groups (album of 3–10 photos): expect grouped event + auto GIF reply (if only images).
2) Single file (photo/video/document/audio/voice/sticker): expect acknowledgment with file_id and URL (if resolvable).
3) Location: share a location → expect footprint reply; run `/myloc` and `/nearby`.
4) Contact: share a contact → expect echo.
5) Web App: `/dashboard` → tap button → page shows your Telegram user info.

## Multi-Channel Design

- `core/service.py` defines the capability interface:
  - `send_message`, `edit_message_text`, `send_chat_action` (typing/loader), handlers registration.
- Adapter maps capabilities to platform APIs.
  - Telegram Adapter: `adapters/telegram/adapter.py`.
- Plugins only depend on the core interface, not on a provider.

To add a new provider (e.g., WhatsApp):
1) Create `adapters/whatsapp/adapter.py` implementing `MessagingService` methods.
2) Add provider config in `config/config.yaml`.
3) Register it in `main.py` similar to Telegram.

## External checkout webhook (payments)

For external gateways (Stripe/your checkout), a minimal webhook server is built into `main.py` using `aiohttp`.

Enable and configure in `config/config.yaml`:

```yaml
webhook:
  enabled: true
  host: 0.0.0.0
  port: 8081
  path: /webhook/payment
  secret: ${WEBHOOK_SECRET}  # required header: x-webhook-secret
```

Send a POST JSON like:

```json
{
  "user_id": "123",
  "chat_id": "123",
  "product_id": "p1",
  "token": "deliver-abc",
  "amount": 499,
  "currency": "USD",
  "meta": {"gateway": "stripe"}
}
```

The server emits `payment.external.confirmed` and DMs the user with a deep link `/start deliver-<token>` to trigger delivery in `ProductCatalog`.

Security notes:
- Protect with a strong `WEBHOOK_SECRET`; consider adding HMAC and idempotency in production.

## Deep Links and Start Router (planned)

- Payloads: `flow:onboarding`, `flow:onboarding:step:ask_nickname`, `product:pro1`.
- `StartRouter` plugin will parse `/start <payload>` and dispatch to the right plugin/flow.

## Payments: Stripe or Telegram

## Virtual Products, Roles, and Subscriptions

You can model non-downloadable products (access, plans, tiers) by assigning roles and optional expiries to users.

- Map product to role in `config/config.yaml` under `products.roles` (e.g., `p1: basic`, `p2: pro`).
- Optional time limit via `products.subscription_days` (e.g., `p2: 30`).
- Storage is in `config/users.json` managed by `AdminTools` and `core/user_store.py`.
- Users can check with `/tier`.

Example config:

```yaml
products:
  roles:
    p1: basic
    p2: pro
  subscription_days:
    p2: 30
```

Currently, role assignment is triggered by the USDT watcher flow (below). You can also assign manually with `/setrole <user_id> <role1,role2>`.

## USDT Crypto Watcher (optional)

Add on-chain USDT payment verification for ETH/BSC/Polygon/TRON, then grant roles/subscriptions and optionally call your delivery HTTP hook.

Enable plugin in `config/config.yaml` under `plugins.enabled`:

```yaml
plugins:
  enabled:
    - crypto_watcher
```

Configure in `config/config.yaml` under `crypto.usdt`:

```yaml
crypto:
  usdt:
    default_chain: eth   # eth | bsc | polygon | tron
    price_map:
      p1: 10
      p2: 20
    eth:
      recipient: ${TUSDT_WATCH_ADDRESS}
      confirmations: 1
      contract: "0xdAC17F958D2ee523a2206206994597C13D831ec7"
      api_key: ${ETHERSCAN_API_KEY}
    polygon:
      recipient: ${POLY_USD_ADDRESS}
      confirmations: 1
      contract: "0xC2132D05D31c914a87C6611C10748AEb04B58e8F"
      api_key: ${POLYGONSCAN_API_KEY}
```

Environment variables in `messaging_service/.env`:

```
TUSDT_WATCH_ADDRESS=0x...
POLY_USD_ADDRESS=0x...
ETHERSCAN_API_KEY=...
BSCSCAN_API_KEY=...
POLYGONSCAN_API_KEY=...
```

Usage in Telegram:

```
/usdt_check <txid> <product_id> [eth|bsc|polygon|tron]
```

If the transaction is a USDT transfer to your configured recipient and meets confirmations and amount (`crypto.usdt.price_map[product_id]`), the bot:

- Grants the mapped role (`products.roles[product_id]`).
- Extends expiry if `products.subscription_days[product_id]` is set.
- Optionally calls `products.delivery.http` and relays the response.

## TON Watcher (optional)

Add on-chain TON payment verification using tonapi.io, then grant roles/subscriptions and optionally trigger delivery similar to USDT.

### Enable and configure

In `config/config.yaml` under `plugins.enabled` add `ton_watcher`, then configure `crypto.ton`:

```yaml
plugins:
  enabled:
    - ton_watcher

crypto:
  ton:
    price_map:         # amount in TON per product_id
      p1: 5
      p2: 10
    recipient: ${TON_WALLET}
    confirmations: 1
    api_base: https://tonapi.io           # set to https://testnet.tonapi.io for testnet (if available)
    api_key: ${TONAPI_KEY}
```

Environment in `messaging_service/.env`:

```
TON_WALLET=EQC...your-ton-wallet...
TONAPI_KEY=your-tonapi-key
```

Usage in Telegram (admin-only):

```
/ton_check <tx_hash> <product_id>
```

If the transaction sent >= `price_map[product_id]` TON to `recipient` with required `confirmations`, the bot:

- Emits `payment.ton.confirmed`.
- Optionally triggers HTTP delivery, similar to USDT.

API docs reference: stub endpoints `POST /ton/verify` and `POST /group/upgrade` exist in `api_server.py` so the schema appears in Swagger/ReDoc. Real on-chain verification/delivery is done by the plugin.

## Group Activation & Credits (Telegram groups)

Require groups to activate via TON payment or an activation code, then meter inbound/outbound traffic using credits.

### Enable and configure

In `config/config.yaml`:

```yaml
plugins:
  enabled:
    - group_activation

group_activation:
  enabled: true
  store_path: config/groups.json
  min_activation_ton: 0.001
  ton_to_credits_rate: 10000000
  in_cost_per_kb_credits: 1.0
  out_cost_per_kb_credits: 0.25
  activation_codes: []
  activation_code_bundle_credits: 10000
  topup_message_cooldown_s: 3600

  # Optional: profile-based rate tiers and plan pricing
  default_profile: standard
  rate_profiles:
    standard:
      in_cost_per_kb_credits: 1.0
      out_cost_per_kb_credits: 0.25
    pro:
      in_cost_per_kb_credits: 0.7
      out_cost_per_kb_credits: 0.15
  plans:
    pro:
      monthly_ton: 0.02
```

And for TON pricing/recipient (testnet example):

```yaml
crypto:
  ton:
    price_map: { }
    recipient: ${TON_WALLET}
    confirmations: 1
    api_base: https://testnet.tonapi.io
    api_key: ${TONAPI_KEY}
```

Create the store file once:

```json
{
  "groups": {}
}
```

### How it works

- When the bot is added to a group, it posts the group `chat_id` and activation instructions.
- Until activated and funded, group messages are gated by `plugins/group_activation.py`.
- Admins can generate a TON invoice tagged with the group and activate by paying or by using an activation code.
- Credits are added based on TON amount (`ton_to_credits_rate`) or fixed bundle for codes.
- Each inbound/outbound message deducts credits based on its payload size in KB (rounded up) using `in_cost_per_kb_credits` and `out_cost_per_kb_credits`.
- If credits are low/exhausted, the plugin throttles reminder messages via `topup_message_cooldown_s`.
  
New (profiles + plans):
- Groups have a `profile` (e.g., `standard` or `pro`) and an optional `plan_expiry_ts`.
- Effective rates are resolved dynamically from `rate_profiles`; when a plan expires, groups fall back to `default_profile`.
- TON upgrade payments let admins set profile + duration using `product_id` = `group_upgrade:<chat_id>:<profile>[:months]`.

### Commands (admin-only in groups)

- `/ga_invoice [amountTON]` — reply in the group to generate a TON deep link invoice labeled as `group:<chat_id>`.
- `/activate <code>` — activate the group using a whitelisted activation code.
- `/ga_status` — show activation state, credits, and rates.
- After payment, run `/ton_check <tx_hash> group:<chat_id>` (handled by `plugins/ton_watcher.py`) to verify and credit the group.

New (plans):
- `/ga_plan` — show current plan/profile, remaining days, available profiles and monthly TON prices.
- `/ga_upgrade <profile> [months=1]` — generate a profile upgrade invoice. Internally uses `product_id` `group_upgrade:<chat_id>:<profile>[:months]` validated by `TonWatcher`.

### Notes

- Admins are sourced from `admin_tools.admins` or via `/admin_register` if enabled.
- The meter and store are implemented in `core/group_meter.py` using `config/groups.json`.
- The system is generic and can attach a `gateway` field per group for external integrations later.

### Welcome message with TON links and QR

When the bot is added to a group, the Telegram adapter posts a welcome that includes:

- The group `chat_id` and activation instructions.
- A TON deep link for minimal activation amount and a web link (tonhub) tagged with `group:<chat_id>`.
- A QR code image (if the optional `qrcode` library is installed) that encodes the web link for scanning.

Configuration keys used:
- `group_activation.min_activation_ton`
- `crypto.ton.recipient`

See `adapters/telegram/adapter.py` for the welcome implementation.

## Support Plugin (tickets, admin relay)

Provide a simple support/ticket flow for users and an admin-side relay to a group or direct messages.

### Enable and configure

In `config/config.yaml`:

```yaml
plugins:
  enabled:
    - support

support:
  group_id: ${SUPPORT_GROUP_ID}         # optional; if set the bot posts new tickets to this group (bot must be a member)
  store_path: config/support_tickets.json
  notify_admins_dm: true                 # if no group, DM all admins
```

Environment in `messaging_service/.env` (optional):

```
SUPPORT_GROUP_ID=-1001234567890
```

Admins are sourced from `admin_tools.admins` or via admin self-registration.

### Commands

- User:
  - `/support <message>` — open/update your open ticket; you’ll receive replies in DM.
- Admin:
  - `/support list` — list open tickets
  - `/support close <ticket_id>` — close a ticket
  - In the configured admin group, reply to the bot’s ticket notice to relay a response to the user
  - `/reply <ticket_id> <text>` — DM-based reply when no group is used

Tickets persist to `config/support_tickets.json`.

## Declarative Menus (YAML)

Register simple text commands without writing code using the `menu` plugin and the `menus` section in `config/config.yaml`.

1) Enable the plugin in `plugins.enabled`:

```yaml
plugins:
  enabled:
    - menu
```

2) Add menu entries:

```yaml
menus:
  - command: "/menu"
    text: |
      Menu:\n- /products\n- /tier\n- /events
  - command: "/events"
    text: |
      Available events:\n- payment.usdt.confirmed\n- delivery.sent\n- role.assigned
```

Now `/menu` and `/events` will respond with the configured text.

## Event Bus and Declarative Handlers

A lightweight in-process event bus allows other systems to hook into core actions (payments, role changes, deliveries) via YAML.

- File: `core/event_bus.py` exports a singleton `bus`.
- Config: `events.handlers` in `config/config.yaml`.
- Built-in actions: `send_message`, `http_request`, `shell`.
- Emitted topics:
  - `payment.usdt.confirmed` from `plugins/crypto_watcher.py`
  - `delivery.sent` from `plugins/crypto_watcher.py` (after HTTP delivery succeeds)
  - `role.assigned` from `core/user_store.py` (after `set_role`)
  - `product.viewed` from `plugins/product_catalog.py` (product detail opened)
  - `product.list_viewed` from `plugins/product_catalog.py` (list page rendered)
  - `product.buy_clicked` from `plugins/product_catalog.py` (Buy button used)

Example configuration:

```yaml
events:
  handlers:
    - topic: payment.usdt.confirmed
      action: send_message
      params:
        to: "${ADMIN_CHAT_ID}"
        template: "Payment {amount} USDT for {product_id} by {user_id} on {chain} (tx {txid})"
    - topic: role.assigned
      action: http_request
      filters:
        role: "pro"
      params:
        method: POST
        url: "https://example.com/webhooks/role"
        headers:
          Content-Type: "application/json"
        body_template: '{"user_id":"{user_id}","role":"{role}","expires_at":{expires_at}}'
    - topic: delivery.sent
      action: shell
      params:
        command: "echo Delivered {product_id} to {user_id}"
    - topic: product.viewed
      action: send_message
      params:
        to: "${ADMIN_CHAT_ID}"
        template: "Viewed {product_id} (page {page}, category {category}) by chat {chat_id}"
    - topic: product.list_viewed
      action: shell
      params:
        command: "echo List page {page}/{pages} cat {category} count {count}"
    - topic: product.buy_clicked
      action: send_message
      params:
        to: "${ADMIN_CHAT_ID}"
        template: "Buy clicked for {product_id} (page {page}, category {category}) by chat {chat_id}"
```

Notes:
- The bus is configured in `main.py` at startup; it uses the first available service (Telegram preferred) for `send_message`.
- Set `ADMIN_CHAT_ID` in `.env` if you use the example handler.
- You can add more handlers without changing code; they are matched by `topic` and optional `filters`.

## Deep Links (Telegram)

- Private chat payload: `https://t.me/<bot_username>?start=<payload>`
- Add bot to group with payload: `https://t.me/<bot_username>?startgroup=<payload>`
- Note: group invite links like `https://t.me/+...` do NOT support `?start=` payloads.

Use cases:
- Payments/delivery deep link: `/start deliver-<token>` in DM.
- Group activation: share `?startgroup=activate` so admins add the bot with context.

## Platform limitations: Calls

Telegram bots cannot place or receive voice/video calls. Alternatives:

- Request user contact phone via `KeyboardButton(request_contact=True)` and call externally.
- Send meeting links (Zoom/Meet/WebRTC) or a WebApp for voice/video.

## /tier command

The Admin Tools plugin provides `/tier` for users to view their current roles and expiries.

Example output:

```
Your plan(s):
- pro (expires in ~29d)
```

## Built-in API with Swagger/ReDoc

A small FastAPI server is included at `messaging_service/api_server.py` with auto-generated OpenAPI docs.

- Run it:

```bash
python3 -m venv messaging_service/.venv
messaging_service/.venv/bin/pip install -r messaging_service/requirements.txt
messaging_service/.venv/bin/uvicorn messaging_service.api_server:app --host 0.0.0.0 --port 8090
```

- Docs:
  - Swagger UI: `http://localhost:8090/docs`
  - ReDoc: `http://localhost:8090/redoc`

- Auth (optional): set `DELIVER_BEARER` in `.env`. When set, requests to `/deliver` must include `Authorization: Bearer $DELIVER_BEARER`.

### Wire Product Delivery to the API

Point `products.delivery.http.url` in `config/config.yaml` to this API:

```yaml
products:
  delivery:
    mode: http
    http:
      url: "http://localhost:8090/deliver"
      method: POST
      headers:
        Content-Type: application/json
        Authorization: "Bearer ${DELIVER_BEARER}"
      body_template: |
        {"action":"{action}","token":"{token}","user_id":"{user_id}","chat_id":"{chat_id}","product_id":"{product_id}"}
```

The `api_server.py` will respond with a JSON including a `license` and `download_url` which the bot forwards to the user.

- **Stripe Checkout Link (simple)**: send a button with a Stripe Payment Link; after success, user taps a deep link back to the bot to continue.
- **Telegram Payments (native)**: use `send_invoice`, handle `pre_checkout_query` and `successful_payment` to unlock flows.

## Ticker plugin (MQTT, SVG→GIF)

This plugin can subscribe to MQTT for live SVG payloads, convert them into efficient 10-second animated GIFs, and post/edit them in Telegram.

### Enable and configure

In `config/config.yaml`:

```yaml
plugins:
  enabled:
    - ticker

ticker:
  interval: 1.0         # seconds between fallback frames
  id: demo-1            # used in MQTT topic template
  mqtt:
    host: localhost
    port: 1883
    topic: "ticker/{id}" # {id} will be replaced with ticker.id
    # username: your-user
    # password: your-pass
```

Dependencies (already in `requirements.txt`): `paho-mqtt`, `cairosvg`, `Pillow`.

### Run

```bash
# create venv if needed
python3 -m venv messaging_service/.venv

# install deps
messaging_service/.venv/bin/pip install -r messaging_service/requirements.txt

# start bot
messaging_service/.venv/bin/python messaging_service/main.py
```

### Test with MQTT

Ensure a broker is running (e.g., Mosquitto on `localhost:1883`). Publish SVG to the configured topic:

```bash
mosquitto_pub -h localhost -p 1883 -t "ticker/demo-1" -m '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480"><rect width="100%" height="100%" fill="#111"/><text x="50%" y="50%" fill="#fff" font-size="48" text-anchor="middle" dominant-baseline="middle">Hello Ticker</text></svg>'
```

Or JSON-wrapped:

```bash
mosquitto_pub -h localhost -p 1883 -t "ticker/demo-1" -m '{"svg":"<svg xmlns=\\"http://www.w3.org/2000/svg\\" width=\\"640\\" height=\\"480\\"><rect width=\\"100%\\" height=\\"100%\\" fill=\\"#111\\"/><text x=\\"50%\\" y=\\"50%\\" fill=\\"#fff\\" font-size=\\"48\\" text-anchor=\\"middle\\" dominant-baseline=\\"middle\\">Hello JSON</text></svg>"}'
```

In Telegram, send `/ticker start` (or `/ticker demo`). When SVG arrives, the plugin updates the message to an animated GIF; otherwise it shows a fallback PNG ticker.

### Troubleshooting

- Exit code 127 / no output when running combined commands: ensure commands are separated correctly. For example, do not concatenate two commands without a delimiter. Use:

  ```bash
  messaging_service/.venv/bin/pip install -r messaging_service/requirements.txt && \
  messaging_service/.venv/bin/python messaging_service/main.py
  ```

- `cairosvg` requires Cairo libs; on macOS it is bundled via wheels for common Python versions. If you hit build issues, ensure Python 3.10+ and try upgrading pip: `python -m pip install -U pip`.

- If MQTT creds are required, set `ticker.mqtt.username/password` in `config/config.yaml`.


## Project Structure

```
messaging_service/
├── adapters/
│   └── telegram/
├── config/
│   └── flows/
├── core/
├── plugins/
├── main.py
└── requirements.txt
```

## Create a Plugin

```python
from core.message import Message
from core.service import MessageHandler, MessagingService

class MyPlugin(MessageHandler):
    async def handle(self, message: Message, service: MessagingService) -> bool:
        if message.content == "/hello":
            await service.send_message(chat_id=message.chat.id, text="Hello!")
            return True
        return False
```

Add it in `main.py` plugin map and enable in config.

## License

Dual-licensed under MIT OR Apache-2.0.

- See repository root: `../LICENSE-MIT` and `../LICENSE-APACHE`.
- Attribution: © 2025 Kai Gartner.
- Community & Commercial support: see `../SUPPORT.md` and the Telegram Support Group badge at top.
