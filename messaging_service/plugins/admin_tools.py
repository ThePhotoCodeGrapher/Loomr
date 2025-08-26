import json
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import time

from core.message import Message
from core.service import MessageHandler, MessagingService


@dataclass
class UserRecord:
    id: str
    username: Optional[str] = None
    chat_ids: Set[str] = field(default_factory=set)
    roles: Set[str] = field(default_factory=set)
    role_expiry: Dict[str, float] = field(default_factory=dict)  # role -> unix_ts
    # optional location fields
    location_text: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "chat_ids": sorted(self.chat_ids),
            "roles": sorted(self.roles),
            "role_expiry": {k: float(v) for k, v in (self.role_expiry or {}).items()},
            "location_text": self.location_text,
            "lat": self.lat,
            "lon": self.lon,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "UserRecord":
        rec = UserRecord(id=str(d.get("id")))
        rec.username = d.get("username")
        rec.chat_ids = set(str(x) for x in (d.get("chat_ids") or []))
        rec.roles = set(str(x) for x in (d.get("roles") or []))
        try:
            rexp = d.get("role_expiry") or {}
            rec.role_expiry = {str(k): float(v) for k, v in rexp.items()}
        except Exception:
            rec.role_expiry = {}
        rec.location_text = d.get("location_text")
        try:
            rec.lat = float(d.get("lat")) if d.get("lat") is not None else None
            rec.lon = float(d.get("lon")) if d.get("lon") is not None else None
        except Exception:
            rec.lat, rec.lon = None, None
        return rec


