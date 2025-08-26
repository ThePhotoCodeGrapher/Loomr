import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional

from core.message import Message
from core.service import MessageHandler, MessagingService
from core import analytics


class AnalyticsPlugin(MessageHandler):
    def __init__(self, full_config: Dict[str, Any]):
        self.cfg = full_config or {}
        admins = ((self.cfg.get("admin_tools") or {}).get("admins") or [])
        self.admin_ids = set(str(x) for x in admins)
        # Path to shared admin/user store used by AdminTools
        store_path = (self.cfg.get("admin_tools") or {}).get("user_store_path") or "config/users.json"
        self.user_store_path: Path = Path(Path(__file__).resolve().parent.parent / store_path)
        # ensure DB exists
        # fire-and-forget; main also initializes
        asyncio.create_task(analytics.init_db())

    def _refresh_admins(self) -> None:
        """Merge admins from config and AdminTools user store (if present)."""
        try:
            # Start with config admins
            cfg_admins = set(str(x) for x in ((self.cfg.get("admin_tools") or {}).get("admins") or []))
            merged = set(cfg_admins)
            # Merge store admins
            if self.user_store_path and self.user_store_path.exists():
                try:
                    raw = json.loads(self.user_store_path.read_text())
                    store_admins = set(str(x) for x in (raw.get("admins") or []))
                    merged |= store_admins
                except Exception:
                    pass
            self.admin_ids = merged
        except Exception:
            # Keep previous admin_ids on any error
            pass

    def _is_admin(self, uid: Optional[str]) -> bool:
        return uid is not None and str(uid) in self.admin_ids

    async def handle(self, message: Message, service: MessagingService) -> bool:
        text = (message.content or "").strip()
        if not text.startswith("/"):
            return False
        parts = text.split()
        cmd = parts[0].lower()
        uid = str(getattr(message.from_user, "id", ""))
        # Refresh dynamic admin list so newly registered admins are recognized without restart
        self._refresh_admins()
        if cmd not in ("/stats", "/stats_group", "/stats_top"):
            return False
        if not self._is_admin(uid):
            return False

        # /stats [days]
        if cmd == "/stats":
            try:
                days = int(parts[1]) if len(parts) >= 2 else 1
            except Exception:
                days = 1
            s = await analytics.query_daily_summary(days=days)
            lines = [f"Stats (last {days}d):",
                     f"- in: {s['messages_in']} msgs, {s['bytes_in']} bytes",
                     f"- out: {s['messages_out']} msgs, {s['bytes_out']} bytes"]
            await service.send_message(chat_id=str(message.chat.id), text="\n".join(lines))
            return True

        # /stats_group <chat_id> [days]
        if cmd == "/stats_group":
            if len(parts) < 2:
                await service.send_message(chat_id=str(message.chat.id), text="Usage: /stats_group <chat_id> [days]")
                return True
            chat_id = parts[1]
            try:
                days = int(parts[2]) if len(parts) >= 3 else 7
            except Exception:
                days = 7
            s = await analytics.query_daily_summary(days=days, chat_id=chat_id)
            lines = [f"Group {chat_id} (last {days}d):",
                     f"- in: {s['messages_in']} msgs, {s['bytes_in']} bytes",
                     f"- out: {s['messages_out']} msgs, {s['bytes_out']} bytes"]
            await service.send_message(chat_id=str(message.chat.id), text="\n".join(lines))
            return True

        # /stats_top [days]
        if cmd == "/stats_top":
            try:
                days = int(parts[1]) if len(parts) >= 2 else 7
            except Exception:
                days = 7
            rows = await analytics.top_groups(days=days)
            if not rows:
                await service.send_message(chat_id=str(message.chat.id), text="No data")
                return True
            lines = [f"Top groups (last {days}d):"]
            for i, (cid, c) in enumerate(rows, 1):
                lines.append(f"{i}. {cid}: {c} msgs")
            await service.send_message(chat_id=str(message.chat.id), text="\n".join(lines))
            return True

        return False
