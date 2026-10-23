"""
M365 Copilot API — an unofficial Python bridge to Microsoft 365 Copilot chat.

>>> from copilot import CopilotClient
>>> client = CopilotClient()
>>> r = client.chat("Hello!")                      # new conversation
>>> r.text, r.conversation_id
>>> client.chat("And again?", r.conversation_id)   # continue it
>>> for chunk in client.stream("Stream this"):     # incremental output
...     print(chunk, end="")
"""

__version__ = "2.0.0"

from .client import ChatReply, ChatStream, CopilotClient
from .errors import AuthExpired, AuthRequired, CopilotError

__all__ = [
    "CopilotClient",
    "ChatReply",
    "ChatStream",
    "CopilotError",
    "AuthRequired",
    "AuthExpired",
]
