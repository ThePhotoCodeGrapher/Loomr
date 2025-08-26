#!/usr/bin/env bash
set -euo pipefail
HOST=${HOST:-http://127.0.0.1:8090}
AUTH=${AUTH:-dev-demo-bearer}
CHAT_ID=${CHAT_ID:--100123456}
PROFILE=${PROFILE:-pro}
MONTHS=${MONTHS:-1}
TX_HASH=${TX_HASH:-0xabc}
PRODUCT_ID=${PRODUCT_ID:-"group_upgrade:${CHAT_ID}:${PROFILE}:${MONTHS}"}

curl -sS -H "Authorization: Bearer ${AUTH}" -H 'Content-Type: application/json' \
  -X POST "${HOST}/group/upgrade" \
  -d "{\"chat_id\":\"${CHAT_ID}\",\"profile\":\"${PROFILE}\",\"months\":${MONTHS},\"tx_hash\":\"${TX_HASH}\",\"product_id\":\"${PRODUCT_ID}\"}"

echo "\nOK"
