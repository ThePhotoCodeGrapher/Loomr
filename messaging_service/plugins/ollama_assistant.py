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
        if not (text.startswith("/ask") or text.startswith("/coach")):
            return False

        if text.startswith("/ask"):
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

        # /coach: analyze a user's reply and suggest a clearer follow-up question
        if text.startswith("/coach"):
            user_reply = text[len("/coach"):].strip()
            if not user_reply:
                await service.send_message(
                    chat_id=message.chat.id,
                    text=(
                        "Usage: /coach <user reply>\n"
                        "I will check if the reply answers the current question and suggest a clearer re-ask."
                    ),
                    reply_to_message_id=message.message_id,
                )
                return True

            await service.send_chat_action(chat_id=message.chat.id, action="typing")
            try:
                coach_text = await self._coach_suggest(self.system_text, user_reply)
            except Exception as e:
                coach_text = f"Coach error: {e}"

            await service.send_message(
                chat_id=message.chat.id,
                text=coach_text.strip()[:4000],
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

    async def _coach_suggest(self, system_text: str, user_reply: str) -> str:
        """
        Ask the model to judge if a reply answers a pending question and suggest a clearer re-ask.
        No persistent memory yet; relies on system prompt as guardrails.
        """
        system = (
            system_text
            + "\n\nYou are a QA coach. Evaluate if the user's reply answers the current question.\n"
              "If not, propose ONE concise, friendly re-ask to elicit the needed info.\n"
              "Respond in this format:\n"
              "- Verdict: valid|invalid\n- Reason: <short>\n- Re-ask: <one sentence question>\n"
              "Use simple language."
        )
        prompt = f"User reply: {user_reply}\nJudge and suggest."
        return await self._chat_ollama(system, prompt)
