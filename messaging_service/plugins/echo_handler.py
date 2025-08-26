from typing import Optional
from core.message import Message
from core.service import MessageHandler, MessagingService

class EchoHandler(MessageHandler):
    """A simple echo handler that replies with the same message"""
    
    async def handle(self, message: Message, service: MessagingService) -> bool:
        """Handle incoming message by echoing it back"""
        if not message.content:
            return False
            
        # Only handle text messages that start with /echo
        if not message.content.startswith('/echo '):
            return False
            
        # Remove the /echo command and get the text to echo
        text_to_echo = message.content[6:].strip()
        
        if not text_to_echo:
            await service.send_message(
                chat_id=message.chat.id,
                text="Please provide a message to echo. Usage: /echo <message>"
            )
            return True
            
        # Send the echoed message back to the user
        await service.send_message(
            chat_id=message.chat.id,
            text=f"Echo: {text_to_echo}",
            reply_to_message_id=message.message_id
        )
        return True
