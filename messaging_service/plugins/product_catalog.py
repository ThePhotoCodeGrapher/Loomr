import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.message import Message
from core.service import MessageHandler, MessagingService
from core.event_bus import bus as event_bus


@dataclass
class Product:
    id: str
    title: str
    desc: str
    price: Optional[float] = None
    url: Optional[str] = None
    category: Optional[str] = None


class ProductCatalog(MessageHandler):
    """
    Simple product catalog with inline-button pagination and detail views.

    Commands:
      - /products [category]

    Callback payloads (encoded under '/cb ...'):
      - prod:list:page=<n>[:cat=<cat>]
      - prod:detail:<id>[:page=<n>][:cat=<cat>]
      - prod:back:page=<n>[:cat=<cat>]
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config or {}
        # source
        self.source_type = (self.config.get("source") or "file").lower()
        self.file_path = self.config.get("file_path") or "config/products.json"
        self.page_size = int(self.config.get("page_size") or 5)
        self.buy_link_template = self.config.get("buy_link_template")  # e.g., https://shop/checkout?id={id}
        self.products: List[Product] = []
        self._load_products()
        # delivery config
        self.delivery = self.config.get("delivery") or {}
        self.delivery_mode = (self.delivery.get("mode") or "disabled").lower()
        self.delivery_http = self.delivery.get("http") or {}
        self.delivery_cli = self.delivery.get("cli") or {}
        # payments config (optional)
        self.payments = self.config.get("payments") or {}
        self.payments_mode = (self.payments.get("mode") or "external_url").lower()
        self.pay_currency: Optional[str] = self.payments.get("currency")
        # amounts are in minor units (e.g., cents) or Stars units for XTR
        self.price_map: Dict[str, int] = self.payments.get("price_map") or {}

    # -- MessageHandler --
    async def handle(self, message: Message, service: MessagingService) -> bool:
        text = (message.content or "").strip()

        # Callback-based pagination and actions
        if text.startswith("/cb ") and text[4:].startswith("prod:"):
            payload = text[4:]
            return await self._handle_callback(payload, message, service)

        # Deep-link return delivery: /start deliver-<token>
        if text.startswith("/start ") and "deliver-" in text:
            token = text.split(" ", 1)[1].strip()
            if token.startswith("deliver-"):
                token = token[len("deliver-") :]
                await self._handle_start_deliver(token=token, message=message, service=service)
                return True

        # Entry point: /products [category]
        if text.startswith("/products"):
            _, *rest = text.split(maxsplit=1)
            category = None
            if rest:
                category = rest[0].strip()
                if category.startswith("#"):
                    category = category[1:]
            page = 1
            await self._send_list(message.chat.id, service, page=page, category=category, reply_to=message.message_id)
            return True

        return False

    # -- Core logic --
    def _load_products(self) -> None:
        try:
            if self.source_type == "file":
                p = (Path(__file__).parent.parent / self.file_path).resolve()
                if not p.exists():
                    self.products = []
                    return
                raw = json.loads(p.read_text())
                items = raw.get("products") if isinstance(raw, dict) else raw
                self.products = [Product(**self._normalize_item(it)) for it in (items or [])]
            else:
                # future: API source
                self.products = []
        except Exception:
            self.products = []

    def _normalize_item(self, it: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": str(it.get("id")),
            "title": it.get("title") or "Untitled",
            "desc": it.get("desc") or it.get("description") or "",
            "price": it.get("price"),
            "url": it.get("url"),
            "category": it.get("category"),
        }

    def _filter(self, category: Optional[str]) -> List[Product]:
        if not category:
            return self.products
        cat = category.lower()
        return [p for p in self.products if (p.category or "").lower() == cat]

    def _paginate(self, products: List[Product], page: int) -> Tuple[List[Product], int, int]:
        page = max(1, page)
        n = len(products)
        ps = max(1, self.page_size)
        pages = max(1, (n + ps - 1) // ps)
        page = min(page, pages)
        start = (page - 1) * ps
        end = start + ps
        return products[start:end], page, pages

    def _render_list_text(self, items: List[Product], page: int, pages: int, category: Optional[str]) -> str:
        header = "Products" + (f" — {category}" if category else "")
        lines = [f"{header} (page {page}/{pages})", ""]
        for p in items:
            price = f" — ${p.price:.2f}" if isinstance(p.price, (int, float)) else ""
            lines.append(f"• {p.title}{price}\n  /cb prod:detail:{p.id}:page={page}{':cat='+category if category else ''}")
        if not items:
            lines.append("No products found.")
        return "\n".join(lines)

    def _nav_buttons(self, page: int, pages: int, category: Optional[str]) -> List[List[Dict[str, str]]]:
        cat = f":cat={category}" if category else ""
        rows: List[List[Dict[str, str]]] = []
        nav: List[Dict[str, str]] = []
        if page > 1:
            nav.append({"text": "⬅️ Prev", "callback_data": f"prod:list:page={page-1}{cat}"})
        nav.append({"text": f"{page}/{pages}", "callback_data": f"prod:list:page={page}{cat}"})
        if page < pages:
            nav.append({"text": "Next ➡️", "callback_data": f"prod:list:page={page+1}{cat}"})
        if nav:
            rows.append(nav)
        return rows

    async def _send_list(self, chat_id: str, service: MessagingService, page: int, category: Optional[str], reply_to: Optional[str] = None, edit_message_id: Optional[str] = None) -> None:
        items = self._filter(category)
        page_items, page, pages = self._paginate(items, page)
        text = self._render_list_text(page_items, page, pages, category)
        buttons = self._nav_buttons(page, pages, category)
        if edit_message_id:
            await service.edit_message_text(chat_id=str(chat_id), message_id=str(edit_message_id), text=text, inline_buttons=buttons)
        else:
            await service.send_message(chat_id=str(chat_id), text=text, inline_buttons=buttons, reply_to_message_id=str(reply_to) if reply_to else None)
        # Fire-and-forget event: product list viewed
        try:
            await event_bus.emit(
                "product.list_viewed",
                {
                    "chat_id": str(chat_id),
                    "page": int(page),
                    "pages": int(pages),
                    "category": category or None,
                    "count": len(page_items),
                },
            )
        except Exception:
            pass

    async def _send_detail(self, chat_id: str, service: MessagingService, product_id: str, page: int, category: Optional[str], edit_message_id: str) -> None:
        p = next((x for x in self.products if x.id == product_id), None)
        if not p:
            await self._send_list(chat_id, service, page=page, category=category, edit_message_id=edit_message_id)
            return
        price = f"\nPrice: ${p.price:.2f}" if isinstance(p.price, (int, float)) else ""
        text = f"{p.title}{price}\n\n{p.desc}"
        cat = f":cat={category}" if category else ""
        buy_url = (self.buy_link_template.format(id=p.id) if self.buy_link_template else p.url)
        buttons: List[List[Dict[str, str]]] = []
        row: List[Dict[str, str]] = []
        if buy_url:
            # Provide a tracked Buy action and a direct Open link
            row.append({"text": "Buy", "callback_data": f"prod:buy:{p.id}:page={page}{cat}"})
            row.append({"text": "Open", "url": buy_url})
        row.append({"text": "Back", "callback_data": f"prod:back:page={page}{cat}"})
        if row:
            buttons.append(row)
        await service.edit_message_text(chat_id=str(chat_id), message_id=str(edit_message_id), text=text, inline_buttons=buttons)
        # Fire-and-forget event: product detail viewed
        try:
            await event_bus.emit(
                "product.viewed",
                {
                    "chat_id": str(chat_id),
                    "product_id": str(product_id),
                    "page": int(page),
                    "category": category or None,
                    "price": float(p.price) if isinstance(p.price, (int, float)) else None,
                    "has_buy_url": bool(buy_url),
                },
            )
        except Exception:
            pass

    async def _handle_callback(self, payload: str, message: Message, service: MessagingService) -> bool:
        # payload like: prod:list:page=2:cat=Shoes
        try:
            parts = payload.split(":")  # prod, list|detail|back, ...
            if len(parts) < 2:
                return False
            action = parts[1]
            kv = parts[2:] if len(parts) > 2 else []
            page = 1
            cat: Optional[str] = None
            pid: Optional[str] = None
            for token in kv:
                if token.startswith("page="):
                    try:
                        page = int(token.split("=", 1)[1])
                    except Exception:
                        page = 1
                elif token.startswith("cat="):
                    cat = token.split("=", 1)[1] or None
                else:
                    # for detail id
                    if action == "detail" and not token.startswith("page=") and not token.startswith("cat="):
                        pid = token
            chat_id = message.chat.id
            msg_id = message.message_id
            if action == "list":
                await self._send_list(chat_id, service, page=page, category=cat, edit_message_id=msg_id)
                return True
            if action == "back":
                await self._send_list(chat_id, service, page=page, category=cat, edit_message_id=msg_id)
                return True
            if action == "detail" and pid:
                await self._send_detail(chat_id, service, product_id=pid, page=page, category=cat, edit_message_id=msg_id)
                return True
            if action == "buy":
                # format: prod:buy:<product_id>:page=..:cat=..
                if not pid:
                    # infer pid from kv (3rd segment)
                    if len(parts) >= 3 and not parts[2].startswith("page="):
                        pid = parts[2]
                # find product and construct URL
                pr = next((x for x in self.products if x.id == pid), None)
                buy_url = (self.buy_link_template.format(id=pr.id) if (pr and self.buy_link_template) else (pr.url if pr else None))
                # Try Telegram invoice when configured and supported by adapter
                invoiced = False
                if (
                    self.payments_mode == "telegram_invoice"
                    and hasattr(service, "send_invoice")
                    and pr is not None
                ):
                    try:
                        amount = self.price_map.get(pr.id)
                        # If no explicit mapping and product has numeric price, derive minor units for fiat
                        if amount is None and isinstance(pr.price, (int, float)) and self.pay_currency and self.pay_currency.upper() != "XTR":
                            amount = int(round(float(pr.price) * 100))
                        # Stars (XTR) should use explicit price_map
                        if amount is not None and amount >= 0:
                            payload_str = f"product:{pr.id};user:{message.from_user.id if message.from_user else ''}"
                            title = pr.title[:32]
                            desc = (pr.desc or "")[:255]
                            await getattr(service, "send_invoice")(  # type: ignore[attr-defined]
                                chat_id=str(message.chat.id),
                                title=title,
                                description=desc or title,
                                payload=payload_str,
                                currency=(self.pay_currency or "XTR"),
                                prices=[{"label": title, "amount": int(amount)}],
                            )
                            invoiced = True
                    except Exception:
                        invoiced = False
                # Emit event regardless of URL/invoice
                try:
                    await event_bus.emit(
                        "product.buy_clicked",
                        {
                            "chat_id": str(chat_id),
                            "product_id": str(pid) if pid else None,
                            "page": int(page),
                            "category": cat or None,
                            "has_url": bool(buy_url),
                            "invoiced": bool(invoiced),
                            "currency": (self.pay_currency or None),
                        },
                    )
                except Exception:
                    pass
                # Respond to user
                if invoiced:
                    await service.send_message(chat_id=str(chat_id), text=f"Invoice sent for {pid}. Please complete the payment in Telegram.")
                elif buy_url:
                    await service.send_message(chat_id=str(chat_id), text=f"Checkout link for {pid}:\n{buy_url}")
                else:
                    await service.send_message(chat_id=str(chat_id), text="No checkout link configured for this product.")
                return True
            return False
        except Exception:
            return False

    # ---- Delivery hooks ----
    async def _handle_start_deliver(self, token: str, message: Message, service: MessagingService) -> None:
        """Handle /start deliver-<token> deep link by verifying token with configured API and delivering."""
        if self.delivery_mode == "disabled":
            await service.send_message(chat_id=message.chat.id, text="Delivery is not configured.")
            return
        # Verify token via HTTP (recommended) or pass to CLI
        user_id = message.from_user.id
        chat_id = message.chat.id
        result_text = None
        if self.delivery_mode == "http":
            ok, msg = await self._deliver_via_http(action="verify", token=token, user_id=user_id, chat_id=chat_id)
            result_text = msg
        elif self.delivery_mode == "cli":
            ok, msg = await self._deliver_via_cli(token=token, user_id=user_id, chat_id=chat_id)
            result_text = msg
        else:
            ok, result_text = False, "Unsupported delivery mode"
        await service.send_message(chat_id=chat_id, text=result_text or ("Delivery " + ("succeeded" if ok else "failed")))

    async def _deliver_via_http(self, action: str, token: Optional[str] = None, user_id: Optional[str] = None, chat_id: Optional[str] = None, product_id: Optional[str] = None) -> Tuple[bool, str]:
        from aiohttp import ClientSession
        try:
            cfg = self.delivery_http or {}
            url = cfg.get("url")
            method = (cfg.get("method") or "POST").upper()
            headers = cfg.get("headers") or {}
            body_tmpl = cfg.get("body_template") or "{}"
            # Template variables
            data_str = body_tmpl.format(
                action=action,
                token=token or "",
                user_id=user_id or "",
                chat_id=chat_id or "",
                product_id=product_id or "",
            )
            async with ClientSession() as sess:
                if method == "GET":
                    async with sess.get(url, headers=headers, params=json.loads(data_str)) as resp:
                        ok = resp.status < 400
                        try:
                            payload = await resp.json()
                        except Exception:
                            payload = {"text": await resp.text()}
                else:
                    async with sess.request(method, url, headers=headers, data=data_str) as resp:
                        ok = resp.status < 400
                        try:
                            payload = await resp.json()
                        except Exception:
                            payload = {"text": await resp.text()}
            # Construct user-facing message
            text = (
                payload.get("message")
                or payload.get("text")
                or payload.get("license")
                or payload.get("download_url")
                or ("Delivered: " + json.dumps(payload)[:1000])
            )
            return ok, str(text)
        except Exception as e:
            return False, f"Delivery error: {e}"

    async def _deliver_via_cli(self, token: Optional[str], user_id: Optional[str], chat_id: Optional[str], product_id: Optional[str] = None) -> Tuple[bool, str]:
        import asyncio
        try:
            cfg = self.delivery_cli or {}
            cmd_tmpl = cfg.get("command_template") or ""
            cmd = cmd_tmpl.format(token=token or "", user_id=user_id or "", chat_id=chat_id or "", product_id=product_id or "")
            if not cmd:
                return False, "CLI delivery not configured"
            proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await proc.communicate()
            ok = proc.returncode == 0
            if ok:
                return True, (out.decode().strip() or "Delivered via CLI")
            return False, f"CLI failed: {err.decode().strip()}"
        except Exception as e:
            return False, f"CLI error: {e}"