class AdminTools(MessageHandler):
    def __init__(self, config: Dict[str, Any]):
        self.config = config or {}
        # Secret used for self-registration; recommended to come from environment via config
        self.register_secret: Optional[str] = self.config.get("register_secret")
        self.admin_ids: Set[str] = set(str(x) for x in (self.config.get("admins") or []))
        store_path = self.config.get("user_store_path") or "config/users.json"
        self.user_store_path = Path(Path(__file__).resolve().parent.parent / store_path)
        self.user_store_path.parent.mkdir(parents=True, exist_ok=True)
        self.users: Dict[str, UserRecord] = {}
        self._load_store()
        # Wallet store for per-product chain recipients
        self.wallet_store_path = Path(Path(__file__).resolve().parent.parent / "config/wallets.json")
        self.wallet_store_path.parent.mkdir(parents=True, exist_ok=True)

    def _load_store(self) -> None:
        try:
            if self.user_store_path.exists():
                with open(self.user_store_path, "r") as f:
                    raw = json.load(f)
                for uid, d in (raw.get("users") or {}).items():
                    self.users[str(uid)] = UserRecord.from_dict({"id": uid, **d})
                store_admins = set(str(x) for x in (raw.get("admins") or []))
                if store_admins:
                    # merge with config admins
                    self.admin_ids |= store_admins
        except Exception:
            # Start fresh on error
            self.users = {}

    def _save_store(self) -> None:
        try:
            data = {
                "users": {uid: rec.to_dict() for uid, rec in self.users.items()},
                "admins": sorted(self.admin_ids),
            }
            with open(self.user_store_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def _is_admin(self, user_id: str) -> bool:
        return str(user_id) in self.admin_ids

    def _ensure_user(self, message: Message) -> UserRecord:
        uid = str(message.from_user.id)
        rec = self.users.get(uid) or UserRecord(id=uid)
        rec.username = getattr(message.from_user, "username", rec.username)
        rec.chat_ids.add(str(message.chat.id))
        self.users[uid] = rec
        self._save_store()
        return rec

    async def _send_to_chat_ids(self, chat_ids: List[str], text: str, service: MessagingService) -> int:
        sent = 0
        for cid in chat_ids:
            try:
                await service.send_message(chat_id=cid, text=text)
                sent += 1
                await asyncio.sleep(0.03)
            except Exception:
                pass
        return sent

    async def handle(self, message: Message, service: MessagingService) -> bool:
        text = (message.content or "").strip()
        if not text:
            return False

        # Always track user presence for broadcast lists
        self._ensure_user(message)

        if not text.startswith("/"):
            return False

        parts = text.split(" ")
        cmd = parts[0].lower()

        # Help visible to all (but admin-only commands are labeled)
        if cmd in ("/admin", "/admin_help", "/help_admin"):
            help_text = (
                "Admin commands:\n"
                "- /broadcast <text> (admin)\n"
                "- /notify role <role> <text> (admin)\n"
                "- /notify user <id|@username> <text> (admin)\n"
                "- /setrole <user_id> <role1,role2> (admin)\n"
                "- /roles <user_id> (admin)\n"
                "- /menu_refresh (admin)\n"
                "- /admin_register <secret> (public, if secret matches)\n"
                "- /admin_unregister <secret> (admin/public if secret matches)\n"
                "- /admin_list (admin)\n"
                "- /iam (public)\n"
                "- /setloc <city,country> (public)\n"
                "- /setgeo <lat> <lon> (public)\n"
                "- /myloc (public)\n"
            )
            await service.send_message(chat_id=str(message.chat.id), text=help_text)
            return True

        # Public utilities
        if cmd == "/iam":
            uid = str(message.from_user.id)
            uname = getattr(message.from_user, "username", None)
            await service.send_message(chat_id=str(message.chat.id), text=f"Your ID: {uid}{' (@'+uname+')' if uname else ''}")
            return True

        if cmd == "/tier":
            rec = self._ensure_user(message)
            now = time.time()
            lines = ["Your plan(s):"]
            if not rec.roles:
                lines.append("(none)")
            for r in sorted(rec.roles):
                exp = rec.role_expiry.get(r)
                if exp and exp < now:
                    # expired, show as expired
                    lines.append(f"- {r} (expired)")
                elif exp:
                    # show days remaining
                    days = int((exp - now) // 86400)
                    lines.append(f"- {r} (expires in ~{days}d)")
                else:
                    lines.append(f"- {r} (no expiry)")
            await service.send_message(chat_id=str(message.chat.id), text="\n".join(lines))
            return True

        if cmd == "/setloc" and len(parts) >= 2:
            loc = (message.content or "").split(" ", 1)[1].strip()
            rec = self._ensure_user(message)
            rec.location_text = loc[:160]
            self.users[rec.id] = rec
            self._save_store()
            await service.send_message(chat_id=str(message.chat.id), text=f"Location saved: {rec.location_text}")
            return True

        if cmd == "/setgeo" and len(parts) >= 3:
            try:
                lat = float(parts[1])
                lon = float(parts[2])
            except Exception:
                await service.send_message(chat_id=str(message.chat.id), text="Usage: /setgeo <lat> <lon>")
                return True
            rec = self._ensure_user(message)
            rec.lat, rec.lon = lat, lon
            self.users[rec.id] = rec
            self._save_store()
            await service.send_message(chat_id=str(message.chat.id), text=f"Geo saved: {lat:.5f},{lon:.5f}")
            return True

        if cmd == "/myloc":
            rec = self._ensure_user(message)
            txt = f"Location: {rec.location_text or '(none)'}\nGeo: {rec.lat if rec.lat is not None else '-'}, {rec.lon if rec.lon is not None else '-'}"
            await service.send_message(chat_id=str(message.chat.id), text=txt)
            return True

        # Allow self-registration if secret is configured
        if cmd == "/admin_register" and len(parts) >= 2:
            provided = parts[1]
            if not self.register_secret:
                await service.send_message(chat_id=str(message.chat.id), text="Registration is disabled.")
                return True
            if provided != self.register_secret:
                await service.send_message(chat_id=str(message.chat.id), text="Invalid secret.")
                return True
            uid = str(message.from_user.id)
            self.admin_ids.add(uid)
            self._save_store()
            await service.send_message(chat_id=str(message.chat.id), text=f"User {uid} promoted to admin.")
            return True

        if cmd == "/admin_unregister" and len(parts) >= 2:
            provided = parts[1]
            if not self.register_secret:
                await service.send_message(chat_id=str(message.chat.id), text="Unregister is disabled.")
                return True
            if provided != self.register_secret:
                await service.send_message(chat_id=str(message.chat.id), text="Invalid secret.")
                return True
            uid = str(message.from_user.id)
            if uid in self.admin_ids:
                self.admin_ids.remove(uid)
                self._save_store()
            await service.send_message(chat_id=str(message.chat.id), text=f"User {uid} removed from admins.")
            return True

        # Admin-gated below
        if not self._is_admin(str(message.from_user.id)):
            return False

        # /setwallet <product_id> <chain> <address>
        if cmd == "/setwallet" and len(parts) >= 4:
            pid = parts[1]
            chain = parts[2].lower()
            addr = parts[3]
            try:
                wallets = {}
                if self.wallet_store_path.exists():
                    wallets = json.loads(self.wallet_store_path.read_text())
                store = wallets.get("wallets") or {}
                prod = store.get(pid) or {}
                prod[chain] = addr
                store[pid] = prod
                wallets["wallets"] = store
                self.wallet_store_path.write_text(json.dumps(wallets, indent=2))
                await service.send_message(chat_id=str(message.chat.id), text=f"Wallet set for {pid} on {chain}: {addr}")
            except Exception as e:
                await service.send_message(chat_id=str(message.chat.id), text=f"Failed to save wallet: {e}")
            return True

        # /wallet <product_id>
        if cmd == "/wallet" and len(parts) >= 2:
            pid = parts[1]
            try:
                wallets = {}
                if self.wallet_store_path.exists():
                    wallets = json.loads(self.wallet_store_path.read_text())
                store = wallets.get("wallets") or {}
                prod = store.get(pid) or {}
                if not prod:
                    await service.send_message(chat_id=str(message.chat.id), text="No wallets configured for this product.")
                else:
                    lines = [f"Wallets for {pid}:"]
                    for ch, ad in sorted(prod.items()):
                        lines.append(f"- {ch}: {ad}")
                    await service.send_message(chat_id=str(message.chat.id), text="\n".join(lines))
            except Exception as e:
                await service.send_message(chat_id=str(message.chat.id), text=f"Error reading wallets: {e}")
            return True

        # /broadcast <text>
        if cmd == "/broadcast" and len(parts) >= 2:
            payload = text[len("/broadcast"):].strip()
            if not payload:
                await service.send_message(chat_id=str(message.chat.id), text="Usage: /broadcast <text>")
                return True
            all_chats: Set[str] = set()
            for rec in self.users.values():
                all_chats.update(rec.chat_ids)
            sent = await self._send_to_chat_ids(sorted(all_chats), payload, service)
            await service.send_message(chat_id=str(message.chat.id), text=f"Broadcast sent to {sent} chats")
            return True

        # /notify role <role> <text>
        if cmd == "/notify" and len(parts) >= 3 and parts[1].lower() == "role":
            role = parts[2]
            payload = text.split(" ", 3)
            if len(payload) < 4:
                await service.send_message(chat_id=str(message.chat.id), text="Usage: /notify role <role> <text>")
                return True
            msg = payload[3]
            chat_ids: Set[str] = set()
            for rec in self.users.values():
                if role in rec.roles:
                    chat_ids.update(rec.chat_ids)
            sent = await self._send_to_chat_ids(sorted(chat_ids), msg, service)
            await service.send_message(chat_id=str(message.chat.id), text=f"Notified role '{role}' in {sent} chats")
            return True

        # /notify user <id|@username> <text>
        if cmd == "/notify" and len(parts) >= 3 and parts[1].lower() == "user":
            who = parts[2]
            payload = text.split(" ", 3)
            if len(payload) < 4:
                await service.send_message(chat_id=str(message.chat.id), text="Usage: /notify user <id|@username> <text>")
                return True
            msg = payload[3]
            usernames = {rec.username: rec for rec in self.users.values() if rec.username}
            target: Optional[UserRecord] = None
            if who.startswith("@"):
                target = usernames.get(who.lstrip("@"))
            else:
                target = self.users.get(str(who))
            if not target:
                await service.send_message(chat_id=str(message.chat.id), text="User not found")
                return True
            sent = await self._send_to_chat_ids(sorted(target.chat_ids), msg, service)
            await service.send_message(chat_id=str(message.chat.id), text=f"Notified user in {sent} chats")
            return True

        # /setrole <user_id> <role1,role2>
        if cmd == "/setrole" and len(parts) >= 3:
            uid = parts[1]
            roles_csv = parts[2]
            roles = [r.strip() for r in roles_csv.split(",") if r.strip()]
            rec = self.users.get(str(uid))
            if not rec:
                await service.send_message(chat_id=str(message.chat.id), text="Unknown user_id")
                return True
            rec.roles = set(roles)
            self.users[str(uid)] = rec
            self._save_store()
            await service.send_message(chat_id=str(message.chat.id), text=f"Roles for {uid}: {', '.join(sorted(rec.roles)) or '(none)'}")
            return True

        # /roles <user_id>
        if cmd == "/roles" and len(parts) >= 2:
            uid = parts[1]
            rec = self.users.get(str(uid))
            if not rec:
                await service.send_message(chat_id=str(message.chat.id), text="Unknown user_id")
                return True
            await service.send_message(chat_id=str(message.chat.id), text=f"Roles for {uid}: {', '.join(sorted(rec.roles)) or '(none)'}")
            return True

        # /menu_refresh
        if cmd == "/menu_refresh":
            # try to refresh commands from adapter config
            try:
                if hasattr(service, "_apply_bot_menu"):
                    await getattr(service, "_apply_bot_menu")()
                    await service.send_message(chat_id=str(message.chat.id), text="Menu refreshed from config")
                    return True
            except Exception:
                pass
            await service.send_message(chat_id=str(message.chat.id), text="Menu refresh not available")
            return True

        if cmd == "/admin_list":
            admins = ", ".join(sorted(self.admin_ids)) or "(none)"
            await service.send_message(chat_id=str(message.chat.id), text=f"Admins: {admins}")
            return True

        return False
