import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from core.message import Message
from core.service import MessageHandler, MessagingService
from core.event_bus import bus as event_bus


@dataclass
class ChainConfig:
    name: str
    recipient: str
    confirmations: int = 1
    contract: Optional[str] = None  # USDT contract
    api_key: Optional[str] = None


class CryptoWatcher(MessageHandler):
    """
    Minimal USDT payment checker.

    Commands:
      - /usdt_check <txid> <product_id> [chain]
        chain: eth|bsc|polygon|tron (default from config.default_chain)

    On success:
      - Confirms token transfer to configured recipient, amount >= required for product_id,
        and confirmations >= min. Replies success; optionally triggers ProductCatalog HTTP delivery
        if configured under products.delivery.mode=http.
    """

    def __init__(self, config: Dict[str, Any], products_cfg: Dict[str, Any]):
        self.cfg = (config or {}).get("crypto", {}).get("usdt", {})
        self.default_chain = (self.cfg.get("default_chain") or "eth").lower()
        self.price_map = self.cfg.get("price_map") or {}  # product_id -> amount (USDT)
        self.chains: Dict[str, ChainConfig] = {}
        for key in ("eth", "bsc", "polygon", "tron"):
            c = self.cfg.get(key) or {}
            if not c:
                continue
            self.chains[key] = ChainConfig(
                name=key,
                recipient=str(c.get("recipient")),
                confirmations=int(c.get("confirmations") or 1),
                contract=str(c.get("contract")) if c.get("contract") else None,
                api_key=str(c.get("api_key")) if c.get("api_key") else None,
            )
        # Optionally integrate with ProductCatalog delivery
        self.products_delivery = (products_cfg or {}).get("delivery") or {}
        # Role/tier mapping and subscriptions
        self.role_map: Dict[str, str] = (products_cfg or {}).get("roles") or {}
        self.sub_days: Dict[str, int] = (products_cfg or {}).get("subscription_days") or {}
        # Per-product wallet overrides
        from pathlib import Path
        self.wallet_store_path = Path(Path(__file__).resolve().parent.parent / "config/wallets.json")

    async def handle(self, message: Message, service: MessagingService) -> bool:
        text = (message.content or "").strip()
        if not text.startswith("/usdt_check"):
            return False
        parts = text.split()
        if len(parts) < 3:
            await service.send_message(chat_id=message.chat.id, text="Usage: /usdt_check <txid> <product_id> [chain]")
            return True
        txid = parts[1]
        product_id = parts[2]
        chain_key = (parts[3].lower() if len(parts) >= 4 else self.default_chain)
        if chain_key not in self.chains:
            await service.send_message(chat_id=message.chat.id, text=f"Unsupported chain '{chain_key}'. Use eth|bsc|polygon|tron.")
            return True
        price_required = float(self.price_map.get(product_id) or 0)
        if price_required <= 0:
            await service.send_message(chat_id=message.chat.id, text=f"No price set for product_id '{product_id}'.")
            return True
        recipient_override = self._get_recipient_override(product_id, chain_key)
        ok, amount, confs, msg = await self._verify_usdt(chain_key, txid, price_required, recipient_override)
        if not ok:
            await service.send_message(chat_id=message.chat.id, text=f"Not confirmed yet or invalid. {msg}")
            return True
        # Success: assign role/subscription if mapped
        assigned_note = ""
        seller_note = ""
        try:
            role = self.role_map.get(product_id)
            if role:
                days = self.sub_days.get(product_id)
                from core import user_store  # lazy import
                user_store.set_role(user_id=str(message.from_user.id), role=role, days=days)
                assigned_note = f"Role assigned: {role}" + (f" (+{days}d)" if days else "")
        except Exception:
            assigned_note = ""

        # Emit payment confirmed event early for external systems
        try:
            await event_bus.emit(
                "payment.usdt.confirmed",
                {
                    "txid": txid,
                    "chain": chain_key,
                    "product_id": product_id,
                    "amount": float(amount),
                    "confirmations": int(confs),
                    "user_id": str(message.from_user.id),
                    "chat_id": str(message.chat.id),
                    "recipient": (recipient_override or self.chains[chain_key].recipient or ""),
                },
            )
        except Exception:
            pass

        # Credit seller if funds went to platform/global wallet (not directly to seller)
        try:
            from core import seller_store
            seller_id = seller_store.get_product_seller(product_id)
            if seller_id:
                seller_wallet = seller_store.get_seller_wallet(seller_id, chain_key)
                # Determine the recipient address used for verification
                want = (recipient_override or self.chains[chain_key].recipient or "").lower()
                if seller_wallet and want == seller_wallet.lower():
                    seller_note = f"Paid directly to seller (seller_id={seller_id}). No credit needed."
                else:
                    # Consider the received amount as credit (could be fee-adjusted later)
                    seller_store.credit_seller(seller_id=seller_id, amount=float(amount), product_id=product_id, txid=txid, chain=chain_key)
                    seller_info = seller_store.get_seller_info(seller_id)
                    bal = float(seller_info.get("balance") or 0.0)
                    seller_note = f"Seller credited {amount:.2f} USDT (seller_id={seller_id}). Balance: {bal:.2f}."
        except Exception:
            # Do not fail the flow if crediting errors occur
            pass

        # Optionally trigger ProductCatalog HTTP delivery
        delivered = False
        note = ""
        if (self.products_delivery.get("mode") or "").lower() == "http":
            try:
                # Lazy import to avoid hard dep
                from plugins.product_catalog import ProductCatalog  # type: ignore
                pc = ProductCatalog({"delivery": self.products_delivery})
                ok2, note = await pc._deliver_via_http(
                    action="crypto",
                    token=txid,
                    user_id=str(message.from_user.id),
                    chat_id=str(message.chat.id),
                    product_id=product_id,
                )
                delivered = ok2
                if delivered:
                    try:
                        await event_bus.emit(
                            "delivery.sent",
                            {
                                "txid": txid,
                                "chain": chain_key,
                                "product_id": product_id,
                                "user_id": str(message.from_user.id),
                                "chat_id": str(message.chat.id),
                                "note": note,
                            },
                        )
                    except Exception:
                        pass
            except Exception as e:
                note = f"delivery call failed: {e}"
        await service.send_message(
            chat_id=message.chat.id,
            text=(
                f"USDT payment confirmed on {chain_key.upper()}\n"
                f"Amount: {amount} USDT (>= {price_required})\n"
                f"Confirmations: {confs}\n"
                + (f"\n{assigned_note}" if assigned_note else "")
                + (f"\n{seller_note}" if seller_note else "")
                + (f"\nDelivered: {delivered}. {note}" if note else "")
            ),
        )
        return True

    async def _verify_usdt(self, chain_key: str, txid: str, price_required: float, recipient_override: Optional[str] = None):
        cfg = self.chains[chain_key]
        if chain_key in ("eth", "bsc", "polygon"):
            return await self._verify_usdt_evm(chain_key, cfg, txid, price_required, recipient_override)
        if chain_key == "tron":
            return await self._verify_usdt_tron(cfg, txid, price_required, recipient_override)
        return False, 0.0, 0, "Unsupported chain"

    async def _verify_usdt_evm(self, chain_key: str, cfg: ChainConfig, txid: str, price_required: float, recipient_override: Optional[str]):
        from aiohttp import ClientSession
        # Etherscan-like API bases
        base_map = {
            "eth": "https://api.etherscan.io/api",
            "bsc": "https://api.bscscan.com/api",
            "polygon": "https://api.polygonscan.com/api",
        }
        base = base_map[chain_key]
        params = {
            "module": "account",
            "action": "tokentx",
            "contractaddress": (cfg.contract or ""),
            "txhash": txid,
            "apikey": cfg.api_key or "",
        }
        async with ClientSession() as sess:
            async with sess.get(base, params=params) as resp:
                data = await resp.json(content_type=None)
        status = str(data.get("status"))
        if status != "1":
            return False, 0.0, 0, data.get("message") or data.get("result")
        txs = data.get("result") or []
        # Find transfer to recipient
        want = (recipient_override or cfg.recipient or "").lower()
        for t in txs:
            if want == str(t.get("to", "")).lower():
                # value is integer with token decimals
                decimals = int(t.get("tokenDecimal") or 6)
                value = float(int(t.get("value")) / (10 ** decimals))
                # get confirmations
                confs = int(t.get("confirmations") or 0)
                if value + 1e-9 >= price_required and confs >= cfg.confirmations:
                    return True, value, confs, "ok"
        return False, 0.0, 0, "No matching USDT transfer to recipient or insufficient confirmations"

    async def _verify_usdt_tron(self, cfg: ChainConfig, txid: str, price_required: float, recipient_override: Optional[str]):
        from aiohttp import ClientSession
        # TronScan API for TRC20 transaction by hash
        url = "https://apilist.tronscanapi.com/api/transaction-info"
        params = {"hash": txid}
        async with ClientSession() as sess:
            async with sess.get(url, params=params) as resp:
                data = await resp.json(content_type=None)
        # Extract contract transfers
        contracts = data.get("contract_map", {})
        # Fallback: token transfers list
        transfers = data.get("tokenTransferInfo", []) or data.get("trc20TransferInfo", [])
        recipient_ok = False
        amount_val = 0.0
        want = (recipient_override or cfg.recipient or "").lower()
        for tr in transfers:
            to_addr = (tr.get("to_address") or tr.get("to_address_base58") or "").lower()
            if to_addr == want:
                decimals = int(tr.get("decimals") or 6)
                raw = tr.get("amount_str") or tr.get("amount") or "0"
                try:
                    raw_int = int(raw)
                except Exception:
                    raw_int = int(float(raw))
                amount_val = float(raw_int / (10 ** decimals))
                recipient_ok = True
                break
        if not recipient_ok:
            return False, 0.0, 0, "No transfer to recipient"
        confs = int(data.get("confirmations") or 0)
        if amount_val + 1e-9 >= price_required and confs >= cfg.confirmations:
            return True, amount_val, confs, "ok"
        return False, amount_val, confs, "Insufficient amount or confirmations"

    def _get_recipient_override(self, product_id: str, chain_key: str) -> Optional[str]:
        try:
            if not self.wallet_store_path.exists():
                return None
            data = json.loads(self.wallet_store_path.read_text())
            store = data.get("wallets") or {}
            prod = store.get(str(product_id)) or {}
            addr = prod.get(chain_key)
            return str(addr) if addr else None
        except Exception:
            return None
