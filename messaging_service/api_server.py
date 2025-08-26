from __future__ import annotations

import os
from typing import Any, Dict, Optional
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

APP_TITLE = "Messaging Service API"

app = FastAPI(title=APP_TITLE, version="0.1.0")


class DeliverRequest(BaseModel):
    action: Optional[str] = "deliver"
    token: Optional[str] = None
    user_id: Optional[str] = None
    chat_id: Optional[str] = None
    product_id: Optional[str] = None
    amount: Optional[int] = None
    currency: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None


class TonVerifyRequest(BaseModel):
    tx_hash: str
    recipient: Optional[str] = None
    min_amount_ton: Optional[float] = None
    product_id: Optional[str] = None
    note: Optional[str] = None

class GroupUpgradeRequest(BaseModel):
    """
    Schema for documenting group plan upgrades handled by the Telegram TonWatcher.
    The actual on-chain verification and state update are performed by the bot plugin,
    this endpoint is for documentation/demo purposes only.
    """
    chat_id: str
    profile: str                   # e.g., "pro"
    months: Optional[int] = 1      # duration in months
    tx_hash: Optional[str] = None  # optional reference
    product_id: Optional[str] = None  # expected format: group_upgrade:<chat_id>:<profile>[:months]


@app.get("/")
def root() -> Dict[str, str]:
    return {"ok": "true", "service": APP_TITLE}


@app.post("/deliver")
def deliver(payload: DeliverRequest, authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    # Optional simple auth via env var DELIVER_BEARER
    expected_bearer = os.getenv("DELIVER_BEARER")
    if expected_bearer:
        if not authorization or not authorization.startswith("Bearer ") or authorization.split(" ", 1)[1] != expected_bearer:
            raise HTTPException(status_code=401, detail="unauthorized")

    # Simulate token verification and return a license/download link
    token = payload.token or ""
    product = payload.product_id or ""
    if not token or not product:
        raise HTTPException(status_code=400, detail="token and product_id required")

    # In real use, validate token and lookup a deliverable
    return {
        "message": f"Delivery ready for {product}",
        "license": f"LIC-{token[:8].upper()}-DEMO",
        "download_url": f"https://example.com/dl/{product}/{token}",
    }


@app.post("/ton/verify")
def ton_verify(payload: TonVerifyRequest, authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    """
    Minimal TON verification stub for documentation/demo purposes.
    This endpoint does NOT query chain; it's provided for external systems to post
    their own verification results and keep a unified schema.

    In production, verification is handled by the Telegram plugin `TonWatcher` using tonapi.io.
    """
    # Optional simple auth via env var DELIVER_BEARER
    expected_bearer = os.getenv("DELIVER_BEARER")
    if expected_bearer:
        if not authorization or not authorization.startswith("Bearer ") or authorization.split(" ", 1)[1] != expected_bearer:
            raise HTTPException(status_code=401, detail="unauthorized")

    if not payload.tx_hash:
        raise HTTPException(status_code=400, detail="tx_hash required")

    # Echo back a simulated response for visibility in Swagger/ReDoc
    return {
        "ok": True,
        "tx_hash": payload.tx_hash,
        "recipient": payload.recipient,
        "min_amount_ton": payload.min_amount_ton,
        "product_id": payload.product_id,
        "note": payload.note or "stub only; plugin performs real verification",
    }


@app.post("/group/upgrade")
def group_upgrade(payload: GroupUpgradeRequest, authorization: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    """
    Document the group upgrade payload. The bot's TonWatcher handles real upgrades
    when it sees product_id in the format: group_upgrade:<chat_id>:<profile>[:months].

    This stub echoes the request so it appears in OpenAPI/Swagger/ReDoc.
    """
    # Optional simple auth via env var DELIVER_BEARER (re-use for demo endpoints)
    expected_bearer = os.getenv("DELIVER_BEARER")
    if expected_bearer:
        if not authorization or not authorization.startswith("Bearer ") or authorization.split(" ", 1)[1] != expected_bearer:
            raise HTTPException(status_code=401, detail="unauthorized")

    return {
        "ok": True,
        "chat_id": payload.chat_id,
        "profile": payload.profile,
        "months": payload.months or 1,
        "tx_hash": payload.tx_hash,
        "product_id": payload.product_id,
        "note": "stub only; real upgrade is performed by Telegram plugin TonWatcher",
    }
