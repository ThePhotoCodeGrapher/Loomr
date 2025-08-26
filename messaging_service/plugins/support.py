import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, List

from core.message import Message
from core.service import MessageHandler, MessagingService
from core.event_bus import bus as event_bus


@dataclass
class Ticket:
    id: str
    user_id: str
    chat_id: str
    status: str  # open|closed
    created_at: float
    updated_at: float
    subject: str


class SupportPlugin(MessageHandler):
    """
    Simple support/ticketing bridge.

    User:
      - /support <message> → opens a ticket (if none open) and forwards to admins.

    Admins:
      - /support list → lists open tickets
      - /support close <ticket_id> → close a ticket
      - Reply to a ticket message in the admin group to respond to the user
      - /reply <ticket_id> <text> → reply to a user via DM if not using group

    Config (config.yaml):
      support:
        group_id: ${SUPPORT_GROUP_ID}   # optional admin group chat id (bot must be member)
        store_path: config/support_tickets.json
        notify_admins_dm: true          # DM all admins if no group_id set

    Admin list comes from `admin_tools.admins`.
    """

    def __init__(self, config: Dict[str, Any]):
        self.cfg = (config or {}).get("support") or {}
        at = (config or {}).get("admin_tools") or {}
        self.admins: List[str] = [str(a) for a in (at.get("admins") or [])]
        self.group_id: Optional[str] = str(self.cfg.get("group_id")) if self.cfg.get("group_id") else None
        from pathlib import Path
        base = Path(__file__).resolve().parent.parent
        store_rel = self.cfg.get("store_path") or "config/support_tickets.json"
        self.store_path = (base / store_rel)
        self.notify_admins_dm: bool = bool(self.cfg.get("notify_admins_dm", True))
        # in-memory map: admin message id -> ticket id (only for group replies)
        self._admin_msg_to_ticket: Dict[str, str] = {}
        self._load_store()

    # -- storage helpers --
    def _load_store(self):
        try:
            if self.store_path.exists():
                data = json.loads(self.store_path.read_text())
            else:
                data = {"tickets": {}}
        except Exception:
            data = {"tickets": {}}
        self.tickets: Dict[str, Ticket] = {}
        for tid, t in (data.get("tickets") or {}).items():
            self.tickets[tid] = Ticket(
                id=tid,
                user_id=str(t.get("user_id")),
                chat_id=str(t.get("chat_id")),
                status=str(t.get("status") or "open"),
                created_at=float(t.get("created_at") or time.time()),
                updated_at=float(t.get("updated_at") or time.time()),
                subject=str(t.get("subject") or ""),
            )

    def _save_store(self):
        try:
            data = {"tickets": {tid: t.__dict__ for tid, t in self.tickets.items()}}
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            self.store_path.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _find_open_by_user(self, user_id: str) -> Optional[Ticket]:
        for t in self.tickets.values():
            if t.user_id == user_id and t.status == "open":
                return t
        return None

    async def handle(self, message: Message, service: MessagingService) -> bool:
        text = (message.content or "").strip()
        if text.startswith("/support"):
            parts = text.split(maxsplit=2)
            if len(parts) == 1 or parts[1] == "list":
                return await self._handle_admin_list_or_usage(message, service, parts)
            if parts[1] == "close":
                return await self._handle_admin_close(message, service, parts)
            # Otherwise treat as user open: /support <subject>
            subject = parts[1] if len(parts) >= 2 else ""
            if len(parts) == 3:
                subject = parts[2]
            return await self._handle_user_open(message, service, subject)

        # Admin DM reply command
        if text.startswith("/reply "):
            parts = text.split(maxsplit=2)
            if len(parts) < 3:
                await service.send_message(chat_id=message.chat.id, text="Usage: /reply <ticket_id> <text>")
                return True
            uid = str(message.from_user.id)
            if uid not in self.admins:
                await service.send_message(chat_id=message.chat.id, text="Admins only.")
                return True
            tid = parts[1]
            resp = parts[2]
            t = self.tickets.get(tid)
            if not t or t.status != "open":
                await service.send_message(chat_id=message.chat.id, text="Ticket not found or closed.")
                return True
            await service.send_message(chat_id=t.chat_id, text=f"[Support] {resp}")
            await service.send_message(chat_id=message.chat.id, text="Sent.")
            return True

        # Admin group reply relay: if in group and replying to a tracked message
        if self.group_id and str(message.chat.id) == str(self.group_id):
            reply = message.reply_to_message
            if reply:
                key = str(getattr(reply, "message_id", ""))
                ticket_id = self._admin_msg_to_ticket.get(key)
                if ticket_id and message.content:
                    t = self.tickets.get(ticket_id)
                    if t and t.status == "open":
                        await service.send_message(chat_id=t.chat_id, text=f"[Support] {message.content}")
                        return True
        return False

    async def _handle_user_open(self, message: Message, service: MessagingService, subject: str) -> bool:
        uid = str(message.from_user.id)
        chat_id = str(message.chat.id)
        t = self._find_open_by_user(uid)
        if not t:
            tid = str(int(time.time() * 1000))[-10:]
            now = time.time()
            t = Ticket(id=tid, user_id=uid, chat_id=chat_id, status="open", created_at=now, updated_at=now, subject=subject or "")
            self.tickets[t.id] = t
            self._save_store()
        else:
            t.updated_at = time.time()
            if subject:
                t.subject = subject
            self._save_store()
        await service.send_message(chat_id=chat_id, text=f"Support ticket opened. ID: {t.id}. Our admins will reply here.")
        # Notify admins
        await self._notify_admins_new_ticket(service, t, message)
        # Emit event
        try:
            await event_bus.emit("support.ticket.opened", {"ticket_id": t.id, "user_id": uid, "chat_id": chat_id, "subject": subject})
        except Exception:
            pass
        return True

    async def _notify_admins_new_ticket(self, service: MessagingService, t: Ticket, source_message: Message) -> None:
        body = f"New support ticket {t.id} from user {t.user_id}:\n{t.subject or '(no subject)'}"
        if self.group_id:
            try:
                m = await service.send_message(chat_id=self.group_id, text=body)
                # Track mapping for group replies
                mid = str(getattr(m, "message_id", "")) if m else None
                if mid:
                    self._admin_msg_to_ticket[mid] = t.id
            except Exception:
                pass
        elif self.notify_admins_dm and self.admins:
            for admin_id in self.admins:
                try:
                    await service.send_message(chat_id=admin_id, text=body + f"\nReply with /reply {t.id} <text> to respond.")
                except Exception:
                    pass

    async def _handle_admin_list_or_usage(self, message: Message, service: MessagingService, parts: List[str]) -> bool:
        uid = str(message.from_user.id)
        is_admin = uid in self.admins
        if len(parts) == 1:
            # user usage
            await service.send_message(chat_id=message.chat.id, text="Usage: /support <your message>\nAdmins: /support list | /support close <ticket_id>")
            return True
        if parts[1] == "list":
            if not is_admin:
                await service.send_message(chat_id=message.chat.id, text="Admins only.")
                return True
            open_ts = [t for t in self.tickets.values() if t.status == "open"]
            if not open_ts:
                await service.send_message(chat_id=message.chat.id, text="No open tickets.")
                return True
            lines = [f"{t.id} • user {t.user_id} • {time.strftime('%Y-%m-%d %H:%M', time.localtime(t.updated_at))} • {t.subject}" for t in sorted(open_ts, key=lambda x: x.updated_at, reverse=True)]
            await service.send_message(chat_id=message.chat.id, text="Open tickets:\n" + "\n".join(lines))
            return True
        return False

    async def _handle_admin_close(self, message: Message, service: MessagingService, parts: List[str]) -> bool:
        uid = str(message.from_user.id)
        if uid not in self.admins:
            await service.send_message(chat_id=message.chat.id, text="Admins only.")
            return True
        if len(parts) < 3:
            await service.send_message(chat_id=message.chat.id, text="Usage: /support close <ticket_id>")
            return True
        tid = parts[2]
        t = self.tickets.get(tid)
        if not t or t.status != "open":
            await service.send_message(chat_id=message.chat.id, text="Ticket not found or already closed.")
            return True
        t.status = "closed"
        t.updated_at = time.time()
        self._save_store()
        await service.send_message(chat_id=message.chat.id, text=f"Ticket {tid} closed.")
        try:
            await service.send_message(chat_id=t.chat_id, text="Your support ticket has been closed. Thank you!")
        except Exception:
            pass
        try:
            await event_bus.emit("support.ticket.closed", {"ticket_id": tid, "user_id": t.user_id})
        except Exception:
            pass
        return True
