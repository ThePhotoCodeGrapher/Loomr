import time
from typing import Any, Dict, Optional
from urllib.parse import quote_plus

from core.message import Message
from core.service import MessageHandler, MessagingService
from core.group_meter import GroupMeter


class GroupActivation(MessageHandler):
    """
    Gates group usage until activation via TON top-up or activation code.

    Commands (group chats):
      - /ga_invoice [amountTON]
      - /activate <code>
      - /ga_status
    Behavior:
      - For each inbound group message, if inactive or credits insufficient, block and prompt to activate/top-up.
      - When active, deduct inbound credits based on configured in_cost_per_kb_credits.
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config or {}
        self.meter = GroupMeter(self.config)
        ga = (self.config or {}).get("group_activation", {})
        self.cooldown_s: int = int(ga.get("topup_message_cooldown_s") or 3600)
        self._last_notify: Dict[str, float] = {}

    async def handle(self, message: Message, service: MessagingService) -> bool:
        chat = message.chat
        # Only process group/supergroup
        if not chat or str(getattr(chat, "type", "")) not in {"group", "supergroup"}:
            return False

        text = (message.content or "").strip()
        chat_id = str(chat.id)

        # Commands
        if text.startswith("/ga_invoice"):
            return await self._cmd_invoice(text, chat_id, message, service)
        if text.startswith("/activate "):
            return await self._cmd_activate(text, chat_id, message, service)
        if text.startswith("/ga_status"):
            return await self._cmd_status(chat_id, message, service)
        if text.startswith("/ga_plan"):
            return await self._cmd_plan(chat_id, message, service)
        if text.startswith("/ga_upgrade"):
            return await self._cmd_upgrade(text, chat_id, message, service)

        # Metering for non-command group traffic
        status = self.meter.get_status(chat_id)
        if not status.active:
            return await self._maybe_prompt_activation(chat_id, message, service, reason="inactive")

        # Spend inbound credits based on payload size
        payload_len = len(message.content or "")
        ok, remaining = self.meter.spend_inbound(chat_id, payload_len)
        if not ok:
            return await self._maybe_prompt_activation(chat_id, message, service, reason="no_credits")
        # Allow other plugins to handle
        return False

    async def _cmd_invoice(self, text: str, chat_id: str, message: Message, service: MessagingService) -> bool:
        parts = text.split()
        amount = None
        if len(parts) >= 2:
            try:
                amount = float(parts[1])
            except Exception:
                amount = None
        # Build a hint to use TON payment with tag group:<chat_id>
        product_tag = f"group:{chat_id}"
        # If amount is not set, tell admin about min_activation_ton
        min_ton = self.meter.min_activation_ton
        amount = amount or min_ton
        nano = int(round(amount * 1_000_000_000))
        note = quote_plus(product_tag)
        recipient = ((self.config or {}).get("crypto", {}) or {}).get("ton", {}) or {}
        to_addr = str(recipient.get("recipient") or "")
        if not to_addr:
            await service.send_message(chat_id=chat_id, text="TON recipient not configured by admin.")
            return True
        ton_uri = f"ton://transfer/{to_addr}?amount={nano}&text={note}"
        tonhub = f"https://tonhub.com/transfer/{to_addr}?amount={nano}&text={note}"
        # Try sending QR if platform supports images
        await self._maybe_send_qr(service, chat_id, tonhub, caption=(
            "Group activation invoice:\n"
            f"Amount: {amount} TON\n"
            f"Tag: {product_tag}"
        ))
        await service.send_message(
            chat_id=chat_id,
            text=(
                "Group activation invoice:\n"
                f"Amount: {amount} TON\n"
                f"Tag: {product_tag}\n\n"
                f"Open in wallet: {ton_uri}\n"
                f"Web link: {tonhub}\n\n"
                f"After payment, an admin runs: /ton_check <tx_hash> {product_tag}"
            ),
            reply_to_message_id=message.message_id,
        )
        return True

    async def _cmd_activate(self, text: str, chat_id: str, message: Message, service: MessagingService) -> bool:
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await service.send_message(chat_id=chat_id, text="Usage: /activate <code>")
            return True
        code = parts[1].strip()
        ok = self.meter.activate_with_code(chat_id, code)
        if ok:
            s = self.meter.get_status(chat_id)
            await service.send_message(chat_id=chat_id, text=f"Group activated via code. Credits: {s.credits}")
        else:
            await service.send_message(chat_id=chat_id, text="Invalid or unavailable activation code.")
        return True

    async def _cmd_status(self, chat_id: str, message: Message, service: MessagingService) -> bool:
        s = self.meter.get_status(chat_id)
        in_cost, out_cost, profile = self.meter.get_effective_rates(chat_id)
        await service.send_message(
            chat_id=chat_id,
            text=(
                "Group status:\n"
                f"Active: {s.active}\n"
                f"Credits: {s.credits}\n"
                f"Profile: {profile}\n"
                f"Plan expires: {s.plan_expires_at}\n"
                f"Rates (credits/KB): in={in_cost} out={out_cost}"
            ),
            reply_to_message_id=message.message_id,
        )
        return True

    async def _cmd_plan(self, chat_id: str, message: Message, service: MessagingService) -> bool:
        s = self.meter.get_status(chat_id)
        in_cost, out_cost, profile = self.meter.get_effective_rates(chat_id)
        await service.send_message(
            chat_id=chat_id,
            text=(
                "Current plan:\n"
                f"Profile: {profile}\n"
                f"Plan expires: {s.plan_expires_at or 'none'}\n"
                f"Rates (credits/KB): in={in_cost} out={out_cost}\n\n"
                "Upgrade: /ga_upgrade pro [months]"
            ),
            reply_to_message_id=message.message_id,
        )
        return True

    async def _cmd_upgrade(self, text: str, chat_id: str, message: Message, service: MessagingService) -> bool:
        parts = text.split()
        if len(parts) < 2:
            await service.send_message(chat_id=chat_id, text="Usage: /ga_upgrade <profile> [months]")
            return True
        profile = parts[1].strip()
        months = 1
        if len(parts) >= 3:
            try:
                months = int(parts[2])
            except Exception:
                months = 1
        # Build product id for TON upgrade
        product_id = f"group_upgrade:{chat_id}:{profile}:{months}"
        recipient = ((self.config or {}).get("crypto", {}) or {}).get("ton", {}) or {}
        to_addr = str(recipient.get("recipient") or "")
        if not to_addr:
            await service.send_message(chat_id=chat_id, text="TON recipient not configured by admin.")
            return True
        # Price from config.group_activation.plans.<profile>.monthly_ton
        ga = (self.config or {}).get("group_activation", {})
        plans = (ga.get("plans") or {})
        plan = (plans.get(profile) or {})
        monthly_ton = float(plan.get("monthly_ton") or 0.0)
        amount = max(0.0, monthly_ton * float(months))
        if amount <= 0:
            await service.send_message(chat_id=chat_id, text="Admin has not configured pricing for this plan.")
            return True
        nano = int(round(amount * 1_000_000_000))
        note = quote_plus(product_id)
        ton_uri = f"ton://transfer/{to_addr}?amount={nano}&text={note}"
        tonhub = f"https://tonhub.com/transfer/{to_addr}?amount={nano}&text={note}"
        await self._maybe_send_qr(service, chat_id, tonhub, caption=(
            f"Upgrade to {profile} for {months} month(s): {amount} TON"
        ))
        await service.send_message(
            chat_id=chat_id,
            text=(
                "Plan upgrade invoice:\n"
                f"Profile: {profile}\n"
                f"Months: {months}\n"
                f"Amount: {amount} TON\n"
                f"Product: {product_id}\n\n"
                f"Open in wallet: {ton_uri}\n"
                f"Web link: {tonhub}\n\n"
                f"After payment, run: /ton_check <tx_hash> {product_id}"
            ),
            reply_to_message_id=message.message_id,
        )
        return True

    async def _maybe_prompt_activation(self, chat_id: str, message: Message, service: MessagingService, reason: str) -> bool:
        now = time.time()
        last = self._last_notify.get(chat_id) or 0
        if now - last < self.cooldown_s:
            # Silently drop to avoid spam, but consume message
            return True
        self._last_notify[chat_id] = now
        s = self.meter.get_status(chat_id)
        if reason == "inactive":
            await service.send_message(
                chat_id=chat_id,
                text=(
                    "This bot is not activated in this group.\n"
                    "Admins: pay TON to activate with /ga_invoice or use /activate <code>."
                ),
            )
        else:
            await service.send_message(
                chat_id=chat_id,
                text=(
                    "Credits exhausted.\n"
                    "Admins: top up via /ga_invoice [amountTON] or pay then /ton_check <tx> group:<chat_id>."
                ).replace("<chat_id>", chat_id),
            )
        return True

    async def _maybe_send_qr(self, service: MessagingService, chat_id: str, url: str, caption: Optional[str] = None) -> None:
        """Best-effort QR code send for platforms supporting images."""
        try:
            import qrcode
            from io import BytesIO
            img = qrcode.make(url)
            bio = BytesIO()
            img.save(bio, format="PNG")
            photo_bytes = bio.getvalue()
            send_photo = getattr(service, "send_photo", None)
            if callable(send_photo):
                await send_photo(chat_id=chat_id, photo_bytes=photo_bytes, caption=caption)
        except Exception:
            pass
