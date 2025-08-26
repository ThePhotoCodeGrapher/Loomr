import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Set
from core.event_bus import bus as event_bus

# Path aligned with AdminTools default
USER_STORE_PATH = Path(__file__).resolve().parent.parent / "config/users.json"
USER_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)


@dataclass
class UserRecord:
    id: str
    username: Optional[str] = None
    chat_ids: Set[str] = field(default_factory=set)
    roles: Set[str] = field(default_factory=set)
    role_expiry: Dict[str, float] = field(default_factory=dict)  # role -> unix_ts
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
        role_expiry = d.get("role_expiry") or {}
        try:
            rec.role_expiry = {str(k): float(v) for k, v in role_expiry.items()}
        except Exception:
            rec.role_expiry = {}
        rec.location_text = d.get("location_text")
        try:
            rec.lat = float(d.get("lat")) if d.get("lat") is not None else None
            rec.lon = float(d.get("lon")) if d.get("lon") is not None else None
        except Exception:
            rec.lat, rec.lon = None, None
        return rec


def _load_store(path: Path = USER_STORE_PATH) -> Dict[str, Any]:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {"users": {}, "admins": []}


def _save_store(data: Dict[str, Any], path: Path = USER_STORE_PATH) -> None:
    try:
        path.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


def ensure_user(user_id: str, username: Optional[str] = None, chat_id: Optional[str] = None) -> UserRecord:
    data = _load_store()
    users = data.get("users") or {}
    uid = str(user_id)
    raw = users.get(uid) or {"id": uid}
    rec = UserRecord.from_dict(raw)
    if username:
        rec.username = username
    if chat_id:
        rec.chat_ids.add(str(chat_id))
    users[uid] = rec.to_dict()
    data["users"] = users
    _save_store(data)
    return rec


def set_role(user_id: str, role: str, days: Optional[int] = None) -> None:
    data = _load_store()
    users = data.get("users") or {}
    uid = str(user_id)
    raw = users.get(uid) or {"id": uid}
    rec = UserRecord.from_dict(raw)
    rec.roles.add(role)
    if days and days > 0:
        now = time.time()
        expires = max(rec.role_expiry.get(role, 0), now) + days * 86400
        rec.role_expiry[role] = expires
    users[uid] = rec.to_dict()
    data["users"] = users
    _save_store(data)
    # Emit role assigned event (async, fire-and-forget)
    try:
        event_bus.emit_sync(
            "role.assigned",
            {
                "user_id": uid,
                "role": role,
                "days": int(days) if days else None,
                "expires_at": float(rec.role_expiry.get(role)) if rec.role_expiry.get(role) else None,
            },
        )
    except Exception:
        pass


def get_roles(user_id: str) -> Dict[str, Any]:
    data = _load_store()
    users = data.get("users") or {}
    uid = str(user_id)
    raw = users.get(uid) or {"id": uid}
    rec = UserRecord.from_dict(raw)
    return {"roles": sorted(rec.roles), "role_expiry": rec.role_expiry}
