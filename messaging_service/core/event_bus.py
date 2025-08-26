import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class EventHandlerSpec:
    topic: str
    action: str
    filters: Optional[Dict[str, Any]] = None
    params: Optional[Dict[str, Any]] = None


class EventBus:
    """
    Minimal in-process event bus with YAML-configurable handlers.

    - Handlers are dicts with: topic, action, filters?, params?
    - Actions supported: send_message, http_request, shell
    - A send func can be provided by the app for send_message.
    """

    def __init__(self) -> None:
        self._handlers: List[EventHandlerSpec] = []
        self._send_func: Optional[Callable[[str, str], Awaitable[None]]] = None

    def set_send_message_func(self, fn: Callable[[str, str], Awaitable[None]]) -> None:
        self._send_func = fn

    def register_handlers(self, items: List[Dict[str, Any]]) -> None:
        self._handlers = []
        for it in items or []:
            try:
                self._handlers.append(
                    EventHandlerSpec(
                        topic=str(it.get("topic")),
                        action=str(it.get("action")),
                        filters=it.get("filters") or {},
                        params=it.get("params") or {},
                    )
                )
            except Exception as e:
                logger.warning(f"Invalid event handler spec skipped: {it} ({e})")

    async def emit(self, topic: str, payload: Dict[str, Any]) -> None:
        """Emit an event and run matching handlers asynchronously (fire-and-forget)."""
        if not self._handlers:
            return
        pending: List[Awaitable[Any]] = []
        for h in self._handlers:
            if h.topic != topic:
                continue
            if not self._match_filters(h.filters or {}, payload):
                continue
            pending.append(self._execute_action(h.action, h.params or {}, payload))
        if pending:
            # run concurrently; don't raise to caller
            try:
                await asyncio.gather(*pending, return_exceptions=True)
            except Exception:
                pass

    def emit_sync(self, topic: str, payload: Dict[str, Any]) -> None:
        """Schedule an async emit from sync code paths."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            asyncio.create_task(self.emit(topic, payload))
        else:
            # in non-async contexts, run a new loop
            asyncio.run(self.emit(topic, payload))

    def _match_filters(self, flt: Dict[str, Any], payload: Dict[str, Any]) -> bool:
        for k, v in (flt or {}).items():
            if str(payload.get(k)) != str(v):
                return False
        return True

    async def _execute_action(self, action: str, params: Dict[str, Any], payload: Dict[str, Any]) -> None:
        try:
            if action == "send_message":
                await self._act_send_message(params, payload)
            elif action == "http_request":
                await self._act_http_request(params, payload)
            elif action == "shell":
                await self._act_shell(params, payload)
            else:
                logger.warning(f"Unknown action: {action}")
        except Exception as e:
            logger.error(f"Event action '{action}' failed: {e}")

    async def _act_send_message(self, params: Dict[str, Any], payload: Dict[str, Any]) -> None:
        if not self._send_func:
            logger.warning("send_message action ignored: no send function configured")
            return
        to = str(params.get("to") or payload.get("chat_id") or "").strip()
        template = str(params.get("template") or "{json}")
        text = template.format(**{**payload, "json": json.dumps(payload)})
        if to:
            await self._send_func(to, text)
        else:
            # fallback: if no explicit target, try payload.chat_id
            cid = str(payload.get("chat_id") or "").strip()
            if cid:
                await self._send_func(cid, text)

    async def _act_http_request(self, params: Dict[str, Any], payload: Dict[str, Any]) -> None:
        from aiohttp import ClientSession
        method = str(params.get("method") or "POST").upper()
        url = str(params.get("url") or "")
        headers = params.get("headers") or {}
        body_tmpl = str(params.get("body_template") or "{}")
        if not url:
            return
        data_str = body_tmpl.format(**{**payload, "json": json.dumps(payload)})
        async with ClientSession() as sess:
            if method == "GET":
                async with sess.get(url, headers=headers, params=json.loads(data_str)) as resp:
                    await resp.text()
            else:
                async with sess.request(method, url, headers=headers, data=data_str) as resp:
                    await resp.text()

    async def _act_shell(self, params: Dict[str, Any], payload: Dict[str, Any]) -> None:
        import asyncio as aio
        cmd_tmpl = str(params.get("command") or "").strip()
        if not cmd_tmpl:
            return
        cmd = cmd_tmpl.format(**{**payload, "json": json.dumps(payload)})
        proc = await aio.create_subprocess_shell(cmd, stdout=aio.subprocess.PIPE, stderr=aio.subprocess.PIPE)
        await proc.communicate()


# Global singleton
bus = EventBus()
