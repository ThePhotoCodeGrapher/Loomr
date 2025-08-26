from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.message import Message
from core.service import MessageHandler, MessagingService


class LocationDemo(MessageHandler):
    """
    Location demo commands using last known user locations persisted by FileRouter.

    Commands (scoped per chat):
    - /nearby [radius_m]
    - /friend_find <@username|user_id> [radius_m]
    - /radar [limit]
    - /myloc

    Config example (config.yaml under plugins.location_demo):
      location_demo:
        default_radius_m: 200
        max_age_s: 43200  # ignore older than 12h
        max_results: 10
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or {}
        base_dir = Path(__file__).parent.parent
        self.store_path = (base_dir / "data/locations.json").resolve()
        self.default_radius_m: float = float(cfg.get("default_radius_m", 200))
        self.max_age_s: int = int(cfg.get("max_age_s", 12 * 3600))
        self.max_results: int = int(cfg.get("max_results", 10))

    async def handle(self, message: Message, service: MessagingService) -> bool:
        text = (message.content or "").strip()
        if not text.startswith("/"):
            return False
        parts = text.split()
        cmd = parts[0].lower()

        if cmd == "/nearby":
            radius_m = self._parse_float(parts[1]) if len(parts) >= 2 else self.default_radius_m
            await self._cmd_nearby(message, service, radius_m)
            return True
        if cmd == "/friend_find":
            if len(parts) < 2:
                await service.send_message(chat_id=str(message.chat.id), text="Usage: /friend_find <@username|user_id> [radius_m]", reply_to_message_id=str(message.message_id))
                return True
            target = parts[1]
            radius_m = self._parse_float(parts[2]) if len(parts) >= 3 else self.default_radius_m
            await self._cmd_friend_find(message, service, target, radius_m)
            return True
        if cmd == "/radar":
            limit = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 5
            await self._cmd_radar(message, service, limit)
            return True
        if cmd == "/myloc":
            await self._cmd_myloc(message, service)
            return True

        return False

    async def _cmd_nearby(self, message: Message, service: MessagingService, radius_m: float) -> None:
        me = self._get_my_location(message)
        if not me:
            await service.send_message(chat_id=str(message.chat.id), text="Send your location first.", reply_to_message_id=str(message.message_id))
            return
        my_lat, my_lon = me["lat"], me["lon"]
        peers = self._get_chat_locations(str(message.chat.id), exclude_user=str(message.from_user.id))
        now = int(time.time())
        results: List[Tuple[float, Dict[str, Any]]] = []
        for _, rec in peers.items():
            if not self._fresh(rec, now):
                continue
            d = self._haversine_m(my_lat, my_lon, rec["lat"], rec["lon"])
            if d <= radius_m:
                results.append((d, rec))
        results.sort(key=lambda x: x[0])
        if not results:
            await service.send_message(chat_id=str(message.chat.id), text=f"No one within {int(radius_m)} m.", reply_to_message_id=str(message.message_id))
            return
        lines = [self._fmt_user(rec, dist_m=d) for d, rec in results[: self.max_results]]
        await service.send_message(chat_id=str(message.chat.id), text="Nearby:\n" + "\n".join(lines), reply_to_message_id=str(message.message_id))

    async def _cmd_friend_find(self, message: Message, service: MessagingService, target: str, radius_m: float) -> None:
        me = self._get_my_location(message)
        if not me:
            await service.send_message(chat_id=str(message.chat.id), text="Send your location first.", reply_to_message_id=str(message.message_id))
            return
        peers = self._get_chat_locations(str(message.chat.id), exclude_user=str(message.from_user.id))
        key = target.lstrip("@") if target.startswith("@") else target
        # Try by username first
        friend = None
        for rec in peers.values():
            if str(rec.get("username") or "") == key or str(rec.get("user_id")) == key:
                friend = rec
                break
        if not friend or not self._fresh(friend, int(time.time())):
            await service.send_message(chat_id=str(message.chat.id), text="Friend location not found or too old.", reply_to_message_id=str(message.message_id))
            return
        d = self._haversine_m(me["lat"], me["lon"], friend["lat"], friend["lon"])
        txt = self._fmt_user(friend, dist_m=d)
        if d <= radius_m:
            txt = f"Friend in range ({int(radius_m)} m):\n" + txt
        else:
            txt = f"Friend is {int(d)} m away (outside {int(radius_m)} m):\n" + txt
        await service.send_message(chat_id=str(message.chat.id), text=txt, reply_to_message_id=str(message.message_id))

    async def _cmd_radar(self, message: Message, service: MessagingService, limit: int) -> None:
        me = self._get_my_location(message)
        if not me:
            await service.send_message(chat_id=str(message.chat.id), text="Send your location first.", reply_to_message_id=str(message.message_id))
            return
        my_lat, my_lon = me["lat"], me["lon"]
        peers = self._get_chat_locations(str(message.chat.id), exclude_user=str(message.from_user.id))
        now = int(time.time())
        results: List[Tuple[float, Dict[str, Any]]] = []
        for _, rec in peers.items():
            if not self._fresh(rec, now):
                continue
            d = self._haversine_m(my_lat, my_lon, rec["lat"], rec["lon"])
            results.append((d, rec))
        results.sort(key=lambda x: x[0])
        if not results:
            await service.send_message(chat_id=str(message.chat.id), text="No tracked peers yet.", reply_to_message_id=str(message.message_id))
            return
        lines = [self._fmt_user(rec, dist_m=d) for d, rec in results[: max(1, int(limit))]]
        await service.send_message(chat_id=str(message.chat.id), text="Radar:\n" + "\n".join(lines), reply_to_message_id=str(message.message_id))

    # ---- Helpers ----
    async def _cmd_myloc(self, message: Message, service: MessagingService) -> None:
        me = self._get_my_location(message)
        if not me:
            await service.send_message(chat_id=str(message.chat.id), text="No location on file. Send your location first.", reply_to_message_id=str(message.message_id))
            return
        lat, lon = me.get("lat"), me.get("lon")
        ts = me.get("ts")
        maps = f"https://maps.google.com/?q={lat},{lon}" if (lat is not None and lon is not None) else ""
        await service.send_message(chat_id=str(message.chat.id), text=f"Your last location:\nLat: {lat}, Lon: {lon}\nUpdated: {ts}{('\n' + maps) if maps else ''}", reply_to_message_id=str(message.message_id))

    def _get_my_location(self, message: Message) -> Optional[Dict[str, Any]]:
        chat_key = str(message.chat.id)
        user_key = str(message.from_user.id)
        data = self._load_store()
        rec = ((data.get("chats") or {}).get(chat_key) or {}).get(user_key)
        return rec

    def _get_chat_locations(self, chat_id: str, exclude_user: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        data = self._load_store()
        users = (data.get("chats") or {}).get(str(chat_id)) or {}
        if exclude_user is not None:
            users = {uid: rec for uid, rec in users.items() if str(uid) != str(exclude_user)}
        return users

    def _load_store(self) -> Dict[str, Any]:
        try:
            if self.store_path.exists():
                with open(self.store_path, "r") as f:
                    return json.load(f) or {}
        except Exception:
            pass
        return {}

    def _fresh(self, rec: Dict[str, Any], now: int) -> bool:
        ts = int(rec.get("ts") or 0)
        return ts > 0 and (now - ts) <= self.max_age_s

    def _haversine_m(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R = 6371000.0  # meters
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2.0) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c

    def _fmt_user(self, rec: Dict[str, Any], dist_m: Optional[float] = None) -> str:
        name = rec.get("username") or rec.get("first_name") or rec.get("user_id")
        dist = f" — {int(dist_m)} m" if dist_m is not None else ""
        return f"• {name}{dist}"

    def _parse_float(self, s: str) -> float:
        try:
            return float(s)
        except Exception:
            return self.default_radius_m
