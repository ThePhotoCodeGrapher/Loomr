from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Type, List
import logging
from .message import Message


class MessageHandler(ABC):
    @abstractmethod
    async def handle(self, message: Message, service: "MessagingService") -> bool:
        """
        Handle an incoming message.
        
        Args:
            message: The incoming message
            service: The messaging service that received the message
            
        Returns:
            bool: True if the message was handled, False otherwise
        """
        pass


class MessagingService(ABC):
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.handlers: List[MessageHandler] = []
        self.running = False
        # child classes should configure logging; we still define a logger here
        self._logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    async def start(self):
        """Start the messaging service"""
        pass

    @abstractmethod
    async def stop(self):
        """Stop the messaging service"""
        pass

    @abstractmethod
    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_to_message_id: Optional[str] = None,
        **kwargs
    ) -> Any:
        """
        Send a message to a chat
        
        Args:
            chat_id: The chat ID to send the message to
            text: The message text
            reply_to_message_id: Optional message ID to reply to
            **kwargs: Additional parameters specific to the messaging service
            
        Returns:
            The sent message or result
        """
        pass

    @abstractmethod
    async def edit_message_text(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        **kwargs,
    ) -> Any:
        """Edit a previously sent message (if supported by the platform)."""
        pass

    @abstractmethod
    async def send_chat_action(self, chat_id: str, action: str) -> None:
        """Send a chat action like 'typing' (if supported)."""
        pass

    def add_handler(self, handler: MessageHandler):
        """
        Add a message handler
        
        Args:
            handler: The handler to add
        """
        self.handlers.append(handler)

    async def _handle_message(self, message: Message) -> bool:
        """
        Process a message through all registered handlers
        
        Args:
            message: The message to process
            
        Returns:
            bool: True if any handler processed the message
        """
        self._logger.info(
            "Dispatching message: chat_id=%s user_id=%s text=%r handlers=%d",
            getattr(message.chat, "id", None),
            getattr(message.from_user, "id", None),
            getattr(message, "content", None),
            len(self.handlers),
        )

        for handler in self.handlers:
            name = handler.__class__.__name__
            try:
                handled = await handler.handle(message, self)
                self._logger.info("Handler %s -> %s", name, handled)
                if handled:
                    return True
            except Exception as e:
                self._logger.exception("Handler %s raised: %s", name, e)
        return False
