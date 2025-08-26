import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote_plus

from core.message import Message
from core.service import MessageHandler, MessagingService
from core.event_bus import bus as event_bus
from core.group_meter import GroupMeter


@dataclass
class TonConfig:
    recipient: str
    confirmations: int = 1
    api_base: str = "https://tonapi.io"
    api_key: Optional[str] = None


class TonWatcher(MessageHandler):
    """
    On-chain TON payment checker.

    Command:
      - /ton_check <tx_hash> <product_id>

    Behavior:
      - Confirms TON transfer to configured recipient and amount >= required for product_id.
      - On success: emits payment event, optionally assigns role/subscription via ProductCatalog config,
        and triggers ProductCatalog HTTP delivery if enabled.
    """

    def __init__(self, config: Dict[str, Any], products_cfg: Dict[str, Any]):
        # Keep root config for downstream helpers (GroupMeter)
        self._config_root = config or {}
        self.config = config or {}
        ton_cfg = (config or {}).get("crypto", {}).get("ton", {})
        self.cfg = TonConfig(
            recipient=str(ton_cfg.get("recipient") or "").strip(),
            confirmations=int(ton_cfg.get("confirmations") or 1),
            api_base=str(ton_cfg.get("api_base") or "https://tonapi.io").rstrip("/"),
            api_key=str(ton_cfg.get("api_key")) if ton_cfg.get("api_key") else None,
        )
        self.price_map: Dict[str, float] = ton_cfg.get("price_map") or {}
        # Admin list for gating
        at = (config or {}).get("admin_tools") or {}
        self.admins = {str(a) for a in (at.get("admins") or [])}
        # Optional integration with ProductCatalog
        self.products_delivery = (products_cfg or {}).get("delivery") or {}
        self.role_map: Dict[str, str] = (products_cfg or {}).get("roles") or {}
        self.sub_days: Dict[str, int] = (products_cfg or {}).get("subscription_days") or {}
        # Per-product wallet overrides
        from pathlib import Path
        self.wallet_store_path = Path(Path(__file__).resolve().parent.parent / "config/wallets.json")
        # Persistent TON stats (by UTC date)
        self.stats_path = Path(Path(__file__).resolve().parent.parent / "config/ton_stats.json")

    async def handle(self, message: Message, service: MessagingService) -> bool:
        text = (message.content or "").strip()

        # Admin-only TON invoice generator
        if text.startswith("/ton_invoice"):
            parts = text.split()
            # Usage: /ton_invoice <product_id> [amountTON]
            if len(parts) < 2:
                await service.send_message(chat_id=message.chat.id, text="Usage: /ton_invoice <product_id> [amountTON]\nTip: reply to a user's message before sending this command to address them.")
                return True
            # Admin-only guard
            try:
                user_id = str(message.from_user.id)
            except Exception:
                user_id = ""
            if self.admins and user_id not in self.admins:
                await service.send_message(chat_id=message.chat.id, text="This command is for admins only.")
                return True

            product_id = parts[1]
            # Determine amount: explicit or from price_map
            amount_ton: Optional[float] = None
            if len(parts) >= 3:
                try:
                    amount_ton = float(parts[2])
                except Exception:
                    amount_ton = None
            if amount_ton is None:
                try:
                    amount_ton = float(self.price_map.get(product_id) or 0)
                except Exception:
                    amount_ton = 0.0
            if (amount_ton or 0) <= 0:
                await service.send_message(chat_id=message.chat.id, text=f"No TON price set for product_id '{product_id}'. Provide amount explicitly: /ton_invoice {product_id} 0.001")
                return True

            recipient = self._get_recipient_override(product_id) or self.cfg.recipient
            if not recipient:
                await service.send_message(chat_id=message.chat.id, text="TON recipient address not configured.")
                return True

            # Build transfer links
            nano = int(round((amount_ton or 0.0) * 1_000_000_000))
            note = f"product:{product_id}"
            enc_note = quote_plus(note)
            ton_uri = f"ton://transfer/{recipient}?amount={nano}&text={enc_note}"
            tonhub = f"https://tonhub.com/transfer/{recipient}?amount={nano}&text={enc_note}"

            target_hint = ""
            try:
                if message.reply_to_message and message.reply_to_message.from_user:
                    tu = message.reply_to_message.from_user
                    uname = (tu.username or f"{tu.first_name or ''} {tu.last_name or ''}").strip()
                    target_hint = f" for @{uname}" if uname else ""
            except Exception:
                target_hint = ""

            await service.send_message(
                chat_id=message.chat.id,
                text=(
                    f"Invoice{target_hint}:\n"
                    f"Amount: {amount_ton} TON\n"
                    f"Product: {product_id}\n"
                    f"Recipient: {recipient}\n\n"
                    f"Open in wallet: {ton_uri}\n"
                    f"Web link: {tonhub}\n\n"
                    f"After payment, run: /ton_check <tx_hash> {product_id}"
                ),
                reply_to_message_id=message.message_id,
            )
            return True

        # Admin-only TON refund link generator
        if text.startswith("/ton_refund"):
            parts = text.split(maxsplit=3)
            # Usage: /ton_refund <to_address> <amountTON> [note]
            if len(parts) < 3:
                await service.send_message(chat_id=message.chat.id, text="Usage: /ton_refund <to_address> <amountTON> [note]")
                return True
            # Admin-only guard
            try:
                user_id = str(message.from_user.id)
            except Exception:
                user_id = ""
            if self.admins and user_id not in self.admins:
                await service.send_message(chat_id=message.chat.id, text="This command is for admins only.")
                return True
            to_addr = parts[1].strip()
            try:
                amount_ton = float(parts[2])
            except Exception:
                await service.send_message(chat_id=message.chat.id, text="Invalid amount. Example: /ton_refund UQ... 0.001 optional-note")
                return True
            note = parts[3] if len(parts) >= 4 else "refund"
            nano = int(round(amount_ton * 1_000_000_000))
            enc_note = quote_plus(note)
            ton_uri = f"ton://transfer/{to_addr}?amount={nano}&text={enc_note}"
            tonhub = f"https://tonhub.com/transfer/{to_addr}?amount={nano}&text={enc_note}"
            await service.send_message(
                chat_id=message.chat.id,
                text=(
                    "Refund link generated:\n"
                    f"Amount: {amount_ton} TON\n"
                    f"To: {to_addr}\n\n"
                    f"Open in wallet: {ton_uri}\n"
                    f"Web link: {tonhub}"
                ),
                reply_to_message_id=message.message_id,
            )
            return True

        # Admin-only TON stats
        if text.startswith("/ton_stats"):
            parts = text.split()
            # Admin-only guard
            try:
                user_id = str(message.from_user.id)
            except Exception:
                user_id = ""
            if self.admins and user_id not in self.admins:
                await service.send_message(chat_id=message.chat.id, text="This command is for admins only.")
                return True
            target = parts[1] if len(parts) >= 2 else "today"
            from datetime import datetime, timezone
            if target == "today":
                day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            else:
                day = target
            stats = self._load_stats()
            day_stat = (stats.get("days") or {}).get(day) or {"count": 0, "sum": 0.0}
            # Overall totals
            total_count = 0
            total_sum = 0.0
            for v in (stats.get("days") or {}).values():
                try:
                    total_count += int(v.get("count") or 0)
                    total_sum += float(v.get("sum") or 0.0)
                except Exception:
                    pass
            await service.send_message(
                chat_id=message.chat.id,
                text=(
                    "TON Stats\n"
                    f"Day: {day}\n"
                    f"  Count: {day_stat.get('count', 0)}\n"
                    f"  Sum: {day_stat.get('sum', 0.0)} TON\n\n"
                    f"Overall Count: {total_count}\n"
                    f"Overall Sum: {round(total_sum, 12)} TON"
                ),
                reply_to_message_id=message.message_id,
            )
            return True

        if not text.startswith("/ton_check"):
            return False
        parts = text.split()
        if len(parts) < 3:
            await service.send_message(chat_id=message.chat.id, text="Usage: /ton_check <tx_hash> <product_id>")
            return True
        # Admin-only guard
        try:
            user_id = str(message.from_user.id)
        except Exception:
            user_id = ""
        if self.admins and user_id not in self.admins:
            await service.send_message(chat_id=message.chat.id, text="This command is for admins only.")
            return True
        tx_hash = parts[1]
        product_id = parts[2]
        # Group activation path: product_id like 'group:<chat_id>'
        is_group_activation = product_id.startswith("group:")
        # Group upgrade path: product_id like 'group_upgrade:<chat_id>:<profile>[:months]'
        is_group_upgrade = product_id.startswith("group_upgrade:")
        meter: Optional[GroupMeter] = None
        if is_group_activation or is_group_upgrade:
            meter = GroupMeter(self._full_config())
            if is_group_activation:
                required = float(meter.min_activation_ton)
            else:
                # parse upgrade pricing from config plans
                try:
                    _, chat_target, profile, *rest = product_id.split(":")
                    months = int(rest[0]) if rest else 1
                except Exception:
                    months = 1
                ga = (self._full_config() or {}).get("group_activation", {})
                plans = ga.get("plans") or {}
                plan = plans.get(profile) or {}
                monthly_ton = float(plan.get("monthly_ton") or 0.0)
                required = monthly_ton * max(1, months)
        else:
            required = float(self.price_map.get(product_id) or 0)
        if required <= 0:
            await service.send_message(chat_id=message.chat.id, text=f"No TON price set for product_id '{product_id}'.")
            return True
        recipient = self._get_recipient_override(product_id) or self.cfg.recipient
        if not recipient:
            await service.send_message(chat_id=message.chat.id, text="TON recipient address not configured.")
            return True

        ok, amount, confs, note = await self._verify_ton(tx_hash, recipient, required)
        if not ok:
            await service.send_message(chat_id=message.chat.id, text=f"Not confirmed or invalid. {note}")
            return True

        # Update stats (UTC day)
        try:
            self._bump_stats(amount)
        except Exception:
            pass

        # Assign role/subscription (only for normal product ids)
        assigned_note = ""
        if not is_group_activation:
            try:
                role = self.role_map.get(product_id)
                if role:
                    days = self.sub_days.get(product_id)
                    from core import user_store
                    user_store.set_role(user_id=str(message.from_user.id), role=role, days=days)
                    assigned_note = f"Role assigned: {role}" + (f" (+{days}d)" if days else "")
            except Exception:
                assigned_note = ""

        # Emit event
        try:
            await event_bus.emit(
                "payment.ton.confirmed",
                {
                    "txid": tx_hash,
                    "product_id": product_id,
                    "amount": float(amount),
                    "confirmations": int(confs),
                    "user_id": str(message.from_user.id),
                    "chat_id": str(message.chat.id),
                    "recipient": recipient,
                },
            )
        except Exception:
            pass

        # For group activation: activate and credit the group
        group_note = ""
        if is_group_activation and meter is not None:
            try:
                chat_target = product_id.split(":", 1)[1]
                meter.activate(chat_target)
                credits = meter.credits_for_ton(amount)
                meter.add_credits(chat_target, credits)
                group_note = f"Group {chat_target} activated. +{credits} credits"
            except Exception as e:
                group_note = f"Group activation error: {e}"
        # For group upgrade: set profile and expiry
        elif is_group_upgrade and meter is not None:
            try:
                _, chat_target, profile, *rest = product_id.split(":")
                months = int(rest[0]) if rest else 1
                # Ensure the group is active
                meter.activate(chat_target)
                meter.set_profile(chat_target, profile=profile, months=months)
                group_note = f"Group {chat_target} upgraded to {profile} for {months} month(s)."
            except Exception as e:
                group_note = f"Group upgrade error: {e}"

        # Trigger ProductCatalog HTTP delivery if configured (only for normal product flow)
        delivered = False
        delivery_note = ""
        if (self.products_delivery.get("mode") or "").lower() == "http" and not is_group_activation:
            try:
                from plugins.product_catalog import ProductCatalog  # type: ignore
                pc = ProductCatalog({"delivery": self.products_delivery})
                ok2, delivery_note = await pc._deliver_via_http(
                    action="crypto",
                    token=tx_hash,
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
                                "txid": tx_hash,
                                "chain": "ton",
                                "product_id": product_id,
                                "user_id": str(message.from_user.id),
                                "chat_id": str(message.chat.id),
                                "note": delivery_note,
                            },
                        )
                    except Exception:
                        pass
            except Exception as e:
                delivery_note = f"delivery call failed: {e}"

        await service.send_message(
            chat_id=message.chat.id,
            text=(
                f"TON payment confirmed\n"
                f"Amount: {amount} TON (>= {required})\n"
                f"Confirmations: {confs}\n"
                + (f"\n{assigned_note}" if assigned_note else "")
                + (f"\nDelivered: {delivered}. {delivery_note}" if delivery_note else "")
                + (f"\n{group_note}" if group_note else "")
            ),
        )
        return True

    def _full_config(self) -> Dict[str, Any]:
        # Provide the root config to GroupMeter for paths and settings
        # TonWatcher was constructed with top-level config as first argument
        # but we only kept subsets; reconstruct a minimal root
        return getattr(self, "_config_root", {"group_activation": (self.config or {}).get("group_activation", {})})

    def _get_recipient_override(self, product_id: str) -> Optional[str]:
        try:
            if not self.wallet_store_path.exists():
                return None
            data = json.loads(self.wallet_store_path.read_text())
            store = data.get("wallets") or {}
            prod = store.get(str(product_id)) or {}
            return str(prod.get("ton")) if prod.get("ton") else None
        except Exception:
            return None

    async def _verify_ton(self, tx_hash: str, recipient: str, required: float) -> Tuple[bool, float, int, str]:
        """Verify TON transaction via tonapi.io. Returns (ok, amount_ton, confirmations, note)."""
        from aiohttp import ClientSession
        headers = {}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"
        url = f"{self.cfg.api_base}/v2/blockchain/transactions/{tx_hash}"
        try:
            async with ClientSession(headers=headers) as sess:
                async with sess.get(url) as resp:
                    if resp.status != 200:
                        return False, 0.0, 0, f"tonapi status {resp.status}"
                    data = await resp.json(content_type=None)
        except Exception as e:
            return False, 0.0, 0, f"fetch error: {e}"
        # Parse transaction: look at out_msgs transfers to recipient
        try:
            out_msgs = data.get("out_msgs") or []
            # Fallback for schema variants
            if not out_msgs and isinstance(data.get("out_msgs_count"), int):
                out_msgs = data.get("out_msgs") or []
            total_nano = 0
            for m in out_msgs:
                to_addr = (m.get("destination") or {}).get("address") or m.get("to") or ""
                if str(to_addr).strip().lower() == recipient.strip().lower():
                    # Value can be under "value" in nanotons
                    val = m.get("value")
                    if isinstance(val, (int, float)):
                        total_nano += int(val)
            amount_ton = float(total_nano) / 1_000_000_000.0
            # Confirmations: tonapi may provide "lt"/block info; if absent, treat as 1
            # Consider transaction finalized if not aborted and block/time exists
            confs = 1
            try:
                if data.get("aborted") is False and (data.get("block") or data.get("now")):
                    confs = max(1, int(self.cfg.confirmations))
            except Exception:
                confs = 1
            ok = (amount_ton + 1e-12) >= required
            return ok, amount_ton, confs, "ok" if ok else "insufficient amount"
        except Exception as e:
            return False, 0.0, 0, f"parse error: {e}"

    # --- Stats helpers ---
    def _load_stats(self) -> Dict[str, Any]:
        try:
            if not self.stats_path.exists():
                return {"days": {}}
            return json.loads(self.stats_path.read_text()) or {"days": {}}
        except Exception:
            return {"days": {}}

    def _save_stats(self, data: Dict[str, Any]) -> None:
        try:
            self.stats_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception:
            pass

    def _bump_stats(self, amount_ton: float) -> None:
        from datetime import datetime, timezone
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        data = self._load_stats()
        days = data.setdefault("days", {})
        cur = days.setdefault(day, {"count": 0, "sum": 0.0})
        try:
            cur["count"] = int(cur.get("count", 0)) + 1
        except Exception:
            cur["count"] = 1
        try:
            cur["sum"] = float(cur.get("sum", 0.0)) + float(amount_ton or 0.0)
        except Exception:
            cur["sum"] = float(amount_ton or 0.0)
        self._save_stats(data)
