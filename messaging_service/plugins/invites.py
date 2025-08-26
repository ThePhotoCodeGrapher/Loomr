import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, List, Set

from core.message import Message
from core.service import MessageHandler, MessagingService


@dataclass
class Invite:
    code: str
    role: Optional[str] = None
    max_uses: int = 1
    uses: int = 0
    expires_at: Optional[int] = None  # epoch seconds
    created_by: Optional[str] = None
    created_at: Optional[int] = None
    bound_user_id: Optional[str] = None  # only this user can redeem
    notes: Optional[str] = None
    flow_name: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    redeemed_by: Optional[List[Dict[str, Any]]] = None  # [{user_id, at}]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "role": self.role,
            "max_uses": self.max_uses,
            "uses": self.uses,
            "expires_at": self.expires_at,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "bound_user_id": self.bound_user_id,
            "notes": self.notes,
            "flow_name": self.flow_name,
            "metadata": self.metadata or {},
            "redeemed_by": self.redeemed_by or [],
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Invite":
        return Invite(
            code=str(d.get("code")),
            role=d.get("role"),
            max_uses=int(d.get("max_uses") or 1),
            uses=int(d.get("uses") or 0),
            expires_at=int(d.get("expires_at")) if d.get("expires_at") else None,
            created_by=(d.get("created_by") if d.get("created_by") is not None else None),
            created_at=int(d.get("created_at")) if d.get("created_at") else None,
            bound_user_id=(str(d.get("bound_user_id")) if d.get("bound_user_id") is not None else None),
            notes=d.get("notes"),
            flow_name=d.get("flow_name"),
            metadata=d.get("metadata") or {},
            redeemed_by=d.get("redeemed_by") or [],
        )


