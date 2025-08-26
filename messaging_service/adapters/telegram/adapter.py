import logging
import asyncio
from typing import Dict, Any, Optional
try:
    import qrcode  # optional, used to render TON payment QR in welcome
    from io import BytesIO
except Exception:
    qrcode = None
    BytesIO = None

from aiohttp import web
from aiogram import Bot
from aiogram import F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    BotCommand,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
    InputMediaPhoto,
    WebAppInfo,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiogram.dispatcher.dispatcher import Dispatcher
from aiogram import Router

from core.message import Message as CoreMessage, User, Chat, MessageType
from core.service import MessagingService
from core.event_bus import bus as event_bus
from core import analytics

logger = logging.getLogger(__name__)


class TelegramAdapter(MessagingService):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        # Accept either 'token' (preferred) or legacy 'telegram_token'
        self.token = config.get('token') or config.get('telegram_token')
        if not self.token:
            raise ValueError("Telegram token is required in config")

        # Mode: 'polling' (default) or 'webhook'
        tg_cfg = config or {}
        self.mode = (tg_cfg.get('mode') or 'polling').lower()
        self.webhook_cfg = tg_cfg.get('webhook') or {}

        # Payments config (optional)
        pay_cfg = tg_cfg.get('payments') or {}
        # For Wallet/Providers-based payments, provider_token is required by Telegram
        self.provider_token: Optional[str] = pay_cfg.get('provider_token')

        # Reply keyboard defaults (configurable)
        kb_cfg = tg_cfg.get('keyboard') or {}
        self.kb_resize: bool = bool(kb_cfg.get('resize', True))
        self.kb_one_time: bool = bool(kb_cfg.get('one_time', True))

        # aiogram primitives
        self.bot = Bot(self.token)
        self.router = Router()
        self.dp = Dispatcher()
        self.dp.include_router(self.router)

        # Background task handles (polling/webhook server)
        self._bg_task: Optional[asyncio.Task] = None
        self._web_runner: Optional[web.AppRunner] = None

        # Register handlers
        self._register_handlers()

    async def _apply_bot_menu(self) -> None:
        """Set bot command menu from config if provided."""
        try:
            menu_cfg = (self.config or {}).get('menu') or {}
            commands_cfg = menu_cfg.get('commands') or []
            if not commands_cfg:
                return
            commands = []
            for c in commands_cfg:
                cmd = str(c.get('command', '')).lstrip('/')
                desc = str(c.get('description', ''))
                if not cmd:
                    continue
                commands.append(BotCommand(command=cmd, description=desc[:256]))
            if commands:
                await self.bot.set_my_commands(commands)
        except Exception:
            logger.exception("Failed to set bot menu commands from config")

    def _register_handlers(self) -> None:
        @self.router.message(CommandStart())
        async def on_start(message: Message):
            await self._on_command(message)

        @self.router.message(Command("help"))
        async def on_help(message: Message):
            await self._on_command(message)

        @self.router.message(F.text)
        async def on_text(message: Message):
            await self._on_text(message)

        # Non-text handlers: forward to plugins
        @self.router.message(F.photo)
        async def on_photo(message: Message):
            core_message = self._to_core_message(message)
            await self._handle_message(core_message)

        @self.router.message(F.document)
        async def on_document(message: Message):
            core_message = self._to_core_message(message)
            await self._handle_message(core_message)

        @self.router.message(F.video)
        async def on_video(message: Message):
            core_message = self._to_core_message(message)
            await self._handle_message(core_message)

        @self.router.message(F.audio)
        async def on_audio(message: Message):
            core_message = self._to_core_message(message)
            await self._handle_message(core_message)

        @self.router.message(F.voice)
        async def on_voice(message: Message):
            # Treat voice notes as audio for downstream handlers
            core_message = self._to_core_message(message)
            await self._handle_message(core_message)

        @self.router.message(F.sticker)
        async def on_sticker(message: Message):
            core_message = self._to_core_message(message)
            await self._handle_message(core_message)

        @self.router.message(F.location)
        async def on_location(message: Message):
            core_message = self._to_core_message(message)
            await self._handle_message(core_message)

        @self.router.message(F.contact)
        async def on_contact(message: Message):
            core_message = self._to_core_message(message)
            await self._handle_message(core_message)

        # Catch-all logger for any other messages
        @self.router.message()
        async def on_any(message: Message):
            try:
                logger.info(
                    "Any message: chat_id=%s user=%s content_type=%s",
                    message.chat.id,
                    message.from_user.username if message.from_user else None,
                    getattr(message, 'content_type', 'unknown'),
                )
            except Exception:
                pass

        # Inline button callbacks
        @self.router.callback_query()
        async def on_callback(callback_query):
            try:
                data = getattr(callback_query, 'data', None)
                logger.info(
                    "Callback: chat_id=%s user=%s data=%r",
                    getattr(getattr(callback_query, 'message', None), 'chat', {}).id if getattr(callback_query, 'message', None) else None,
                    getattr(getattr(callback_query, 'from_user', None), 'username', None),
                    data,
                )
            except Exception:
                data = None
            # Build a CoreMessage-like wrapper using the underlying message
            tg_message = getattr(callback_query, 'message', None)
            if tg_message is None:
                await callback_query.answer()
                return
            core_message = self._to_core_message(tg_message)
            # Encode callback data into content for plugin routing
            if data:
                core_message.content = f"/cb {data}"
            await self._handle_message(core_message)
            # Always answer the callback to stop loading state
            try:
                await callback_query.answer()
            except Exception:
                pass

        # Payments: PreCheckout approval
        try:
            from aiogram.types import PreCheckoutQuery

            @self.router.pre_checkout_query()
            async def on_pre_checkout(pcq: PreCheckoutQuery):
                try:
                    # TODO: add business validation if needed; for now approve
                    await self.bot.answer_pre_checkout_query(pcq.id, ok=True)
                except Exception:
                    try:
                        await self.bot.answer_pre_checkout_query(pcq.id, ok=False, error_message="Payment temporarily unavailable")
                    except Exception:
                        pass
        except Exception:
            # aiogram types/decorator not available — skip payments handling
            pass

        # Payments: Successful payment
        try:
            @self.router.message(F.successful_payment)
            async def on_successful_payment(message: Message):
                try:
                    sp = message.successful_payment
                    payload = getattr(sp, 'invoice_payload', None)
                    total_amount = getattr(sp, 'total_amount', None)
                    currency = getattr(sp, 'currency', None)
                    provider_charge_id = getattr(sp, 'provider_payment_charge_id', None)
                    telegram_charge_id = getattr(sp, 'telegram_payment_charge_id', None)
                    data = {
                        "chat_id": str(message.chat.id),
                        "user_id": str(message.from_user.id) if message.from_user else None,
                        "payload": payload,
                        "total_amount": int(total_amount) if total_amount is not None else None,
                        "currency": currency,
                        "provider_payment_charge_id": provider_charge_id,
                        "telegram_payment_charge_id": telegram_charge_id,
                    }
                    # Emit event for downstream processing (delivery, roles, etc.)
                    try:
                        await event_bus.emit("payment.tg.confirmed", data)
                    except Exception:
                        pass
                except Exception:
                    pass
        except Exception:
            pass

        # Group membership updates: detect when the bot is added/removed from groups
        try:
            from aiogram.types import ChatMemberUpdated

            @self.router.my_chat_member()
            async def on_my_chat_member(update: ChatMemberUpdated):
                try:
                    me = await self.bot.get_me()
                    bot_id = getattr(me, 'id', None)
                    chat = getattr(update, 'chat', None)
                    old = getattr(update, 'old_chat_member', None)
                    new = getattr(update, 'new_chat_member', None)
                    new_status = getattr(new, 'status', None)
                    old_status = getattr(old, 'status', None)
                    user = getattr(getattr(update, 'from_user', None), 'id', None)
                    # Added if status becomes member/administrator from something else
                    if bot_id and getattr(new, 'user', None) and getattr(new.user, 'id', None) == bot_id:
                        if str(new_status) in {"member", "administrator"} and str(old_status) not in {"member", "administrator"}:
                            # Emit event and send welcome with chat id
                            payload = {
                                "chat_id": str(getattr(chat, 'id', '')),
                                "title": getattr(chat, 'title', None),
                                "type": getattr(chat, 'type', None),
                                "inviter_user_id": str(user) if user else None,
                            }
                            try:
                                await event_bus.emit("group.bot_added", payload)
                            except Exception:
                                pass
                            # Enhanced welcome with TON link + optional QR
                            try:
                                chat_id = str(getattr(chat, 'id', ''))
                                ga_cfg = (self.config or {}).get('group_activation', {}) or {}
                                min_ton = float(ga_cfg.get('min_activation_ton') or 0.001)
                                ton_cfg = (self.config or {}).get('crypto', {}).get('ton', {}) or {}
                                to_addr = str(ton_cfg.get('recipient') or '')
                                product_tag = f"group:{chat_id}"
                                if to_addr:
                                    nano = int(round(min_ton * 1_000_000_000))
                                    from urllib.parse import quote_plus
                                    note = quote_plus(product_tag)
                                    ton_uri = f"ton://transfer/{to_addr}?amount={nano}&text={note}"
                                    tonhub = f"https://tonhub.com/transfer/{to_addr}?amount={nano}&text={note}"
                                    if qrcode and BytesIO:
                                        try:
                                            img = qrcode.make(tonhub)
                                            bio = BytesIO()
                                            img.save(bio, format="PNG")
                                            photo_bytes = bio.getvalue()
                                            await self.send_photo(chat_id=chat_id, photo_bytes=photo_bytes, caption=f"Scan to activate: {min_ton} TON")
                                        except Exception:
                                            pass
                                    await self.send_message(
                                        chat_id=chat_id,
                                        text=(
                                            "Thanks for adding me!\n"
                                            f"Group chat_id: {chat_id}\n\n"
                                            "Activation required. Pay TON to activate/top-up or use /activate <code>.\n"
                                            f"Min: {min_ton} TON\n"
                                            f"Tag: {product_tag}\n\n"
                                            f"Open in wallet: {ton_uri}\n"
                                            f"Web link: {tonhub}\n\n"
                                            f"After payment, an admin runs: /ton_check <tx_hash> {product_tag}"
                                        ),
                                    )
                                else:
                                    await self.send_message(
                                        chat_id=chat_id,
                                        text=(
                                            "Thanks for adding me!\n"
                                            f"Group chat_id: {chat_id}\n\n"
                                            "Activation required: Admins can pay with /ga_invoice or use /activate <code>.\n"
                                            "After paying, run /ton_check <tx_hash> group:<chat_id>."
                                        ).replace("<chat_id>", chat_id),
                                    )
                            except Exception:
                                pass
                        elif str(new_status) in {"kicked", "left"} and str(old_status) in {"member", "administrator"}:
                            payload = {
                                "chat_id": str(getattr(chat, 'id', '')),
                                "title": getattr(chat, 'title', None),
                                "type": getattr(chat, 'type', None),
                            }
                            try:
                                await event_bus.emit("group.bot_removed", payload)
                            except Exception:
                                pass
                except Exception:
                    pass
        except Exception:
            # aiogram types not available — skip group membership handling
            pass

        # Some clients signal bot join via new_chat_members in a message
        @self.router.message(F.new_chat_members)
        async def on_new_members(message: Message):
            try:
                me = await self.bot.get_me()
                bot_id = getattr(me, 'id', None)
                new_members = getattr(message, 'new_chat_members', []) or []
                if bot_id and any(getattr(u, 'id', None) == bot_id for u in new_members):
                    payload = {
                        "chat_id": str(message.chat.id),
                        "title": getattr(message.chat, 'title', None),
                        "type": getattr(message.chat, 'type', None),
                        "inviter_user_id": str(message.from_user.id) if message.from_user else None,
                    }
                    try:
                        await event_bus.emit("group.bot_added", payload)
                    except Exception:
                        pass
                    # Enhanced welcome with TON link + optional QR
                    try:
                        chat_id = str(message.chat.id)
                        ga_cfg = (self.config or {}).get('group_activation', {}) or {}
                        min_ton = float(ga_cfg.get('min_activation_ton') or 0.001)
                        ton_cfg = (self.config or {}).get('crypto', {}).get('ton', {}) or {}
                        to_addr = str(ton_cfg.get('recipient') or '')
                        product_tag = f"group:{chat_id}"
                        if to_addr:
                            nano = int(round(min_ton * 1_000_000_000))
                            from urllib.parse import quote_plus
                            note = quote_plus(product_tag)
                            ton_uri = f"ton://transfer/{to_addr}?amount={nano}&text={note}"
                            tonhub = f"https://tonhub.com/transfer/{to_addr}?amount={nano}&text={note}"
                            # Try QR
                            if qrcode and BytesIO:
                                try:
                                    img = qrcode.make(tonhub)
                                    bio = BytesIO()
                                    img.save(bio, format="PNG")
                                    photo_bytes = bio.getvalue()
                                    await self.send_photo(chat_id=chat_id, photo_bytes=photo_bytes, caption=f"Scan to activate: {min_ton} TON")
                                except Exception:
                                    pass
                            await self.send_message(
                                chat_id=chat_id,
                                text=(
                                    "Thanks for adding me!\n"
                                    f"Group chat_id: {chat_id}\n\n"
                                    "Activation required. Pay TON to activate/top-up or use /activate <code>.\n"
                                    f"Min: {min_ton} TON\n"
                                    f"Tag: {product_tag}\n\n"
                                    f"Open in wallet: {ton_uri}\n"
                                    f"Web link: {tonhub}\n\n"
                                    f"After payment, an admin runs: /ton_check <tx_hash> {product_tag}"
                                ),
                            )
                        else:
                            await self.send_message(
                                chat_id=chat_id,
                                text=(
                                    "Thanks for adding me!\n"
                                    f"Group chat_id: {chat_id}\n\n"
                                    "Activation required: Admins can pay with /ga_invoice or use /activate <code>.\n"
                                    "After paying, run /ton_check <tx_hash> group:<chat_id>."
                                ).replace("<chat_id>", chat_id),
                            )
                    except Exception:
                        pass
            except Exception:
                pass

    async def start(self):
        """Start the Telegram bot"""
        if self.running:
            return

        self.running = True
        # Apply command menu (best-effort)
        await self._apply_bot_menu()
        if self.mode == 'webhook':
            listen = self.webhook_cfg.get('listen', '0.0.0.0')
            port = int(self.webhook_cfg.get('port', 8080))
            secret_token = self.webhook_cfg.get('secret_token')
            url = self.webhook_cfg.get('url')
            path = self.webhook_cfg.get('path', '/telegram')
            if not url:
                logger.error("Webhook mode requires telegram.webhook.url in config")
                raise ValueError("telegram.webhook.url is required for webhook mode")

            logger.info("Setting webhook to %s", url)
            await self.bot.set_webhook(url=url, secret_token=secret_token, drop_pending_updates=False)

            logger.info("Starting webhook server listen=%s port=%s path=%s", listen, port, path)
            app = web.Application()
            webhook_handler = SimpleRequestHandler(dispatcher=self.dp, bot=self.bot, secret_token=secret_token)
            webhook_handler.register(app, path)

            self._web_runner = web.AppRunner(app)
            await self._web_runner.setup()
            site = web.TCPSite(self._web_runner, listen, port)

            async def _run_webhook():
                await site.start()
                while self.running:
                    await asyncio.sleep(1)

            self._bg_task = asyncio.create_task(_run_webhook())
            logger.info("Telegram bot started (webhook)")
        else:
            logger.info("Starting polling...")

            async def _run_polling():
                await self.dp.start_polling(self.bot)

            self._bg_task = asyncio.create_task(_run_polling())
            logger.info("Telegram bot started (polling)")

    async def get_file_download_url(self, file_id: str) -> Optional[str]:
        """
        Return a direct HTTPS URL to download a Telegram file for the given file_id.
        The URL is valid without extra auth because the bot token is embedded.
        """
        try:
            f = await self.bot.get_file(file_id)
            file_path = getattr(f, 'file_path', None)
            if not file_path:
                return None
            return f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        except Exception:
            return None

    async def stop(self):
        """Stop the Telegram bot"""
        if not self.running:
            return

        logger.info("Stopping Telegram bot...")
        self.running = False

        # Stop background task
        if self._bg_task:
            self._bg_task.cancel()
            try:
                await self._bg_task
            except asyncio.CancelledError:
                pass
            self._bg_task = None

        # Teardown webhook server
        if self._web_runner:
            try:
                await self._web_runner.cleanup()
            except Exception:
                pass
            self._web_runner = None

        # Remove webhook (so polling could be used next time)
        try:
            await self.bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            pass

        logger.info("Telegram bot stopped")

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to_message_id: Optional[str] = None,
        **kwargs
    ) -> Any:
        """Send a message to a Telegram chat"""
        parse_mode = kwargs.get("parse_mode")
        disable_notification = kwargs.get("disable_notification")
        reply_markup = kwargs.get("reply_markup")
        # Support simple keyboard schema via 'keyboard' or 'remove_keyboard'
        keyboard = kwargs.get("keyboard")
        remove_keyboard = kwargs.get("remove_keyboard")
        inline_buttons = kwargs.get("inline_buttons")  # List[List[(text,url)|{text,callback_data}]]
        if remove_keyboard:
            reply_markup = ReplyKeyboardRemove()
        elif keyboard and not reply_markup:
            # keyboard: List[List[str]]
            rows = []
            for row in keyboard:
                rows.append([KeyboardButton(text=str(btn)) for btn in row])
            reply_markup = ReplyKeyboardMarkup(
                keyboard=rows,
                resize_keyboard=self.kb_resize,
                one_time_keyboard=self.kb_one_time,
            )
        # Inline URL/WebApp buttons
        if inline_buttons and not reply_markup:
            rows = []
            for row in inline_buttons:
                btn_row = []
                for btn in row:
                    if isinstance(btn, (list, tuple)) and len(btn) >= 2:
                        text_b, url_b = btn[0], btn[1]
                        btn_row.append(InlineKeyboardButton(text=str(text_b), url=str(url_b)))
                    elif isinstance(btn, dict):
                        text_b = btn.get("text")
                        url_b = btn.get("url")
                        cb = btn.get("callback_data")
                        wurl = btn.get("web_app_url")
                        if text_b and cb:
                            btn_row.append(InlineKeyboardButton(text=str(text_b), callback_data=str(cb)))
                        elif text_b and url_b:
                            btn_row.append(InlineKeyboardButton(text=str(text_b), url=str(url_b)))
                        elif text_b and wurl:
                            try:
                                btn_row.append(InlineKeyboardButton(text=str(text_b), web_app=WebAppInfo(url=str(wurl))))
                            except Exception:
                                # Fallback to normal URL if WebAppInfo unavailable
                                btn_row.append(InlineKeyboardButton(text=str(text_b), url=str(wurl)))
                if btn_row:
                    rows.append(btn_row)
            if rows:
                reply_markup = InlineKeyboardMarkup(inline_keyboard=rows)
        # Photo support: if photo provided, send as photo with caption instead of text
        photo_bytes = kwargs.get("photo_bytes")
        photo_path = kwargs.get("photo_path")
        caption = kwargs.get("caption") or text
        if photo_bytes or photo_path:
            try:
                from aiogram.types import BufferedInputFile, FSInputFile
                if photo_bytes:
                    photo = BufferedInputFile(photo_bytes, filename="qr.png")
                else:
                    photo = FSInputFile(photo_path)
                result = await self.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption,
                    parse_mode=parse_mode,
                    disable_notification=disable_notification,
                    reply_markup=reply_markup,
                )
                # Analytics: outbound photo with caption bytes
                try:
                    out_bytes = len((caption or "").encode("utf-8")) if caption else 0
                    await analytics.log_message(
                        platform="telegram",
                        chat_id=str(chat_id),
                        user_id=None,
                        direction="out",
                        bytes_count=out_bytes,
                        message_type="photo",
                    )
                except Exception:
                    pass
                return result
            except Exception:
                # fallback to text if photo send fails
                pass
        result = await self.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_to_message_id=reply_to_message_id,
            parse_mode=parse_mode,
            disable_notification=disable_notification,
            reply_markup=reply_markup,
        )
        # Analytics: outbound text
        try:
            out_bytes = len((text or "").encode("utf-8")) if text else 0
            await analytics.log_message(
                platform="telegram",
                chat_id=str(chat_id),
                user_id=None,
                direction="out",
                bytes_count=out_bytes,
                message_type="text",
            )
        except Exception:
            pass
        return result

    async def send_animation(self, chat_id: str, animation_bytes: bytes, caption: Optional[str] = None, **kwargs) -> Any:
        """Send an animation (GIF/MP4) from in-memory bytes and return the Message."""
        disable_notification = kwargs.get("disable_notification")
        input_file = BufferedInputFile(animation_bytes, filename="animation.gif")
        return await self.bot.send_animation(chat_id=chat_id, animation=input_file, caption=caption, disable_notification=disable_notification)

    async def edit_message_animation(self, chat_id: str, message_id: str, animation_bytes: bytes, caption: Optional[str] = None) -> Any:
        """Edit a message's media with a new animation from bytes."""
        from aiogram.types import InputMediaAnimation
        input_file = BufferedInputFile(animation_bytes, filename="animation.gif")
        media = InputMediaAnimation(media=input_file, caption=caption)
        return await self.bot.edit_message_media(chat_id=chat_id, message_id=int(message_id), media=media)

    async def send_photo(self, chat_id: str, photo_bytes: bytes, caption: Optional[str] = None, **kwargs) -> Any:
        """Send a photo from in-memory bytes and return the Message."""
        disable_notification = kwargs.get("disable_notification")
        input_file = BufferedInputFile(photo_bytes, filename="image.png")
        return await self.bot.send_photo(chat_id=chat_id, photo=input_file, caption=caption, disable_notification=disable_notification)

    async def edit_message_media(self, chat_id: str, message_id: str, photo_bytes: bytes, caption: Optional[str] = None) -> Any:
        """Edit a message's media with a new photo from bytes."""
        input_file = BufferedInputFile(photo_bytes, filename="image.png")
        media = InputMediaPhoto(media=input_file, caption=caption)
        return await self.bot.edit_message_media(chat_id=chat_id, message_id=int(message_id), media=media)

    async def edit_message_text(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        **kwargs,
    ) -> Any:
        """Edit a previously sent Telegram message"""
        parse_mode = kwargs.get("parse_mode")
        reply_markup = kwargs.get("reply_markup")
        inline_buttons = kwargs.get("inline_buttons")
        # Only inline keyboards are valid for edits
        if inline_buttons and not reply_markup:
            rows = []
            for row in inline_buttons:
                btn_row = []
                for btn in row:
                    if isinstance(btn, (list, tuple)) and len(btn) >= 2:
                        text_b, url_b = btn[0], btn[1]
                        btn_row.append(InlineKeyboardButton(text=str(text_b), url=str(url_b)))
                    elif isinstance(btn, dict):
                        text_b = btn.get("text")
                        url_b = btn.get("url")
                        cb = btn.get("callback_data")
                        if text_b and cb:
                            btn_row.append(InlineKeyboardButton(text=str(text_b), callback_data=str(cb)))
                        elif text_b and url_b:
                            btn_row.append(InlineKeyboardButton(text=str(text_b), url=str(url_b)))
                if btn_row:
                    rows.append(btn_row)
            if rows:
                reply_markup = InlineKeyboardMarkup(inline_keyboard=rows)
        return await self.bot.edit_message_text(
            chat_id=chat_id,
            message_id=int(message_id),
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
        )

    async def send_invoice(
        self,
        chat_id: str,
        title: str,
        description: str,
        payload: str,
        currency: str,
        prices: Any,
        **kwargs,
    ) -> Any:
        """Send a Telegram invoice.

        Args:
            chat_id: target chat id
            title: product title
            description: product description
            payload: opaque payload for reconciliation (e.g., product_id,user_id)
            currency: ISO 4217 or special code like XTR
            prices: list of LabeledPrice or list of {label, amount}
            **kwargs: optional params, e.g., provider_token override
        """
        try:
            from aiogram.types import LabeledPrice
        except Exception:
            LabeledPrice = None  # type: ignore

        # Normalize prices
        norm_prices = []
        if isinstance(prices, (list, tuple)):
            for p in prices:
                if LabeledPrice and hasattr(p, 'label') and hasattr(p, 'amount'):
                    norm_prices.append(p)
                elif isinstance(p, dict):
                    label = str(p.get('label', 'Item'))
                    amount = int(p.get('amount', 0))
                    if LabeledPrice:
                        try:
                            norm_prices.append(LabeledPrice(label=label, amount=amount))
                        except Exception:
                            pass
                    else:
                        # Fallback: aiogram should still accept dicts in some versions, keep as-is
                        norm_prices.append({'label': label, 'amount': amount})

        provider_token = kwargs.get('provider_token') or self.provider_token
        call_args = dict(
            chat_id=chat_id,
            title=title,
            description=description,
            payload=payload,
            currency=currency,
            prices=norm_prices,
            **{k: v for k, v in kwargs.items() if k not in {'provider_token'}}
        )
        # For Stars (XTR), Telegram does not require provider_token; omit if not set
        if provider_token:
            call_args['provider_token'] = provider_token
        return await self.bot.send_invoice(**call_args)

    async def send_chat_action(self, chat_id: str, action: str) -> None:
        """Send a chat action like 'typing'"""
        # Map generic action names to Telegram ChatAction
        action_map = {
            "typing": "typing",
            "upload_photo": "upload_photo",
            "record_video": "record_video",
            "upload_video": "upload_video",
            "record_voice": "record_voice",
            "upload_voice": "upload_voice",
            "upload_document": "upload_document",
            "choose_sticker": "choose_sticker",
            "find_location": "find_location",
        }
        tg_action = action_map.get(action, "typing")
        await self.bot.send_chat_action(chat_id=chat_id, action=tg_action)

    async def _on_text(self, message: Message) -> None:
        # Log and reply for visibility
        try:
            logger.info(
                "Text message: chat_id=%s user=%s text=%r",
                message.chat.id,
                message.from_user.username if message.from_user else None,
                message.text,
            )
        except Exception:
            pass

        # Analytics: inbound text
        try:
            bytes_count = len((message.text or "").encode("utf-8")) if getattr(message, 'text', None) else 0
            await analytics.log_message(
                platform="telegram",
                chat_id=str(message.chat.id),
                user_id=str(message.from_user.id) if message.from_user else None,
                direction="in",
                bytes_count=bytes_count,
                message_type="text",
            )
        except Exception:
            pass

        core_message = self._to_core_message(message)
        await self._handle_message(core_message)

    async def _on_command(self, message: Message) -> None:
        try:
            logger.info(
                "Command: chat_id=%s user=%s command=%r",
                message.chat.id,
                message.from_user.username if message.from_user else None,
                message.text,
            )
        except Exception:
            pass

        # Analytics: commands count as inbound text
        try:
            bytes_count = len((message.text or "").encode("utf-8")) if getattr(message, 'text', None) else 0
            await analytics.log_message(
                platform="telegram",
                chat_id=str(message.chat.id),
                user_id=str(message.from_user.id) if message.from_user else None,
                direction="in",
                bytes_count=bytes_count,
                message_type="text",
            )
        except Exception:
            pass

        core_message = self._to_core_message(message)
        await self._handle_message(core_message)

    def _to_core_message(self, tg_message: Message) -> CoreMessage:
        """Convert aiogram Message to core.message.Message"""
        from_user = tg_message.from_user
        chat = tg_message.chat

        # Determine message type
        message_type = MessageType.TEXT
        if getattr(tg_message, 'photo', None):
            message_type = MessageType.IMAGE
        elif getattr(tg_message, 'document', None):
            message_type = MessageType.DOCUMENT
        elif getattr(tg_message, 'video', None):
            message_type = MessageType.VIDEO
        elif getattr(tg_message, 'audio', None):
            message_type = MessageType.AUDIO
        elif getattr(tg_message, 'sticker', None):
            message_type = MessageType.STICKER
        elif getattr(tg_message, 'location', None):
            message_type = MessageType.LOCATION
        elif getattr(tg_message, 'contact', None):
            message_type = MessageType.CONTACT
        elif getattr(tg_message, 'voice', None):
            # Treat voice messages as audio in our core model
            message_type = MessageType.AUDIO

        # Entities -> list of dicts
        entities = []
        try:
            if tg_message.entities:
                entities = [e.model_dump() for e in tg_message.entities]
        except Exception:
            entities = []

        raw_dict = tg_message.model_dump() if hasattr(tg_message, 'model_dump') else {}

        # Choose a useful textual content for non-text types
        content_text = tg_message.text or tg_message.caption or ""
        try:
            if message_type == MessageType.LOCATION and getattr(tg_message, 'location', None):
                loc = tg_message.location
                content_text = f"{loc.latitude},{loc.longitude}"
            elif message_type == MessageType.CONTACT and getattr(tg_message, 'contact', None):
                c = tg_message.contact
                phone = getattr(c, 'phone_number', '')
                name = ((getattr(c, 'first_name', '') or '') + ' ' + (getattr(c, 'last_name', '') or '')).strip()
                content_text = f"{name} {phone}".strip()
            elif message_type in (MessageType.IMAGE, MessageType.DOCUMENT, MessageType.VIDEO, MessageType.AUDIO, MessageType.STICKER):
                # store primary file_id in content for downstream processors
                file_id = None
                if message_type == MessageType.IMAGE and getattr(tg_message, 'photo', None):
                    # photo is a list (smallest..largest); use last
                    try:
                        file_id = tg_message.photo[-1].file_id
                    except Exception:
                        file_id = None
                elif message_type == MessageType.DOCUMENT and getattr(tg_message, 'document', None):
                    file_id = tg_message.document.file_id
                elif message_type == MessageType.VIDEO and getattr(tg_message, 'video', None):
                    file_id = tg_message.video.file_id
                elif message_type == MessageType.AUDIO and getattr(tg_message, 'audio', None):
                    file_id = tg_message.audio.file_id
                elif message_type == MessageType.AUDIO and getattr(tg_message, 'voice', None):
                    file_id = tg_message.voice.file_id
                elif message_type == MessageType.STICKER and getattr(tg_message, 'sticker', None):
                    file_id = tg_message.sticker.file_id
                if file_id:
                    content_text = file_id
        except Exception:
            pass

        return CoreMessage(
            message_id=str(tg_message.message_id),
            from_user=User(
                id=str(from_user.id) if from_user else "",
                username=(from_user.username or "") if from_user else "",
                first_name=(from_user.first_name or "") if from_user else "",
                last_name=(from_user.last_name or "") if from_user else "",
                is_bot=(from_user.is_bot if from_user else False),
                language_code=(from_user.language_code if from_user else None),
            ),
            chat=Chat(
                id=str(chat.id),
                type=str(chat.type),
                title=getattr(chat, 'title', None),
                username=getattr(chat, 'username', None),
                first_name=getattr(chat, 'first_name', None),
                last_name=getattr(chat, 'last_name', None),
            ),
            date=tg_message.date,
            message_type=message_type,
            content=content_text,
            raw_data=raw_dict,
            reply_to_message=(self._to_core_message(tg_message.reply_to_message) if getattr(tg_message, 'reply_to_message', None) else None),
            entities=entities,
        )
