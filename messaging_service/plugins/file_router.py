import json
import asyncio
from typing import Any, Dict, Optional, List, Tuple
from dataclasses import dataclass
import time
from pathlib import Path

from core.message import Message, MessageType
from core.service import MessageHandler, MessagingService
from core.event_bus import bus as event_bus


@dataclass
class FileRouterConfig:
    mode: str = "emit_only"  # emit_only | http
    http: Optional[Dict[str, Any]] = None


class FileRouter(MessageHandler):
    """
    Routes non-text messages (files/media/location/contact) to events and optional HTTP endpoint.

    - Emits events:
      * message.file_received (for image/document/video/audio/sticker)
      * message.location_received
      * message.contact_received
    - Optional HTTP forward when mode=http using config under plugins.file_router.http
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or {}
        self.mode = (cfg.get("mode") or "emit_only").lower()
        self.http_cfg = cfg.get("http") or {}
        # Grouping config
        grp = cfg.get("grouping") or {}
        self.grouping_enabled: bool = bool(grp.get("enabled", True))
        self.group_window_ms: int = int(grp.get("window_ms", 1200))
        self.group_max_items: int = int(grp.get("max_items", 10))
        # media_group_id -> (attachments, timer_task, chat_id, user_id)
        self._groups: Dict[str, Tuple[List[Dict[str, Any]], Optional[asyncio.Task], str, str]] = {}
        # Demo transform config
        demo = cfg.get("demo") or {}
        self.demo_make_gif: bool = bool(demo.get("make_gif", False))
        self.demo_gif_width: int = int(demo.get("gif_width", 512))
        self.demo_frame_ms: int = int(demo.get("frame_ms", 600))
        self.demo_ack_files: bool = bool(demo.get("ack_files", True))
        self.demo_location_footprint: bool = bool(demo.get("location_footprint", True))
        self.demo_contact_echo: bool = bool(demo.get("contact_echo", True))

    async def handle(self, message: Message, service: MessagingService) -> bool:
        mt = message.message_type
        handled = False
        # Grouped media handling via Telegram media_group_id
        media_group_id = None
        try:
            media_group_id = (message.raw_data or {}).get("media_group_id")
        except Exception:
            media_group_id = None

        # keep service reference for demo replies
        self._service_ref = service
        if self.grouping_enabled and media_group_id and mt in (
            MessageType.IMAGE, MessageType.DOCUMENT, MessageType.VIDEO, MessageType.AUDIO
        ):
            await self._process_grouped_file(media_group_id, message, service)
            handled = True
        elif mt in (MessageType.IMAGE, MessageType.DOCUMENT, MessageType.VIDEO, MessageType.AUDIO, MessageType.STICKER):
            await self._process_file(message, service)
            handled = True
        elif mt == MessageType.LOCATION:
            await self._process_location(message, service)
            handled = True
        elif mt == MessageType.CONTACT:
            await self._process_contact(message, service)
            handled = True
        return handled

    async def _demo_send_gif(self, chat_id: str, attachments: List[Dict[str, Any]], service: Optional[MessagingService]) -> None:
        if not service:
            return
        # Only proceed if adapter supports send_animation
        if not hasattr(service, "send_animation"):
            return
        # Download images
        urls = [att.get("download_url") for att in attachments if att.get("download_url")]
        if not urls:
            return
        from io import BytesIO
        import aiohttp
        from PIL import Image
        frames: List[Image.Image] = []
        async with aiohttp.ClientSession() as sess:
            for url in urls:
                try:
                    async with sess.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            img = Image.open(BytesIO(data)).convert("RGB")
                            # resize to target width preserving aspect
                            w = self.demo_gif_width
                            ratio = w / float(img.width)
                            h = max(1, int(img.height * ratio))
                            frames.append(img.resize((w, h)))
                except Exception:
                    continue
        if not frames:
            return
        # Build GIF in-memory
        out = BytesIO()
        try:
            frames[0].save(
                out,
                format="GIF",
                save_all=True,
                append_images=frames[1:] if len(frames) > 1 else None,
                duration=self.demo_frame_ms,
                loop=0,
                disposal=2,
            )
            gif_bytes = out.getvalue()
        finally:
            out.close()
        # Send back
        try:
            await getattr(service, "send_animation")(chat_id=str(chat_id), animation_bytes=gif_bytes, caption="Auto GIF from your album")
        except Exception:
            pass

    async def _process_file(self, message: Message, service: MessagingService) -> None:
        download_url = None
        try:
            if hasattr(service, "get_file_download_url") and message.content:
                download_url = await getattr(service, "get_file_download_url")(message.content)
        except Exception:
            download_url = None
        payload = {
            "chat_id": str(message.chat.id),
            "user_id": str(message.from_user.id),
            "type": message.message_type.value,
            "file_id": message.content,  # adapter puts primary file_id here
            "download_url": download_url,
            "message_id": message.message_id,
        }
        await self._emit_and_forward("message.file_received", payload)
        # Demo acknowledgment for single files
        if self.demo_ack_files:
            try:
                url_info = f"\nURL: {download_url}" if download_url else ""
                txt = (
                    f"Got your {message.message_type.value}.\n"
                    f"file_id: {message.content}{url_info}"
                )
                await service.send_message(chat_id=str(message.chat.id), text=txt, reply_to_message_id=str(message.message_id))
            except Exception:
                pass

    async def _process_grouped_file(self, group_id: str, message: Message, service: MessagingService) -> None:
        """Accumulate album items by media_group_id and flush once after a short window."""
        # Build attachment item
        att = await self._build_attachment_item(message, service)

        # Append to buffer
        attachments, timer_task, chat_id, user_id = self._groups.get(group_id, ([], None, str(message.chat.id), str(message.from_user.id)))
        attachments.append(att)
        # Cap items to avoid runaway
        if len(attachments) >= self.group_max_items:
            # Replace/update buffer then flush immediately
            self._groups[group_id] = (attachments, timer_task, chat_id, user_id)
            await self._flush_group(group_id)
            return

        # Store updated buffer
        # Cancel previous timer if any, then start a new one per window
        if timer_task and not timer_task.done():
            try:
                timer_task.cancel()
            except Exception:
                pass

        async def _timer():
            try:
                await asyncio.sleep(self.group_window_ms / 1000.0)
                await self._flush_group(group_id)
            except asyncio.CancelledError:
                return
            except Exception:
                # Best-effort flush on errors
                try:
                    await self._flush_group(group_id)
                except Exception:
                    pass

        new_timer = asyncio.create_task(_timer())
        self._groups[group_id] = (attachments, new_timer, chat_id, user_id)

    async def _flush_group(self, group_id: str) -> None:
        group = self._groups.pop(group_id, None)
        if not group:
            return
        attachments, timer_task, chat_id, user_id = group
        if timer_task and not timer_task.done():
            try:
                timer_task.cancel()
            except Exception:
                pass
        if not attachments:
            return
        payload = {
            "chat_id": str(chat_id),
            "user_id": str(user_id),
            "type": "group",
            "media_group_id": group_id,
            "count": len(attachments),
            "attachments": attachments,
        }
        await self._emit_and_forward("message.file_group_received", payload)
        # Optional demo processing: build GIF from images and send back
        try:
            if self.demo_make_gif:
                only_images = all((att.get("type") == MessageType.IMAGE.value) for att in attachments)
                if only_images:
                    await self._demo_send_gif(chat_id, attachments, service=self._service_ref)
        except Exception:
            # silent demo failure; do not affect main flow
            pass

    async def _build_attachment_item(self, message: Message, service: MessagingService) -> Dict[str, Any]:
        download_url = None
        try:
            if hasattr(service, "get_file_download_url") and message.content:
                download_url = await getattr(service, "get_file_download_url")(message.content)
        except Exception:
            download_url = None
        # Try to detect mime if present in raw_data
        mime = None
        try:
            doc = (message.raw_data or {}).get("document")
            if doc and isinstance(doc, dict):
                mime = doc.get("mime_type")
        except Exception:
            mime = None
        # Caption support
        caption = None
        try:
            caption = (message.raw_data or {}).get("caption") or None
        except Exception:
            caption = None
        return {
            "type": message.message_type.value,
            "file_id": message.content,
            "download_url": download_url,
            "message_id": message.message_id,
            "mime": mime,
            "caption": caption,
        }

    # Store a weak reference to service for demo replies (set in handle)
    _service_ref: Optional[MessagingService] = None

    async def _process_location(self, message: Message, service: MessagingService) -> None:
        # adapter content is "lat,lon"
        lat, lon = None, None
        try:
            parts = (message.content or "").split(",")
            lat = float(parts[0])
            lon = float(parts[1])
        except Exception:
            pass
        payload = {
            "chat_id": str(message.chat.id),
            "user_id": str(message.from_user.id),
            "type": message.message_type.value,
            "lat": lat,
            "lon": lon,
            "message_id": message.message_id,
        }
        await self._emit_and_forward("message.location_received", payload)
        # Persist last known location for Location Demo plugin
        try:
            if lat is not None and lon is not None:
                base_dir = Path(__file__).parent.parent
                store_path = (base_dir / "data/locations.json").resolve()
                store_path.parent.mkdir(parents=True, exist_ok=True)
                data = {}
                if store_path.exists():
                    try:
                        with open(store_path, "r") as f:
                            data = json.load(f) or {}
                    except Exception:
                        data = {}
                chats = data.get("chats") or {}
                chat_key = str(message.chat.id)
                users = chats.get(chat_key) or {}
                uname = getattr(message.from_user, "username", None)
                first = getattr(message.from_user, "first_name", None)
                last = getattr(message.from_user, "last_name", None)
                users[str(message.from_user.id)] = {
                    "user_id": str(message.from_user.id),
                    "username": uname,
                    "first_name": first,
                    "last_name": last,
                    "lat": lat,
                    "lon": lon,
                    "ts": int(time.time()),
                }
                chats[chat_key] = users
                data["chats"] = chats
                try:
                    with open(store_path, "w") as f:
                        json.dump(data, f, indent=2)
                except Exception:
                    pass
        except Exception:
            pass
        # Demo: leave a digital footprint (send back a maps link)
        if self.demo_location_footprint and lat is not None and lon is not None:
            try:
                maps = f"https://maps.google.com/?q={lat},{lon}"
                txt = (
                    "Location received. Digital footprint saved.\n"
                    f"Lat: {lat}, Lon: {lon}\n{maps}"
                )
                await service.send_message(chat_id=str(message.chat.id), text=txt, reply_to_message_id=str(message.message_id))
            except Exception:
                pass

    async def _process_contact(self, message: Message, service: MessagingService) -> None:
        payload = {
            "chat_id": str(message.chat.id),
            "user_id": str(message.from_user.id),
            "type": message.message_type.value,
            "text": message.content,
            "message_id": message.message_id,
        }
        await self._emit_and_forward("message.contact_received", payload)
        if self.demo_contact_echo:
            try:
                name, phone = None, None
                try:
                    c = (message.raw_data or {}).get("contact") or {}
                    name = (c.get("first_name") or "") + (" " + c.get("last_name") if c.get("last_name") else "")
                    phone = c.get("phone_number")
                except Exception:
                    pass
                details = []
                if name:
                    details.append(f"Name: {name.strip()}")
                if phone:
                    details.append(f"Phone: {phone}")
                detail_txt = ("\n".join(details)) if details else (message.content or "")
                txt = ("Contact received.\n" + detail_txt).strip()
                await service.send_message(chat_id=str(message.chat.id), text=txt, reply_to_message_id=str(message.message_id))
            except Exception:
                pass

    async def _emit_and_forward(self, topic: str, payload: Dict[str, Any]) -> None:
        try:
            await event_bus.emit(topic, payload)
        except Exception:
            pass
        if self.mode == "http":
            await self._forward_http(topic, payload)

    async def _forward_http(self, topic: str, payload: Dict[str, Any]) -> None:
        from aiohttp import ClientSession
        cfg = self.http_cfg or {}
        url = cfg.get("url")
        if not url:
            return
        method = (cfg.get("method") or "POST").upper()
        headers = cfg.get("headers") or {"Content-Type": "application/json"}
        body_template = cfg.get("body_template")
        data = json.dumps(payload) if not body_template else body_template.format(**{**payload, "json": json.dumps(payload)})
        async with ClientSession() as sess:
            try:
                async with sess.request(method, url, headers=headers, data=data) as resp:
                    await resp.text()
            except Exception:
                pass
