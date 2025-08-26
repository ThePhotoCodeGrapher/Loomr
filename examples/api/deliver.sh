#!/usr/bin/env bash
set -euo pipefail
HOST=${HOST:-http://127.0.0.1:8090}
AUTH=${AUTH:-dev-demo-bearer}
USER_ID=${USER_ID:-"721254241"}
CHAT_ID=${CHAT_ID:-"721254241"}
PRODUCT_ID=${PRODUCT_ID:-"p1"}
TOKEN=${TOKEN:-"deliver-abc"}

curl -sS -H "Authorization: Bearer ${AUTH}" -H 'Content-Type: application/json' \
  -X POST "${HOST}/deliver" \
  -d "{\"action\":\"deliver\",\"user_id\":\"${USER_ID}\",\"chat_id\":\"${CHAT_ID}\",\"product_id\":\"${PRODUCT_ID}\",\"token\":\"${TOKEN}\"}"

echo "\nOK"
