from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, Any, Optional, List


class MessageType(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    DOCUMENT = "document"
    AUDIO = "audio"
    VIDEO = "video"
    STICKER = "sticker"
    LOCATION = "location"
    CONTACT = "contact"
    POLL = "poll"


@dataclass
class User:
    id: str
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    is_bot: bool = False
    language_code: Optional[str] = None


@dataclass
class Chat:
    id: str
    type: str  # 'private', 'group', 'supergroup', 'channel'
    title: Optional[str] = None
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None


@dataclass
class Message:
    message_id: str
    from_user: User
    chat: Chat
    date: datetime
    message_type: MessageType
    content: str
    raw_data: Dict[str, Any]
    reply_to_message: Optional['Message'] = None
    entities: Optional[List[Dict[str, Any]]] = None

    @property
    def is_command(self) -> bool:
        return any(entity.get('type') == 'bot_command' for entity in self.entities or [])