class InvitesPlugin(MessageHandler):
    def __init__(self, config: Dict[str, Any], admin_tools_config: Dict[str, Any]):
        self.config = config or {}
        self.admin_cfg = admin_tools_config or {}
        base_dir = Path(__file__).parent.parent
        # store for invites
        path = self.config.get("store_path") or "config/invites.json"
        self.store_path = (base_dir / path).resolve()
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        # access lock config
        access_cfg = self.config.get("access") or {}
        # fallback to top-level access block if provided in global config
        self.locked = bool(access_cfg.get("locked", False))
        self.allowed_when_locked: Set[str] = set(access_cfg.get("allowed_when_locked") or ["/start", "/help", "/join"])
        # defaults for generated invites
        self.default_role = self.config.get("default_role")
        self.code_length = int(self.config.get("code_length") or 8)
        self.expire_days_default = int(self.config.get("expire_days_default") or 7)
        self.max_uses_default = int(self.config.get("max_uses_default") or 1)
        self.on_redeem_cfg = self.config.get("on_redeem") or {}
        # behavior toggles
        self.bind_to_user_by_default: bool = bool(self.config.get("bind_to_user_by_default", True))
        self.daily_create_limit: int = int(self.config.get("daily_create_limit") or 10)
        # optional app link template, e.g., myapp://join?code={code}&user={user_id}
        self.app_link_template: Optional[str] = self.config.get("app_link_template")
        # QR config
        qr_cfg = self.config.get("qr") or {}
        self.qr_enabled: bool = bool(qr_cfg.get("enabled", True))
        self.qr_box_size: int = int(qr_cfg.get("box_size", 6))
        self.qr_border: int = int(qr_cfg.get("border", 2))
        # load
        self.invites: Dict[str, Invite] = {}
        self._load_store()

    def _load_store(self) -> None:
        try:
            if self.store_path.exists():
                with open(self.store_path, "r") as f:
                    raw = json.load(f) or {}
                for code, d in (raw.get("invites") or {}).items():
                    self.invites[code] = Invite.from_dict({"code": code, **d})
        except Exception:
            self.invites = {}

    def _save_store(self) -> None:
        try:
            data = {"invites": {code: inv.to_dict() for code, inv in self.invites.items()}}
            with open(self.store_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def _is_admin(self, user_id: str) -> bool:
        # read live admins from AdminTools store (merge with config)
        admins: Set[str] = set(str(x) for x in (self.admin_cfg.get("admins") or []))
        user_store_path = self.admin_cfg.get("user_store_path")
        if user_store_path:
            try:
                abs_path = (Path(__file__).parent.parent / user_store_path).resolve()
                if abs_path.exists():
                    with open(abs_path, "r") as f:
                        raw = json.load(f) or {}
                    admins |= set(str(x) for x in (raw.get("admins") or []))
            except Exception:
                pass
        return str(user_id) in admins

    def _assign_role(self, user_id: str, role: str) -> None:
        # update AdminTools user store
        user_store_path = self.admin_cfg.get("user_store_path") or "config/users.json"
        abs_path = (Path(__file__).parent.parent / user_store_path).resolve()
        data = {"users": {}, "admins": self.admin_cfg.get("admins") or []}
        try:
            if abs_path.exists():
                with open(abs_path, "r") as f:
                    data = json.load(f) or data
        except Exception:
            pass
        users = data.get("users") or {}
        u = users.get(str(user_id)) or {"id": str(user_id), "roles": []}
        roles = set(str(r) for r in (u.get("roles") or []))
        roles.add(role)
        u["roles"] = sorted(roles)
        users[str(user_id)] = u
        data["users"] = users
        try:
            with open(abs_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def _user_has_access(self, message: Message) -> bool:
        # admins bypass
        if self._is_admin(str(message.from_user.id)):
            return True
        # if not locked, allow
        if not self.locked:
            return True
        # if locked: allow only if user has any role (joined) or allowed commands
        try:
            user_store_path = self.admin_cfg.get("user_store_path") or "config/users.json"
            abs_path = (Path(__file__).parent.parent / user_store_path).resolve()
            if abs_path.exists():
                with open(abs_path, "r") as f:
                    data = json.load(f) or {}
                users = data.get("users") or {}
                rec = users.get(str(message.from_user.id))
                if rec and (rec.get("roles") or []):
                    return True
        except Exception:
            pass
        return False

    async def _redeem_code(self, code: str, message: Message, service: MessagingService) -> bool:
        inv = self.invites.get(code)
        if not inv:
            await service.send_message(chat_id=str(message.chat.id), text="Invalid invite code.")
            return True
        now = int(time.time())
        if inv.expires_at and now > inv.expires_at:
            await service.send_message(chat_id=str(message.chat.id), text="Invite code has expired.")
            return True
        if inv.uses >= inv.max_uses:
            await service.send_message(chat_id=str(message.chat.id), text="Invite code has no remaining uses.")
            return True
        if inv.bound_user_id and str(message.from_user.id) != str(inv.bound_user_id):
            await service.send_message(chat_id=str(message.chat.id), text="This invite code is not for your account.")
            return True
        role = inv.role or self.default_role
        if role:
            self._assign_role(str(message.from_user.id), role)
        inv.uses += 1
        try:
            rec = {"user_id": str(message.from_user.id), "at": now, "by": inv.created_by}
            inv.redeemed_by = (inv.redeemed_by or []) + [rec]
        except Exception:
            pass
        self.invites[code] = inv
        self._save_store()
        welcome_tpl = (self.on_redeem_cfg.get("welcome_template") or "Welcome!") if isinstance(self.on_redeem_cfg, dict) else None
        if welcome_tpl:
            try:
                uname = getattr(message.from_user, "username", "")
                first = getattr(message.from_user, "first_name", "")
                rendered = welcome_tpl.format(code=code, role=role or "-", username=uname, first_name=first)
            except Exception:
                rendered = "Welcome!"
            await service.send_message(chat_id=str(message.chat.id), text=rendered)
        else:
            await service.send_message(chat_id=str(message.chat.id), text="Invite accepted. You're in!")
        return True

    async def handle(self, message: Message, service: MessagingService) -> bool:
        text = (message.content or "").strip()
        parts = text.split()
        cmd = parts[0].lower() if parts else ""

        # Access gate: block early for non-allowed commands
        if text.startswith("/"):
            parts = text.split(" ")
            cmd = parts[0].lower()
            if cmd not in self.allowed_when_locked and not self._user_has_access(message):
                await service.send_message(chat_id=str(message.chat.id), text="This bot is invite-only. Use /join <code> to enter.")
                return True
        else:
            # Non-command messages are blocked if locked and not admitted
            if not self._user_has_access(message):
                await service.send_message(chat_id=str(message.chat.id), text="This bot is invite-only. Use /join <code> to enter.")
                return True

        if not text.startswith("/"):
            return False

        # Public join
        if cmd == "/join" and len(parts) >= 2:
            return await self._redeem_code(parts[1], message, service)

        # support deep-link payloads: /start join-<code>
        if cmd == "/start" and len(parts) >= 2 and parts[1].startswith("join-"):
            code = parts[1][len("join-"):]
            return await self._redeem_code(code, message, service)

        # Admin commands below
        if not self._is_admin(str(message.from_user.id)):
            return False

        # /invite_create <code> [role=...] [max=n] [expires=YYYY-MM-DD] [flow=name] [notes="..."] [user=<id>|@username]
        if cmd == "/invite_create" and len(parts) >= 2:
            # enforce per-creator daily limit
            if not self._can_create_more_today(str(message.from_user.id)):
                await service.send_message(chat_id=str(message.chat.id), text="Daily invite limit reached.")
                return True
            code = parts[1]
            # parse args
            args: Dict[str, str] = {}
            for token in parts[2:]:
                if "=" in token:
                    k, v = token.split("=", 1)
                    args[k.strip()] = v.strip().strip('"')
            role = args.get("role") or self.default_role
            max_uses = int(args.get("max") or self.max_uses_default)
            expires_at = None
            if args.get("expires"):
                try:
                    # naive parse YYYY-MM-DD to epoch
                    import datetime as dt
                    d = dt.datetime.strptime(args["expires"], "%Y-%m-%d")
                    expires_at = int(d.replace(tzinfo=dt.timezone.utc).timestamp())
                except Exception:
                    pass
            flow_name = args.get("flow") or (self.on_redeem_cfg.get("flow") if isinstance(self.on_redeem_cfg, dict) else None)
            notes = args.get("notes")
            # optional binding if user provided
            bound_user_id = None
            target = args.get("user") or args.get("username")
            if target:
                if target.startswith("@"):
                    bound_user_id = self._resolve_username_to_id(target.lstrip("@"))
                else:
                    bound_user_id = str(target)
                # when explicitly bound, make it single-use
                max_uses = 1
            inv = Invite(code=code, role=role, max_uses=max_uses, uses=0, expires_at=expires_at, created_by=str(message.from_user.id), created_at=int(time.time()), bound_user_id=bound_user_id, notes=notes, flow_name=flow_name)
            self.invites[code] = inv
            self._save_store()
            binding_info = f", bound_to={bound_user_id}" if bound_user_id else ""
            text_out = f"Invite created: {code} (role={role or '-'}, max={max_uses}{', expires' if expires_at else ''}{binding_info})"
            inline_buttons = None
            if bound_user_id:
                deep_link = self._deep_link(code)
                app_link = self._app_link(code, bound_user_id)
                row = [["Open in Telegram", deep_link]]
                if app_link:
                    row.append(["Open in App", app_link])
                inline_buttons = [row]
                # Try sending QR image
                if self.qr_enabled and deep_link:
                    qr = self._make_qr(deep_link)
                    caption = f"Invite for {bound_user_id}: {code}\nScan QR to open."
                    await service.send_message(chat_id=str(message.chat.id), text=text_out, inline_buttons=inline_buttons, photo_bytes=qr, caption=caption)
                    return True
            await service.send_message(chat_id=str(message.chat.id), text=text_out, inline_buttons=inline_buttons)
            return True

        if cmd == "/invite_list":
            active_only = len(parts) >= 2 and parts[1].lower() == "active"
            now = int(time.time())
            lines: List[str] = []
            for code, inv in sorted(self.invites.items()):
                expired = inv.expires_at and now > inv.expires_at
                if active_only and (expired or inv.uses >= inv.max_uses):
                    continue
                lines.append(f"{code} role={inv.role or '-'} uses={inv.uses}/{inv.max_uses} {'exp:'+str(inv.expires_at) if inv.expires_at else ''}")
            if not lines:
                await service.send_message(chat_id=str(message.chat.id), text="No invites.")
            else:
                await service.send_message(chat_id=str(message.chat.id), text="Invites:\n" + "\n".join(lines))
            return True

        if cmd == "/invite_revoke" and len(parts) >= 2:
            code = parts[1]
            if code in self.invites:
                self.invites.pop(code)
                self._save_store()
                await service.send_message(chat_id=str(message.chat.id), text=f"Invite revoked: {code}")
            else:
                await service.send_message(chat_id=str(message.chat.id), text="Invite not found")
            return True

        # /invite_for <user_id|@username> [role=...] [expires=YYYY-MM-DD]
        if cmd == "/invite_for" and len(parts) >= 2:
            # enforce per-creator daily limit
            if not self._can_create_more_today(str(message.from_user.id)):
                await service.send_message(chat_id=str(message.chat.id), text="Daily invite limit reached.")
                return True
            target = parts[1]
            # resolve @username to user_id via AdminTools store if available
            bound_user_id = None
            if target.startswith("@"):
                bound_user_id = self._resolve_username_to_id(target.lstrip("@"))
            else:
                bound_user_id = str(target)
            if not bound_user_id:
                await service.send_message(chat_id=str(message.chat.id), text="Unknown user for binding.")
                return True
            # args
            args: Dict[str, str] = {}
            for token in parts[2:]:
                if "=" in token:
                    k, v = token.split("=", 1)
                    args[k.strip()] = v.strip().strip('"')
            role = args.get("role") or self.default_role
            expires_at = None
            if args.get("expires"):
                try:
                    import datetime as dt
                    d = dt.datetime.strptime(args["expires"], "%Y-%m-%d")
                    expires_at = int(d.replace(tzinfo=dt.timezone.utc).timestamp())
                except Exception:
                    pass
            # generate code
            code = self._generate_code()
            inv = Invite(
                code=code,
                role=role,
                max_uses=1,
                uses=0,
                expires_at=expires_at,
                created_by=str(message.from_user.id),
                created_at=int(time.time()),
                bound_user_id=str(bound_user_id),
            )
            self.invites[code] = inv
            self._save_store()
            deep_link = self._deep_link(code)
            app_link = self._app_link(code, bound_user_id)
            text_out = f"Invite for {bound_user_id}: {code}\nTelegram link: {deep_link}"
            buttons = [["Open in Telegram", deep_link]]
            if app_link:
                buttons.append(["Open in App", app_link])
            if self.qr_enabled and deep_link:
                qr = self._make_qr(deep_link)
                caption = f"Invite for {bound_user_id}: {code}\nScan QR to open."
                await service.send_message(chat_id=str(message.chat.id), text=text_out, inline_buttons=[buttons], photo_bytes=qr, caption=caption)
            else:
                await service.send_message(chat_id=str(message.chat.id), text=text_out, inline_buttons=[buttons])
            return True

        return False

    # -------- Helpers --------
    def _generate_code(self) -> str:
        import secrets
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return "".join(secrets.choice(alphabet) for _ in range(self.code_length))

    def _deep_link(self, code: str) -> str:
        # best-effort: read TELEGRAM_BOT_ID env-like from global config not available here; fallback to /join instruction
        try:
            import os
            bot_id = os.getenv("TELEGRAM_BOT_ID")
            if bot_id and bot_id.startswith("@"):
                bot_id = bot_id[1:]
            if bot_id:
                # use start payload prefixed to avoid collisions
                payload = f"join-{code}"
                return f"https://t.me/{bot_id}?start={payload}"
        except Exception:
            pass
        return f"Send /join {code} to the bot"

    def _app_link(self, code: str, user_id: Optional[str]) -> Optional[str]:
        tpl = self.app_link_template
        if not tpl:
            return None
        try:
            return tpl.format(code=code, user_id=user_id or "")
        except Exception:
            return None

    def _make_qr(self, url: str) -> bytes:
        try:
            import io
            import qrcode
            qr = qrcode.QRCode(version=None, box_size=self.qr_box_size, border=self.qr_border)
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception:
            return b""

    def _resolve_username_to_id(self, username: str) -> Optional[str]:
        try:
            user_store_path = self.admin_cfg.get("user_store_path") or "config/users.json"
            abs_path = (Path(__file__).parent.parent / user_store_path).resolve()
            if abs_path.exists():
                with open(abs_path, "r") as f:
                    data = json.load(f) or {}
                users = data.get("users") or {}
                for uid, rec in users.items():
                    if str(rec.get("username")) == username:
                        return str(uid)
        except Exception:
            pass
        return None

    def _can_create_more_today(self, creator_id: str) -> bool:
        if self.daily_create_limit <= 0:
            return True
        try:
            import datetime as dt
            now = dt.datetime.utcnow()
            start = int(dt.datetime(now.year, now.month, now.day, tzinfo=dt.timezone.utc).timestamp())
        except Exception:
            # fallback: 24h window
            start = int(time.time()) - 24*3600
        count = 0
        for inv in self.invites.values():
            if inv.created_by == str(creator_id) and (inv.created_at or 0) >= start:
                count += 1
        return count < self.daily_create_limit
