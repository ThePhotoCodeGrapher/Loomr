#!/usr/bin/env bash
set -euo pipefail
HOST=${HOST:-http://127.0.0.1:8090}
AUTH=${AUTH:-dev-demo-bearer}
TX_HASH=${TX_HASH:-0xabc}
PRODUCT_ID=${PRODUCT_ID:-p1}

curl -sS -H "Authorization: Bearer ${AUTH}" -H 'Content-Type: application/json' \
  -X POST "${HOST}/ton/verify" \
  -d "{\"tx_hash\":\"${TX_HASH}\",\"product_id\":\"${PRODUCT_ID}\"}"

echo "\nOK"
