import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

STORE_PATH = Path(__file__).resolve().parent.parent / "config/sellers.json"
STORE_PATH.parent.mkdir(parents=True, exist_ok=True)


def _load() -> Dict[str, Any]:
    try:
        if STORE_PATH.exists():
            return json.loads(STORE_PATH.read_text())
    except Exception:
        pass
    return {"sellers": {}, "product_sellers": {}, "payouts": []}


def _save(data: Dict[str, Any]) -> None:
    try:
        STORE_PATH.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


def set_product_seller(product_id: str, seller_id: str) -> None:
    data = _load()
    data.setdefault("product_sellers", {})[str(product_id)] = str(seller_id)
    # ensure seller exists
    sellers = data.setdefault("sellers", {})
    sellers.setdefault(str(seller_id), {"wallets": {}, "balance": 0.0})
    _save(data)


def get_product_seller(product_id: str) -> Optional[str]:
    data = _load()
    return (data.get("product_sellers") or {}).get(str(product_id))


def set_seller_wallet(seller_id: str, chain: str, address: str) -> None:
    data = _load()
    sellers = data.setdefault("sellers", {})
    s = sellers.setdefault(str(seller_id), {"wallets": {}, "balance": 0.0})
    wallets = s.setdefault("wallets", {})
    wallets[str(chain).lower()] = str(address)
    _save(data)


def get_seller_wallet(seller_id: str, chain: str) -> Optional[str]:
    data = _load()
    s = (data.get("sellers") or {}).get(str(seller_id)) or {}
    w = (s.get("wallets") or {}).get(str(chain).lower())
    return str(w) if w else None


def credit_seller(seller_id: str, amount: float, product_id: Optional[str] = None, txid: Optional[str] = None, chain: Optional[str] = None) -> None:
    data = _load()
    sellers = data.setdefault("sellers", {})
    s = sellers.setdefault(str(seller_id), {"wallets": {}, "balance": 0.0})
    s["balance"] = float(s.get("balance") or 0.0) + float(amount or 0.0)
    # optional ledger entry
    payouts = data.setdefault("ledger", [])
    payouts.append({
        "type": "credit",
        "seller_id": str(seller_id),
        "amount": float(amount or 0.0),
        "product_id": str(product_id) if product_id else None,
        "txid": txid,
        "chain": chain,
    })
    _save(data)


def get_seller_info(seller_id: str) -> Dict[str, Any]:
    data = _load()
    sellers = data.get("sellers") or {}
    s = sellers.get(str(seller_id)) or {"wallets": {}, "balance": 0.0}
    return {"seller_id": str(seller_id), **s}


def list_payouts(status: Optional[str] = None) -> List[Dict[str, Any]]:
    data = _load()
    arr = data.get("payouts") or []
    if status:
        return [p for p in arr if str(p.get("status")) == status]
    return arr


def request_payout(seller_id: str, amount: float, note: Optional[str] = None) -> Tuple[bool, str, Optional[str]]:
    data = _load()
    sellers = data.setdefault("sellers", {})
    s = sellers.setdefault(str(seller_id), {"wallets": {}, "balance": 0.0})
    bal = float(s.get("balance") or 0.0)
    amount = float(amount or 0.0)
    if amount <= 0:
        return False, "Amount must be > 0", None
    if amount > bal + 1e-9:
        return False, f"Insufficient balance ({bal:.2f})", None
    pid = f"p{len(data.get('payouts') or []) + 1}"
    p = {
        "id": pid,
        "seller_id": str(seller_id),
        "amount": amount,
        "note": note,
        "status": "pending",
    }
    data.setdefault("payouts", []).append(p)
    _save(data)
    return True, "Payout requested", pid


def approve_payout(payout_id: str) -> Tuple[bool, str]:
    data = _load()
    payouts = data.setdefault("payouts", [])
    for p in payouts:
        if str(p.get("id")) == str(payout_id):
            if p.get("status") != "pending":
                return False, "Payout not pending"
            seller_id = str(p.get("seller_id"))
            amount = float(p.get("amount") or 0.0)
            sellers = data.setdefault("sellers", {})
            s = sellers.setdefault(seller_id, {"wallets": {}, "balance": 0.0})
            bal = float(s.get("balance") or 0.0)
            if amount > bal + 1e-9:
                return False, "Insufficient balance to approve"
            s["balance"] = bal - amount
            p["status"] = "approved"
            _save(data)
            return True, "Payout approved"
    return False, "Payout not found"
