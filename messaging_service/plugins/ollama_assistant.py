import os
import asyncio
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp

from core.message import Message
from core.service import MessageHandler, MessagingService


class OllamaAssistant(MessageHandler):
    """
    Simple local LLM assistant using Ollama.

    Usage in Telegram: /ask <your question>
    Reads a system prompt (questionary) from config path to focus answers.
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.host: str = cfg.get("host") or os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
        self.model: str = cfg.get("model") or os.getenv("OLLAMA_MODEL", "llama3:8b")
        qpath = cfg.get("questionary_path") or os.getenv(
            "QUESTIONARY_PATH", "messaging_service/config/questionary.md"
        )
        self.system_text: str = self._load_questionary(qpath)

    def _load_questionary(self, path: str) -> str:
        try:
            p = (Path(__file__).parent.parent / path).resolve() if not Path(path).is_absolute() else Path(path)
            if p.exists():
                return p.read_text(encoding="utf-8")
        except Exception:
            pass
        return (
            "You are Loomr's local assistant. Be concise. Answer only within the provided domain."
        )

    async def handle(self, message: Message, service: MessagingService) -> bool:
        text = (message.content or "").strip()
        if not text.startswith("/ask"):
            return False

        prompt = text[4:].strip()
        if not prompt:
            await service.send_message(
                chat_id=message.chat.id,
                text="Usage: /ask <your question>",
                reply_to_message_id=message.message_id,
            )
            return True

        await service.send_chat_action(chat_id=message.chat.id, action="typing")
        try:
            answer = await self._chat_ollama(self.system_text, prompt)
        except Exception as e:
            answer = f"Error talking to local model: {e}"

        await service.send_message(
            chat_id=message.chat.id,
            text=answer.strip()[:4000],
            reply_to_message_id=message.message_id,
        )
        return True

    async def _chat_ollama(self, system_text: str, user_text: str) -> str:
        url = f"{self.host.rstrip('/')}/api/chat"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
            "stream": False,
        }
        timeout = aiohttp.ClientTimeout(total=180)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                resp.raise_for_status()
                data = await resp.json()
                # Newer ollama returns {message: {content: str}}
                msg = data.get("message") or {}
                content = msg.get("content")
                if not content:
                    # Fallback if response schema differs
                    content = data.get("response") or ""
                return content or "(empty response)"
