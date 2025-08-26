import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set

from core.message import Message
from core.service import MessageHandler, MessagingService
import aiohttp


class JobBoard(MessageHandler):
    """
    Simple job board with first-claim reservation and credits.

    Permissions:
    - Admins (from AdminTools store or config) can post/approve/reject.
    - Optionally allow posting by users with roles in job_board.posters_roles.

    Storage:
    - SQLite db at job_board.db_path (default: data/jobs.db)
    - Reads AdminTools users store (JSON) to resolve role membership for broadcasts.
    """

    def __init__(self, config: Dict[str, Any], admin_cfg: Dict[str, Any]):
        self.config = config or {}
        self.admin_cfg = admin_cfg or {}
        base_dir = Path(__file__).resolve().parent.parent
        # DB path
        db_rel = self.config.get("db_path", "data/jobs.db")
        self.db_path = (base_dir / db_rel).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # AdminTools user store
        store_rel = self.admin_cfg.get("user_store_path", "config/users.json")
        self.user_store_path = (base_dir / store_rel).resolve()
        # Roles
        self.target_roles: List[str] = list(self.config.get("roles", ["influencer"]))
        self.posters_roles: Set[str] = set(self.config.get("posters_roles", []))
        # Defaults
        self.default_verification: str = str(self.config.get("verification", "manual"))
        self.default_credits: int = int(self.config.get("credits", 50))
        self.broadcast_template: str = self.config.get(
            "broadcast_template",
            "New job: {title}\n{desc}\nReward: {credits} credits\nJob ID: {job_id}\nClaim with /claim {job_id}",
        )
        # Seed DB
        self._init_db()

    # --------------- DB Helpers ---------------
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    description TEXT,
                    meta TEXT,
                    verification_type TEXT,
                    hashtag TEXT,
                    payout_credits INTEGER,
                    posted_by TEXT,
                    status TEXT,
                    created_at INTEGER,
                    claimed_by TEXT,
                    -- optional job location
                    location_text TEXT,
                    lat REAL,
                    lon REAL,
                    radius_km REAL,
                    -- crypto payment config
                    crypto_allowed INTEGER,
                    crypto_chain TEXT,
                    crypto_token TEXT,
                    pay_from_address TEXT,
                    min_amount REAL,
                    confirmations INTEGER,
                    auto_approve_on_payment INTEGER
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS claims (
                    job_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    claimed_at INTEGER,
                    submitted_url TEXT,
                    submission_at INTEGER,
                    status TEXT,
                    -- crypto payment fields per-claim
                    worker_wallet TEXT,
                    payment_txid TEXT,
                    payment_status TEXT
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS credits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    delta INTEGER,
                    reason TEXT,
                    ref TEXT,
                    created_at INTEGER
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS user_wallets (
                    user_id TEXT,
                    chain TEXT,
                    address TEXT,
                    PRIMARY KEY (user_id, chain)
                )
                """
            )
            # Safe migrations for already-existing installs (ignore errors if columns exist)
            for ddl in [
                "ALTER TABLE jobs ADD COLUMN location_text TEXT",
                "ALTER TABLE jobs ADD COLUMN lat REAL",
                "ALTER TABLE jobs ADD COLUMN lon REAL",
                "ALTER TABLE jobs ADD COLUMN radius_km REAL",
                "ALTER TABLE jobs ADD COLUMN crypto_allowed INTEGER",
                "ALTER TABLE jobs ADD COLUMN crypto_chain TEXT",
                "ALTER TABLE jobs ADD COLUMN crypto_token TEXT",
                "ALTER TABLE jobs ADD COLUMN pay_from_address TEXT",
                "ALTER TABLE jobs ADD COLUMN min_amount REAL",
                "ALTER TABLE jobs ADD COLUMN confirmations INTEGER",
                "ALTER TABLE jobs ADD COLUMN auto_approve_on_payment INTEGER",
                "ALTER TABLE claims ADD COLUMN worker_wallet TEXT",
                "ALTER TABLE claims ADD COLUMN payment_txid TEXT",
                "ALTER TABLE claims ADD COLUMN payment_status TEXT",
            ]:
                try:
                    c.execute(ddl)
                except Exception:
                    pass

    # --------------- Utility ---------------
    def _now(self) -> int:
        return int(time.time())

    def _load_users_store(self) -> Dict[str, Any]:
        try:
            if self.user_store_path.exists():
                return json.loads(self.user_store_path.read_text())
        except Exception:
            pass
        return {"users": {}}

    def _is_admin(self, user_id: str) -> bool:
        # Merge config admins and store admins
        cfg_admins = set(str(x) for x in (self.admin_cfg.get("admins") or []))
        store = self._load_users_store()
        store_admins = set(str(x) for x in (store.get("admins") or []))
        return str(user_id) in (cfg_admins | store_admins)

    def _has_role(self, user_id: str, role: str) -> bool:
        store = self._load_users_store()
        rec = store.get("users", {}).get(str(user_id))
        if not rec:
            return False
        return role in (rec.get("roles") or [])

    def _allowed_to_post(self, user_id: str) -> bool:
        if self._is_admin(user_id):
            return True
        if not self.posters_roles:
            return False
        return any(self._has_role(user_id, r) for r in self.posters_roles)

    async def _broadcast_to_role(self, role: str, text: str, service: MessagingService) -> int:
        store = self._load_users_store()
        users = store.get("users", {})
        sent = 0
        for _, rec in users.items():
            roles = set(rec.get("roles") or [])
            if role in roles:
                for chat_id in rec.get("chat_ids") or []:
                    try:
                        await service.send_message(chat_id=str(chat_id), text=text)
                        sent += 1
                    except Exception:
                        pass
        return sent

    # --------------- Command Handling ---------------
    async def handle(self, message: Message, service: MessagingService) -> bool:
        text = (message.content or "").strip()
        if not text.startswith('/'):
            return False
        parts = text.split(' ')
        cmd = parts[0].lower()

        # Public
        if cmd == '/jobs':
            await self._cmd_jobs(message, service)
            return True
        if cmd == '/job' and len(parts) >= 2:
            await self._cmd_job(message, service, parts[1])
            return True
        if cmd == '/claim' and len(parts) >= 2:
            await self._cmd_claim(message, service, parts[1])
            return True
        if cmd == '/submit' and len(parts) >= 3:
            await self._cmd_submit(message, service, parts[1], parts[2])
            return True
        if cmd == '/my_jobs':
            await self._cmd_my_jobs(message, service)
            return True
        if cmd == '/credits':
            await self._cmd_credits(message, service)
            return True
        if cmd == '/wallet_set' and len(parts) >= 3:
            await self._cmd_wallet_set(message, service, parts[1], parts[2])
            return True
        if cmd == '/wallet_get':
            chain = parts[1] if len(parts) >= 2 else None
            await self._cmd_wallet_get(message, service, chain)
            return True
        if cmd == '/wallet':
            await self._cmd_wallet_get(message, service, None)
            return True
        if cmd == '/wallet_set_user' and len(parts) >= 4:
            # admin-only: /wallet_set_user <user_id> <chain> <address>
            uid = str(message.from_user.id)
            if not self._is_admin(uid):
                return False
            target_user, chain, address = parts[1], parts[2], parts[3]
            await self._cmd_wallet_set_user(message, service, target_user, chain, address)
            return True
        if cmd == '/check_pay' and len(parts) >= 2:
            await self._cmd_check_pay(message, service, parts[1])
            return True

        # Posting and moderation
        uid = str(message.from_user.id)
        if cmd == '/job_post':
            if not self._allowed_to_post(uid):
                return False
            rest = text[len('/job_post'):].strip()
            await self._cmd_post(message, service, rest)
            return True
        if cmd == '/job_approve' and len(parts) >= 2:
            if not self._is_admin(uid):
                return False
            await self._cmd_approve(message, service, parts[1])
            return True
        if cmd == '/job_reject' and len(parts) >= 3:
            if not self._is_admin(uid):
                return False
            reason = text.split(' ', 2)[2]
            await self._cmd_reject(message, service, parts[1], reason)
            return True
        if cmd == '/job_list':
            if not self._is_admin(uid):
                return False
            status = parts[1] if len(parts) >= 2 else None
            await self._cmd_admin_list(message, service, status)
            return True

        return False

    # --------------- Commands ---------------
    async def _cmd_post(self, message: Message, service: MessagingService, rest: str) -> None:
        # Parse: "<title>" "<desc>" hashtag=#brand credits=50 type=ig_post
        title, desc, args = self._parse_post_args(rest)
        if not title or not desc:
            await service.send_message(chat_id=str(message.chat.id), text="Usage: /job_post \"<title>\" \"<desc>\" [hashtag=#tag] [credits=50] [type=manual|ig_post]")
            return
        job_id = f"J{int(time.time())}"
        verification = args.get('type', self.default_verification)
        hashtag = args.get('hashtag', '')
        credits = int(args.get('credits', self.default_credits))
        # Optional location
        location_text = args.get('location')
        lat = float(args['lat']) if 'lat' in args else None
        lon = float(args['lon']) if 'lon' in args else None
        radius_km = float(args['radius_km']) if 'radius_km' in args else None
        # Optional crypto
        crypto_allowed = str(args.get('crypto_allowed', 'false')).lower() in ('1','true','yes')
        crypto_chain = args.get('chain') if crypto_allowed else None
        crypto_token = args.get('token') if crypto_allowed else None
        pay_from_address = args.get('pay_from') if crypto_allowed else None
        min_amount = float(args['min']) if crypto_allowed and 'min' in args else None
        confirmations = int(args['confirmations']) if crypto_allowed and 'confirmations' in args else None
        auto_approve = str(args.get('auto_approve_on_payment', 'false')).lower() in ('1','true','yes') if crypto_allowed else False
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO jobs(
                    id,title,description,meta,verification_type,hashtag,payout_credits,posted_by,status,created_at,claimed_by,
                    location_text,lat,lon,radius_km,
                    crypto_allowed,crypto_chain,crypto_token,pay_from_address,min_amount,confirmations,auto_approve_on_payment
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?, ?,?,?,?, ?,?,?,?,?,?,?)
                """,
                (
                    job_id,
                    title,
                    desc,
                    json.dumps(args),
                    verification,
                    hashtag,
                    credits,
                    str(message.from_user.id),
                    'open',
                    self._now(),
                    None,
                    location_text,
                    lat,
                    lon,
                    radius_km,
                    1 if crypto_allowed else 0,
                    crypto_chain,
                    crypto_token,
                    pay_from_address,
                    min_amount,
                    confirmations,
                    1 if auto_approve else 0,
                ),
            )
        # Broadcast to each target role
        total = 0
        msg = self.broadcast_template.format(title=title, desc=desc, credits=credits, job_id=job_id)
        for role in self.target_roles:
            try:
                total += await self._broadcast_to_role(role, msg, service)
            except Exception:
                pass
        await service.send_message(chat_id=str(message.chat.id), text=f"Job {job_id} posted and broadcast to {total} chats")

    async def _cmd_jobs(self, message: Message, service: MessagingService) -> None:
        with self._conn() as c:
            rows = c.execute("SELECT id,title,payout_credits,status FROM jobs WHERE status='open' ORDER BY created_at DESC LIMIT 10").fetchall()
        if not rows:
            await service.send_message(chat_id=str(message.chat.id), text="No open jobs.")
            return
        lines = [f"{r['id']}: {r['title']} ({r['payout_credits']} cr)" for r in rows]
        await service.send_message(chat_id=str(message.chat.id), text="Open jobs:\n" + "\n".join(lines))

    async def _cmd_job(self, message: Message, service: MessagingService, job_id: str) -> None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not r:
            await service.send_message(chat_id=str(message.chat.id), text="Job not found")
            return
        text = f"Job {r['id']}\nTitle: {r['title']}\nDesc: {r['description']}\nReward: {r['payout_credits']} credits\nStatus: {r['status']}"
        await service.send_message(chat_id=str(message.chat.id), text=text)

    async def _cmd_claim(self, message: Message, service: MessagingService, job_id: str) -> None:
        uid = str(message.from_user.id)
        with self._conn() as c:
            # atomic first claim via transaction
            cur = c.cursor()
            cur.execute("BEGIN IMMEDIATE")
            row = cur.execute("SELECT status, crypto_allowed, crypto_chain FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row['status'] != 'open':
                cur.execute("ROLLBACK")
                await service.send_message(chat_id=str(message.chat.id), text="Job is not open for claim.")
                return
            cur.execute("UPDATE jobs SET status='claimed', claimed_by=? WHERE id=?", (uid, job_id))
            cur.execute(
                "INSERT OR REPLACE INTO claims(job_id,user_id,claimed_at,submitted_url,submission_at,status) VALUES(?,?,?,?,?,?)",
                (job_id, uid, self._now(), None, None, 'claimed')
            )
            cur.execute("COMMIT")
        # If crypto job, try to attach user's saved wallet for the chain
        if row and row.get('crypto_allowed'):
            chain = row.get('crypto_chain')
            wallet = self._get_user_wallet(uid, chain) if chain else None
            if wallet:
                with self._conn() as c:
                    c.execute("UPDATE claims SET worker_wallet=? WHERE job_id=?", (wallet, job_id))
        await service.send_message(chat_id=str(message.chat.id), text=f"You claimed job {job_id}. Submit proof with /submit {job_id} <url>")

    async def _cmd_submit(self, message: Message, service: MessagingService, job_id: str, url: str) -> None:
        uid = str(message.from_user.id)
        with self._conn() as c:
            r = c.execute("SELECT claimed_by, verification_type FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not r:
                await service.send_message(chat_id=str(message.chat.id), text="Job not found")
                return
            if r['claimed_by'] != uid:
                await service.send_message(chat_id=str(message.chat.id), text="You did not claim this job.")
                return
            c.execute("UPDATE claims SET submitted_url=?, submission_at=?, status='submitted' WHERE job_id=?",
                      (url, self._now(), job_id))
            vtype = r['verification_type']
        if vtype == 'manual':
            await service.send_message(chat_id=str(message.chat.id), text="Submitted. Awaiting review by admin.")
        else:
            # For now, mark submitted; auto verification phase can be added later
            await service.send_message(chat_id=str(message.chat.id), text="Submitted. Verification in progress.")

    async def _cmd_my_jobs(self, message: Message, service: MessagingService) -> None:
        uid = str(message.from_user.id)
        with self._conn() as c:
            rows = c.execute("SELECT j.id,j.title,c.status FROM jobs j JOIN claims c ON j.id=c.job_id WHERE c.user_id=? ORDER BY c.claimed_at DESC LIMIT 10", (uid,)).fetchall()
        if not rows:
            await service.send_message(chat_id=str(message.chat.id), text="You have no active jobs.")
            return
        lines = [f"{r['id']}: {r['title']} ({r['status']})" for r in rows]
        await service.send_message(chat_id=str(message.chat.id), text="Your jobs:\n" + "\n".join(lines))

    async def _cmd_credits(self, message: Message, service: MessagingService) -> None:
        uid = str(message.from_user.id)
        with self._conn() as c:
            row = c.execute("SELECT COALESCE(SUM(delta),0) AS bal FROM credits WHERE user_id=?", (uid,)).fetchone()
        bal = row['bal'] if row else 0
        await service.send_message(chat_id=str(message.chat.id), text=f"Your balance: {bal} credits")

    async def _cmd_approve(self, message: Message, service: MessagingService, job_id: str) -> None:
        with self._conn() as c:
            r = c.execute("SELECT c.user_id, j.payout_credits FROM claims c JOIN jobs j ON j.id=c.job_id WHERE c.job_id=?", (job_id,)).fetchone()
            if not r:
                await service.send_message(chat_id=str(message.chat.id), text="No submission found")
                return
            c.execute("UPDATE claims SET status='approved' WHERE job_id=?", (job_id,))
            c.execute("UPDATE jobs SET status='approved' WHERE id=?", (job_id,))
            c.execute("INSERT INTO credits(user_id,delta,reason,ref,created_at) VALUES(?,?,?,?,?)",
                      (r['user_id'], int(r['payout_credits']), 'job_payout', job_id, self._now()))
        await service.send_message(chat_id=str(message.chat.id), text=f"Job {job_id} approved and credits granted.")

    async def _cmd_reject(self, message: Message, service: MessagingService, job_id: str, reason: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE claims SET status='rejected' WHERE job_id=?", (job_id,))
            c.execute("UPDATE jobs SET status='rejected' WHERE id=?", (job_id,))
        await service.send_message(chat_id=str(message.chat.id), text=f"Job {job_id} rejected. Reason: {reason}")

    async def _cmd_admin_list(self, message: Message, service: MessagingService, status: Optional[str]) -> None:
        q = "SELECT id,title,status FROM jobs"
        args: Tuple[Any, ...] = tuple()
        if status:
            q += " WHERE status=?"
            args = (status,)
        q += " ORDER BY created_at DESC LIMIT 20"
        with self._conn() as c:
            rows = c.execute(q, args).fetchall()
        if not rows:
            await service.send_message(chat_id=str(message.chat.id), text="No jobs.")
            return
        lines = [f"{r['id']}: {r['title']} ({r['status']})" for r in rows]
        await service.send_message(chat_id=str(message.chat.id), text="Jobs:\n" + "\n".join(lines))

    # --------------- Parsing ---------------
    def _parse_post_args(self, rest: str) -> Tuple[Optional[str], Optional[str], Dict[str, str]]:
        # Extract quoted title/desc
        rest = rest.strip()
        if not rest:
            return None, None, {}
        parts: List[str] = []
        current = ''
        in_quote = False
        for ch in rest:
            if ch == '"':
                in_quote = not in_quote
                continue
            if ch == ' ' and not in_quote:
                if current:
                    parts.append(current)
                    current = ''
                continue
            current += ch
        if current:
            parts.append(current)
        if len(parts) < 2:
            return None, None, {}
        title = parts[0]
        desc = parts[1]
        args: Dict[str, str] = {}
        for token in parts[2:]:
            if '=' in token:
                k, v = token.split('=', 1)
                args[k.strip()] = v.strip()
        return title, desc, args

    # --------------- Wallet helpers and commands ---------------
    def _get_user_wallet(self, user_id: str, chain: Optional[str]) -> Optional[str]:
        if not chain:
            return None
        with self._conn() as c:
            r = c.execute("SELECT address FROM user_wallets WHERE user_id=? AND chain=?", (str(user_id), chain.lower())).fetchone()
            return r['address'] if r else None

    def _set_user_wallet(self, user_id: str, chain: str, address: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO user_wallets(user_id,chain,address) VALUES(?,?,?)",
                (str(user_id), chain.lower(), address.strip())
            )

    async def _cmd_wallet_set(self, message: Message, service: MessagingService, chain: str, address: str) -> None:
        if not chain or not address:
            await service.send_message(chat_id=str(message.chat.id), text="Usage: /wallet_set <chain> <address>")
            return
        self._set_user_wallet(message.from_user.id, chain, address)
        await service.send_message(chat_id=str(message.chat.id), text=f"Wallet saved for {chain.upper()}: {address}")

    async def _cmd_wallet_get(self, message: Message, service: MessagingService, chain: Optional[str]) -> None:
        uid = str(message.from_user.id)
        if chain:
            addr = self._get_user_wallet(uid, chain)
            await service.send_message(chat_id=str(message.chat.id), text=f"{chain.upper()} wallet: {addr or '(none)'}")
            return
        # list all
        with self._conn() as c:
            rows = c.execute("SELECT chain,address FROM user_wallets WHERE user_id=? ORDER BY chain", (uid,)).fetchall()
        if not rows:
            await service.send_message(chat_id=str(message.chat.id), text="No wallets saved. Use /wallet_set <chain> <address>.")
            return
        lines = [f"{r['chain'].upper()}: {r['address']}" for r in rows]
        await service.send_message(chat_id=str(message.chat.id), text="Your wallets:\n" + "\n".join(lines))

    async def _cmd_wallet_set_user(self, message: Message, service: MessagingService, target_user: str, chain: str, address: str) -> None:
        if not target_user or not chain or not address:
            await service.send_message(chat_id=str(message.chat.id), text="Usage: /wallet_set_user <user_id> <chain> <address>")
            return
        self._set_user_wallet(target_user, chain, address)
        await service.send_message(chat_id=str(message.chat.id), text=f"Set {chain.upper()} wallet for user {target_user}: {address}")

    async def _cmd_check_pay(self, message: Message, service: MessagingService, job_id: str) -> None:
        with self._conn() as c:
            j = c.execute("SELECT crypto_allowed,crypto_chain,crypto_token,min_amount,confirmations,pay_from_address FROM jobs WHERE id=?", (job_id,)).fetchone()
            cl = c.execute("SELECT worker_wallet,payment_status,payment_txid FROM claims WHERE job_id=?", (job_id,)).fetchone()
        if not j or not cl:
            await service.send_message(chat_id=str(message.chat.id), text="No such claim or job.")
            return
        if not j['crypto_allowed']:
            await service.send_message(chat_id=str(message.chat.id), text="This job is not crypto-enabled.")
            return
        chain = (j['crypto_chain'] or '').upper()
        wallet = cl['worker_wallet']
        if not wallet:
            await service.send_message(chat_id=str(message.chat.id), text="No worker wallet set. Use /wallet_set <chain> <address> and re-claim or contact admin.")
            return
        min_amount = float(j['min_amount']) if j['min_amount'] is not None else 0.0
        confirmations = int(j['confirmations'] or 1)
        pay_from = j['pay_from_address']

        try:
            found = None
            if chain in ("BTC", "LTC", "DOGE", "ETH"):
                found = await self._check_blockcypher(chain, wallet, min_amount, confirmations, pay_from)
            elif chain == "XRP":
                found = await self._check_xrp(wallet, min_amount, pay_from)
            elif chain == "XLM":
                found = await self._check_xlm(wallet, min_amount, pay_from)
            else:
                await service.send_message(chat_id=str(message.chat.id), text=f"Unsupported chain: {chain}")
                return

            if found and found.get('txid'):
                status_text = 'confirmed' if found.get('confirmed', False) else 'detected'
                with self._conn() as c:
                    c.execute("UPDATE claims SET payment_txid=?, payment_status=? WHERE job_id=?", (found['txid'], status_text, job_id))
                await service.send_message(chat_id=str(message.chat.id), text=f"Payment {status_text}: tx {found['txid']} amount {found['amount']} {chain}")
            else:
                await service.send_message(chat_id=str(message.chat.id), text=f"No qualifying payment found yet for {wallet} on {chain}.")
        except Exception as e:
            await service.send_message(chat_id=str(message.chat.id), text=f"Payment check error: {e}")

    # -------- Crypto checkers --------
    async def _check_blockcypher(self, chain: str, address: str, min_amount: float, min_conf: int, pay_from: Optional[str]) -> Optional[Dict[str, Any]]:
        chain_map = {"BTC": "btc", "LTC": "ltc", "DOGE": "doge", "ETH": "eth"}
        base = chain_map.get(chain)
        if not base:
            return None
        token = None  # optionally read from env later
        url = f"https://api.blockcypher.com/v1/{base}/main/addrs/{address}/full?limit=50"
        if token:
            url += f"&token={token}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=20) as resp:
                if resp.status != 200:
                    raise Exception(f"BlockCypher HTTP {resp.status}")
                data = await resp.json()
        txs = data.get('txs') or []
        # unit divisors
        divisor = 1e8 if chain in ("BTC", "LTC", "DOGE") else 1e18  # ETH wei
        for tx in txs:
            conf = int(tx.get('confirmations') or 0)
            if conf < min_conf:
                continue
            # Optional from filter
            if pay_from:
                inputs = tx.get('inputs') or []
                in_addrs = set(a for i in inputs for a in (i.get('addresses') or []))
                if pay_from not in in_addrs:
                    continue
            # Sum outputs to our address
            outs = tx.get('outputs') or []
            amt_sats = 0
            for o in outs:
                if address in (o.get('addresses') or []):
                    v = o.get('value')
                    if v is not None:
                        amt_sats += int(v)
            amt = amt_sats / divisor
            if amt >= min_amount and amt > 0:
                return {"txid": tx.get('hash'), "amount": amt, "confirmed": True}
        return None

    async def _check_xrp(self, address: str, min_amount: float, pay_from: Optional[str]) -> Optional[Dict[str, Any]]:
        # Ripple Data API
        url = f"https://data.ripple.com/v2/accounts/{address}/transactions?type=Payment&result=tesSUCCESS&limit=20"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=20) as resp:
                if resp.status != 200:
                    raise Exception(f"XRPL HTTP {resp.status}")
                data = await resp.json()
        txs = data.get('transactions') or []
        for item in txs:
            tx = item.get('tx') or {}
            meta = item.get('meta') or {}
            if pay_from and tx.get('Account') != pay_from:
                continue
            amt = tx.get('Amount')
            if isinstance(amt, str):  # drops
                value = float(amt) / 1_000_000
            elif isinstance(amt, dict) and amt.get('currency') == 'XRP':
                value = float(amt.get('value', 0))
            else:
                continue
            if value >= min_amount:
                return {"txid": tx.get('hash') or item.get('hash'), "amount": value, "confirmed": True}
        return None

    async def _check_xlm(self, address: str, min_amount: float, pay_from: Optional[str]) -> Optional[Dict[str, Any]]:
        url = f"https://horizon.stellar.org/accounts/{address}/payments?order=desc&limit=20"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=20) as resp:
                if resp.status != 200:
                    raise Exception(f"Stellar HTTP {resp.status}")
                data = await resp.json()
        records = (data.get('_embedded') or {}).get('records') or []
        for rec in records:
            if rec.get('type') != 'payment' or rec.get('asset_type') != 'native':
                continue
            if pay_from and rec.get('from') != pay_from:
                continue
            value = float(rec.get('amount') or 0)
            if value >= min_amount and rec.get('to') == address:
                return {"txid": rec.get('transaction_hash'), "amount": value, "confirmed": True}
        return None
