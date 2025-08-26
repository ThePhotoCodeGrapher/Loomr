from typing import Any, Dict, List, Optional
import json
from pathlib import Path

from core.message import Message
from core.service import MessageHandler, MessagingService


class MenuPlugin(MessageHandler):
    """
    Declarative menu/command plugin.

    Config shape passed from main:
    menus: List[{
      command: "/menu",
      text: "Menu content..."
    }]
    """

    def __init__(self, menus: List[Dict[str, Any]]):
        self.menus = menus or []
        # build index for O(1) lookup
        self._index: Dict[str, Dict[str, Any]] = {}
        for it in self.menus:
            cmd = str(it.get("command") or "").strip().lower()
            if cmd.startswith("/"):
                self._index[cmd] = it
        # path to shared users/admins store (align with AdminTools default)
        self._user_store_path: Path = (Path(__file__).parent.parent / "config/users.json").resolve()

    async def handle(self, message: Message, service: MessagingService) -> bool:
        text = (message.content or "").strip()
        if not text.startswith("/"):
            return False
        cmd = text.split(" ", 1)[0].lower()
        spec = self._index.get(cmd)
        if not spec:
            return False
        # Admin/role gating if specified
        if not self._allowed(spec, str(message.from_user.id)):
            return False
        out = str(spec.get("text") or "")
        if not out:
            return False
        await service.send_message(chat_id=message.chat.id, text=out, reply_to_message_id=str(message.message_id))
        return True

    def _allowed(self, spec: Dict[str, Any], user_id: str) -> bool:
        admins_only = bool(spec.get("admins_only", False))
        required_roles = spec.get("roles") or []
        if not admins_only and not required_roles:
            return True
        # Read user store if exists
        admins: List[str] = []
        roles: List[str] = []
        try:
            if self._user_store_path.exists():
                with open(self._user_store_path, "r") as f:
                    data = json.load(f) or {}
                admins = [str(x) for x in (data.get("admins") or [])]
                user_rec = (data.get("users") or {}).get(str(user_id)) or {}
                roles = [str(x) for x in (user_rec.get("roles") or [])]
        except Exception:
            pass
        if admins_only and str(user_id) not in admins:
            return False
        if required_roles:
            # allow if any of required_roles present
            if not any(r in roles for r in required_roles):
                return False
        return True
