from __future__ import annotations

from typing import Any, Dict, Optional

from core.message import Message
from core.service import MessageHandler, MessagingService


class DashboardPlugin(MessageHandler):
    """Provides /dashboard command to open a Telegram Web App."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or {}
        # Expect web_app.url at root config; fall back to localhost webhook path
        self.web_app_url: str = cfg.get("url") or "http://localhost:8081/webapp"

    async def handle(self, message: Message, service: MessagingService) -> bool:
        text = (message.content or "").strip()
        if not text.startswith("/"):
            return False
        cmd = text.split()[0].lower()
        if cmd not in ("/dashboard",):
            return False
        await service.send_message(
            chat_id=str(message.chat.id),
            text="Open the dashboard WebApp",
            inline_buttons=[[{"text": "Open Dashboard", "web_app_url": self.web_app_url}]],
            reply_to_message_id=str(message.message_id),
        )
        return True
