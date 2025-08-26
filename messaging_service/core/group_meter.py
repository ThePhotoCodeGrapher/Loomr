import json
from dataclasses import dataclass
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


@dataclass
class GroupStatus:
    active: bool
    credits: int
    gateway: Optional[str] = None  # arbitrary external service identifier/URL
    profile: Optional[str] = None  # e.g., "standard", "pro"
    plan_expires_at: Optional[int] = None  # unix epoch seconds


class GroupMeter:
    """
    Persistent group activation and credit accounting.

    - Stores state in config/groups.json with structure:
      {
        "groups": {
          "<chat_id>": {"active": bool, "credits": int, "gateway": str|null}
        }
      }
    - Rates and settings are read from config.group_activation
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = (config or {}).get("group_activation", {})
        base = Path(__file__).resolve().parent.parent
        self.store_path = Path(base / "config/groups.json")
        sp = self.config.get("store_path")
        if sp:
            self.store_path = Path(base / sp)
        # Defaults
        self.min_activation_ton: float = float(self.config.get("min_activation_ton") or 0.001)
        self.ton_to_credits_rate: float = float(self.config.get("ton_to_credits_rate") or 1_000_000.0)
        self.in_cost_per_kb: float = float(self.config.get("in_cost_per_kb_credits") or 1.0)
        self.out_cost_per_kb: float = float(self.config.get("out_cost_per_kb_credits") or 0.25)
        # Optional rate profiles override
        self.rate_profiles: Dict[str, Dict[str, float]] = self._load_rate_profiles()
        self.code_bundle_credits: int = int(self.config.get("activation_code_bundle_credits") or 10_000)
        self._ensure_store()

    # ---- store helpers ----
    def _ensure_store(self) -> None:
        if not self.store_path.exists():
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            self.store_path.write_text(json.dumps({"groups": {}}, ensure_ascii=False, indent=2))

    def _read(self) -> Dict[str, Any]:
        try:
            return json.loads(self.store_path.read_text())
        except Exception:
            return {"groups": {}}

    def _write(self, data: Dict[str, Any]) -> None:
        try:
            self.store_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception:
            pass

    # ---- public API ----
    def get_status(self, chat_id: str) -> GroupStatus:
        data = self._read()
        g = (data.get("groups") or {}).get(str(chat_id)) or {}
        # Downgrade expired plans on read
        self._maybe_downgrade_if_expired(g)
        return GroupStatus(
            active=bool(g.get("active")),
            credits=int(g.get("credits") or 0),
            gateway=g.get("gateway"),
            profile=g.get("profile"),
            plan_expires_at=g.get("plan_expires_at"),
        )

    def set_gateway(self, chat_id: str, gateway: Optional[str]) -> None:
        data = self._read()
        groups = data.setdefault("groups", {})
        g = groups.setdefault(str(chat_id), {"active": False, "credits": 0})
        g["gateway"] = gateway
        self._write(data)

    # ---- profiles / plans ----
    def set_profile(self, chat_id: str, profile: Optional[str], months: Optional[int] = None) -> None:
        data = self._read()
        groups = data.setdefault("groups", {})
        g = groups.setdefault(str(chat_id), {"active": False, "credits": 0})
        if profile:
            g["profile"] = str(profile)
        else:
            g.pop("profile", None)
        if months is not None:
            # extend or set expiry months ahead from now
            now = int(time.time())
            base_ts = int(g.get("plan_expires_at") or 0)
            if base_ts and base_ts > now:
                start = base_ts
            else:
                start = now
            # approximate month length = 30 days
            g["plan_expires_at"] = int(start + months * 30 * 24 * 3600)
        self._write(data)

    def clear_expiry(self, chat_id: str) -> None:
        data = self._read()
        groups = data.setdefault("groups", {})
        g = groups.setdefault(str(chat_id), {"active": False, "credits": 0})
        g.pop("plan_expires_at", None)
        self._write(data)

    def get_effective_rates(self, chat_id: str) -> Tuple[float, float, str]:
        """Return (in_cost, out_cost, profile) after applying profile and expiry logic."""
        data = self._read()
        groups = data.setdefault("groups", {})
        g = groups.setdefault(str(chat_id), {"active": False, "credits": 0})
        self._maybe_downgrade_if_expired(g)
        profile = str(g.get("profile") or self._default_profile())
        rp = self.rate_profiles.get(profile)
        if rp:
            in_cost = float(rp.get("in", self.in_cost_per_kb))
            out_cost = float(rp.get("out", self.out_cost_per_kb))
        else:
            in_cost = self.in_cost_per_kb
            out_cost = self.out_cost_per_kb
        return in_cost, out_cost, profile

    def activate(self, chat_id: str) -> None:
        data = self._read()
        groups = data.setdefault("groups", {})
        g = groups.setdefault(str(chat_id), {"active": False, "credits": 0})
        g["active"] = True
        self._write(data)

    def add_credits(self, chat_id: str, credits: int) -> None:
        data = self._read()
        groups = data.setdefault("groups", {})
        g = groups.setdefault(str(chat_id), {"active": False, "credits": 0})
        g["credits"] = int(g.get("credits") or 0) + int(max(0, credits))
        self._write(data)

    def spend_inbound(self, chat_id: str, bytes_len: int) -> Tuple[bool, int]:
        """Spend credits for inbound payload; returns (ok, remaining)."""
        kb = (max(0, int(bytes_len)) + 1023) // 1024
        in_cost, _, _ = self.get_effective_rates(chat_id)
        cost = int(round(kb * in_cost))
        return self._spend(chat_id, cost)

    def spend_outbound(self, chat_id: str, bytes_len: int) -> Tuple[bool, int]:
        """Spend credits for outbound payload; returns (ok, remaining)."""
        kb = (max(0, int(bytes_len)) + 1023) // 1024
        _, out_cost, _ = self.get_effective_rates(chat_id)
        cost = int(round(kb * out_cost))
        return self._spend(chat_id, cost)

    def _spend(self, chat_id: str, cost: int) -> Tuple[bool, int]:
        data = self._read()
        groups = data.setdefault("groups", {})
        g = groups.setdefault(str(chat_id), {"active": False, "credits": 0})
        cur = int(g.get("credits") or 0)
        if cur < cost:
            g["credits"] = cur
            self._write(data)
            return False, cur
        g["credits"] = cur - cost
        self._write(data)
        return True, g["credits"]

    # ---- helpers for TON mapping ----
    def credits_for_ton(self, amount_ton: float) -> int:
        return int(round(float(amount_ton or 0.0) * self.ton_to_credits_rate))

    # ---- activation via code ----
    def activate_with_code(self, chat_id: str, code: str) -> bool:
        codes = [str(c) for c in (self.config.get("activation_codes") or [])]
        if not codes or code not in codes:
            return False
        self.activate(chat_id)
        if self.code_bundle_credits > 0:
            self.add_credits(chat_id, int(self.code_bundle_credits))
        return True

    # ---- internals ----
    def _default_profile(self) -> str:
        return str((self.config.get("default_profile") or "standard")).strip()

    def _load_rate_profiles(self) -> Dict[str, Dict[str, float]]:
        profiles = {}
        rp = self.config.get("rate_profiles") or {}
        # normalize
        try:
            for name, vals in rp.items():
                profiles[str(name)] = {
                    "in": float(vals.get("in", self.in_cost_per_kb)),
                    "out": float(vals.get("out", self.out_cost_per_kb)),
                }
        except Exception:
            pass
        # Always ensure default profile exists pointing to base costs
        profiles.setdefault(self._default_profile(), {"in": self.in_cost_per_kb, "out": self.out_cost_per_kb})
        return profiles

    def _maybe_downgrade_if_expired(self, g: Dict[str, Any]) -> None:
        try:
            exp = int(g.get("plan_expires_at") or 0)
        except Exception:
            exp = 0
        if not exp:
            return
        now = int(time.time())
        if exp <= now:
            # Expired: revert to default profile and clear expiry
            g["profile"] = self._default_profile()
            g.pop("plan_expires_at", None)
            # Persist downgrade
            data = self._read()
            groups = data.setdefault("groups", {})
            # Find current group key by value reference is tricky; rely on caller updating store afterward.
            # Instead, we locate by scanning; acceptable for small JSON.
            for k, v in list(groups.items()):
                if v is g:
                    groups[k] = g
                    break
            self._write(data)
