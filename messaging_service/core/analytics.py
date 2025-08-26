import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, List, Tuple

import aiosqlite

_DB_PATH = Path(__file__).parent.parent / "config" / "analytics.db"


async def _ensure_dir() -> None:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)


async def init_db() -> None:
    await _ensure_dir()
    async with aiosqlite.connect(str(_DB_PATH)) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_ts INTEGER NOT NULL,
              kind TEXT NOT NULL,
              platform TEXT,
              chat_id TEXT,
              user_id TEXT,
              meta_json TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_ts INTEGER NOT NULL,
              platform TEXT,
              chat_id TEXT,
              user_id TEXT,
              direction TEXT CHECK(direction IN ('in','out')),
              bytes INTEGER,
              tokens INTEGER,
              message_type TEXT,
              meta_json TEXT
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_ts INTEGER NOT NULL,
              job_name TEXT,
              status TEXT,
              chat_id TEXT,
              user_id TEXT,
              meta_json TEXT
            )
            """
        )
        await db.commit()


async def log_event(kind: str, platform: Optional[str] = None, chat_id: Optional[str] = None, user_id: Optional[str] = None, event_ts: Optional[int] = None, meta: Optional[Dict[str, Any]] = None) -> None:
    await _ensure_dir()
    async with aiosqlite.connect(str(_DB_PATH)) as db:
        await db.execute(
            "INSERT INTO events(event_ts, kind, platform, chat_id, user_id, meta_json) VALUES (?,?,?,?,?,?)",
            (
                event_ts or int(asyncio.get_event_loop().time()),
                kind,
                platform,
                chat_id,
                user_id,
                json.dumps(meta or {}),
            ),
        )
        await db.commit()


async def log_message(platform: str, chat_id: str, user_id: Optional[str], direction: str, bytes_count: int, message_type: Optional[str] = None, tokens: Optional[int] = None, event_ts: Optional[int] = None, meta: Optional[Dict[str, Any]] = None) -> None:
    await _ensure_dir()
    async with aiosqlite.connect(str(_DB_PATH)) as db:
        await db.execute(
            "INSERT INTO messages(event_ts, platform, chat_id, user_id, direction, bytes, tokens, message_type, meta_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                event_ts or int(asyncio.get_event_loop().time()),
                platform,
                chat_id,
                user_id,
                direction,
                int(bytes_count),
                tokens,
                message_type,
                json.dumps(meta or {}),
            ),
        )
        await db.commit()


async def log_job(job_name: str, status: str = "ok", chat_id: Optional[str] = None, user_id: Optional[str] = None, event_ts: Optional[int] = None, meta: Optional[Dict[str, Any]] = None) -> None:
    await _ensure_dir()
    async with aiosqlite.connect(str(_DB_PATH)) as db:
        await db.execute(
            "INSERT INTO jobs(event_ts, job_name, status, chat_id, user_id, meta_json) VALUES (?,?,?,?,?,?)",
            (
                event_ts or int(asyncio.get_event_loop().time()),
                job_name,
                status,
                chat_id,
                user_id,
                json.dumps(meta or {}),
            ),
        )
        await db.commit()


async def query_daily_summary(days: int = 1, chat_id: Optional[str] = None) -> Dict[str, Any]:
    # days counts backwards from now in seconds using event loop time as epoch substitute
    # Note: this uses loop time, sufficient for relative daily windows in long-running bot.
    # For true wall-clock grouping, switch to time.time() everywhere; kept simple for now.
    now = int(asyncio.get_event_loop().time())
    window = 86400 * max(1, days)
    since = now - window
    q = "SELECT direction, COUNT(1), COALESCE(SUM(bytes),0) FROM messages WHERE event_ts>=?"
    params: List[Any] = [since]
    if chat_id:
        q += " AND chat_id=?"
        params.append(chat_id)
    q += " GROUP BY direction"
    res = {"messages_in": 0, "messages_out": 0, "bytes_in": 0, "bytes_out": 0}
    async with aiosqlite.connect(str(_DB_PATH)) as db:
        async with db.execute(q, params) as cur:
            async for direction, cnt, total_bytes in cur:
                if direction == "in":
                    res["messages_in"] = cnt
                    res["bytes_in"] = total_bytes
                elif direction == "out":
                    res["messages_out"] = cnt
                    res["bytes_out"] = total_bytes
    return res


async def top_groups(limit: int = 10, days: int = 7) -> List[Tuple[str, int]]:
    now = int(asyncio.get_event_loop().time())
    since = now - 86400 * max(1, days)
    q = """
        SELECT chat_id, COUNT(1) as c
        FROM messages
        WHERE event_ts>=?
        GROUP BY chat_id
        ORDER BY c DESC
        LIMIT ?
    """
    rows: List[Tuple[str, int]] = []
    async with aiosqlite.connect(str(_DB_PATH)) as db:
        async with db.execute(q, (since, limit)) as cur:
            async for chat_id, c in cur:
                rows.append((str(chat_id), int(c)))
    return rows
