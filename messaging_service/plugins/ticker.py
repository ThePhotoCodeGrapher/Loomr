import asyncio
import io
import time
import threading
import json
from typing import Dict, Optional

from core.message import Message as CoreMessage
from core.service import MessageHandler, MessagingService

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    Image = None

try:
    import cairosvg  # type: ignore
except Exception:
    cairosvg = None

try:
    import paho.mqtt.client as mqtt  # type: ignore
except Exception:
    mqtt = None


class TickerPlugin(MessageHandler):
    """
    Provides a simple ticker demo:
      - /ticker start → sends an initial image and then edits it ~1s interval
      - /ticker stop  → stops updating
    """
    def __init__(self, config: Dict):
        self._tasks: Dict[str, asyncio.Task] = {}
        self._message_ids: Dict[str, str] = {}
        self.interval: float = float((config or {}).get("interval", 1.0))
        self.size = (640, 480)
        # MQTT config
        cfg = config or {}
        self.ticker_id: str = str(cfg.get("id") or "default")
        self.mqtt_cfg: Dict = cfg.get("mqtt") or {}
        self._mqtt_client: Optional["mqtt.Client"] = None
        self._mqtt_thread: Optional[threading.Thread] = None
        self._mqtt_running = threading.Event()
        # Queue to deliver incoming MQTT payloads into asyncio loop
        self._queue: "asyncio.Queue[bytes]" = asyncio.Queue()

    async def handle(self, message: CoreMessage, service: MessagingService) -> bool:
        text = (message.content or "").strip()
        if not text.startswith("/ticker"):
            return False
        parts = text.split()
        action = parts[1] if len(parts) > 1 else ""
        chat_id = message.chat.id

        if action == "start":
            await self._handle_start(chat_id, service)
            return True
        if action == "demo":
            # Start and auto-stop after 10 seconds
            await self._handle_start(chat_id, service)
            asyncio.create_task(self._auto_stop(chat_id, service, after_seconds=10))
            return True
        if action == "stop":
            await self._handle_stop(chat_id, service)
            return True

        await service.send_message(chat_id, "Usage: /ticker start | /ticker demo | /ticker stop")
        return True

    async def _handle_start(self, chat_id: str, service) -> None:
        if chat_id in self._tasks and not self._tasks[chat_id].done():
            await service.send_message(chat_id, "Ticker already running. Use /ticker stop to stop.")
            return
        # Ensure MQTT is running (best-effort)
        await self._ensure_mqtt_started(service)
        # Send initial frame or waiting message
        if Image is None:
            sent = await service.send_message(chat_id, "Ticker started. Waiting for MQTT data...")
            self._message_ids[chat_id] = str(getattr(sent, "message_id", ""))
        else:
            frame = self._render_frame(counter=0)
            sent = await service.send_photo(chat_id, frame, caption="Ticker started. Waiting for MQTT data...")
            self._message_ids[chat_id] = str(getattr(sent, "message_id", ""))

        # Launch background updater to process MQTT -> GIF updates, and fallback live ticks between updates
        task = asyncio.create_task(self._run_ticker(chat_id, service))
        self._tasks[chat_id] = task

    async def _handle_stop(self, chat_id: str, service) -> None:
        task = self._tasks.get(chat_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.pop(chat_id, None)
        self._message_ids.pop(chat_id, None)
        await service.send_message(chat_id, "Ticker stopped.")

    async def _run_ticker(self, chat_id: str, service: MessagingService) -> None:
        start_t = time.time()
        counter = 0
        try:
            while True:
                # Prefer MQTT-driven SVG payloads
                try:
                    payload: bytes = await asyncio.wait_for(self._queue.get(), timeout=self.interval)
                    gif = self._svg_payload_to_gif(payload)
                    if gif is not None:
                        msg_id = self._message_ids.get(chat_id)
                        if msg_id:
                            try:
                                await service.edit_message_animation(chat_id, msg_id, gif, caption="live")
                            except Exception:
                                sent = await service.send_animation(chat_id, gif, caption="live")
                                self._message_ids[chat_id] = str(getattr(sent, "message_id", ""))
                        else:
                            sent = await service.send_animation(chat_id, gif, caption="live")
                            self._message_ids[chat_id] = str(getattr(sent, "message_id", ""))
                        continue
                except asyncio.TimeoutError:
                    # No MQTT data in this interval; render fallback PNG tick
                    pass

                # Fallback progress PNG to show liveness
                counter += 1
                frame = self._render_frame(counter, start_t)
                if frame is None:
                    await asyncio.sleep(self.interval)
                    continue
                msg_id = self._message_ids.get(chat_id)
                if not msg_id:
                    sent = await service.send_photo(chat_id, frame, caption=f"Tick #{counter}")
                    self._message_ids[chat_id] = str(getattr(sent, "message_id", ""))
                    continue
                try:
                    await service.edit_message_media(chat_id, msg_id, frame, caption=f"Tick #{counter}")
                except Exception:
                    try:
                        sent = await service.send_photo(chat_id, frame, caption=f"Tick #{counter}")
                        self._message_ids[chat_id] = str(getattr(sent, "message_id", ""))
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass

    async def _auto_stop(self, chat_id: str, service: MessagingService, after_seconds: int = 10):
        try:
            await asyncio.sleep(max(1, int(after_seconds)))
            await self._handle_stop(chat_id, service)
        except asyncio.CancelledError:
            pass

    async def _ensure_mqtt_started(self, service: MessagingService) -> None:
        if mqtt is None:
            return
        if self._mqtt_client is not None:
            return

        host = str(self.mqtt_cfg.get("host") or "localhost")
        port = int(self.mqtt_cfg.get("port") or 1883)
        topic_tpl = str(self.mqtt_cfg.get("topic") or "ticker/{id}")
        username = self.mqtt_cfg.get("username")
        password = self.mqtt_cfg.get("password")
        topic = topic_tpl.replace("{id}", self.ticker_id)

        client = mqtt.Client()
        if username and password:
            client.username_pw_set(str(username), str(password))

        def on_connect(cl, userdata, flags, rc, properties=None):  # type: ignore
            try:
                cl.subscribe(topic, qos=0)
            except Exception:
                pass

        def on_message(cl, userdata, msg):  # type: ignore
            try:
                payload = msg.payload or b""
                # Forward raw payload as-is to asyncio queue (best effort)
                loop = asyncio.get_event_loop()
                loop.call_soon_threadsafe(self._queue.put_nowait, payload)
            except Exception:
                pass

        client.on_connect = on_connect
        client.on_message = on_message
        self._mqtt_client = client

        def _runner():
            try:
                self._mqtt_running.set()
                client.connect(host, port, keepalive=60)
                client.loop_forever()
            except Exception:
                pass
            finally:
                self._mqtt_running.clear()

        th = threading.Thread(target=_runner, daemon=True)
        self._mqtt_thread = th
        th.start()

    def _svg_payload_to_gif(self, payload: bytes) -> Optional[bytes]:
        """Convert an incoming payload to a 10s animated GIF.
        Accepts either raw SVG (string/bytes) or JSON {"svg":"<svg...>"}.
        """
        if cairosvg is None or Image is None:
            return None
        svg_data: Optional[bytes] = None
        try:
            # Try JSON first
            txt = payload.decode("utf-8", errors="ignore").strip()
            if txt.startswith("{"):
                obj = json.loads(txt)
                svg_s = obj.get("svg") or obj.get("SVG")
                if isinstance(svg_s, str):
                    svg_data = svg_s.encode("utf-8")
            if svg_data is None:
                # Assume raw SVG
                if "<svg" in txt:
                    svg_data = txt.encode("utf-8")
        except Exception:
            return None
        if not svg_data:
            return None

        # Rasterize base PNG from SVG
        try:
            base_png = cairosvg.svg2png(bytestring=svg_data, output_width=self.size[0], output_height=self.size[1])
        except Exception:
            return None
        try:
            base_img = Image.open(io.BytesIO(base_png)).convert("RGBA")
        except Exception:
            return None

        # Build simple 10-second animation (20 frames x 500ms) with a moving highlight bar
        frames = []
        w, h = base_img.size
        steps = 20
        for i in range(steps):
            frame = base_img.copy()
            draw = ImageDraw.Draw(frame)
            # moving bar across width
            bar_w = max(6, w // 30)
            x = int(i / max(1, steps - 1) * (w - bar_w))
            draw.rectangle([x, 0, x + bar_w, h], fill=(255, 255, 255, 40))
            frames.append(frame.convert("P", palette=Image.ADAPTIVE))

        out = io.BytesIO()
        try:
            frames[0].save(
                out,
                format="GIF",
                save_all=True,
                append_images=frames[1:],
                duration=500,  # ms -> 20 * 500 = 10s
                loop=0,
                optimize=True,
                disposal=2,
            )
            return out.getvalue()
        except Exception:
            return None

    def _render_frame(self, counter: int, start_t: Optional[float] = None) -> Optional[bytes]:
        if Image is None:
            return None
        img = Image.new("RGB", self.size, (18, 18, 24))
        draw = ImageDraw.Draw(img)
        w, h = self.size
        # Background bar that grows with counter
        bar_w = int((counter % 100) / 100.0 * (w - 40))
        draw.rectangle([20, h // 2 - 20, 20 + bar_w, h // 2 + 20], fill=(70, 160, 255))
        # Text
        elapsed = 0.0 if start_t is None else max(0.0, time.time() - start_t)
        text = f"Ticker: {counter} | {elapsed:0.1f}s"
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None
        draw.text((20, 20), text, fill=(240, 240, 240), font=font)
        # Encode PNG
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
