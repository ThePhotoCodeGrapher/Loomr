from typing import Dict, Optional

from core.message import Message
from core.service import MessageHandler, MessagingService


class StartRouter(MessageHandler):
    """
    Generic /start deep-link router.
    Payload patterns (case-sensitive):
      - flow:<alias>
      - flow:<alias>:step:<step_id>
      - paid:<sku>               # reserved for payments; can map to a flow
      - product:<sku>            # reserved; reply with link or instructions (future)
    """

    def __init__(self, flow_registry: Dict[str, MessageHandler], default_flow: Optional[str] = None):
        self.flow_registry = flow_registry  # alias -> QuestionnaireHandler
        self.default_flow = default_flow

    async def handle(self, message: Message, service: MessagingService) -> bool:
        text = (message.content or "").strip()
        if not text.startswith("/start"):
            return False

        parts = text.split(maxsplit=1)
        payload = parts[1].strip() if len(parts) > 1 else ""

        # No payload: route to default flow if any
        if not payload:
            if self.default_flow and self.default_flow in self.flow_registry:
                handler = self.flow_registry[self.default_flow]
                # type: ignore - handler exposes start_session when QuestionnaireHandler
                await getattr(handler, "start_session")(message.chat.id, message.from_user.id, service)
                return True
            return False

        # Parse payload tokens split by ':'
        tokens = payload.split(":")
        head = tokens[0]

        if head == "flow" and len(tokens) >= 2:
            alias = tokens[1]
            step: Optional[str] = None
            if len(tokens) >= 4 and tokens[2] == "step":
                step = tokens[3]
            handler = self.flow_registry.get(alias)
            if not handler:
                await service.send_message(chat_id=message.chat.id, text=f"Unknown flow: {alias}")
                return True
            await getattr(handler, "start_session")(message.chat.id, message.from_user.id, service, step=step)
            return True

        if head == "paid" and len(tokens) >= 2:
            sku = tokens[1]
            # For now, just acknowledge; future: map sku -> flow
            await service.send_message(chat_id=message.chat.id, text=f"Payment confirmed for {sku}. Please choose a flow.")
            return True

        if head == "product" and len(tokens) >= 2:
            sku = tokens[1]
            await service.send_message(chat_id=message.chat.id, text=f"To purchase {sku}, please use the provided payment link.")
            return True

        # Unknown payload
        await service.send_message(chat_id=message.chat.id, text="Unsupported start payload.")
        return True
