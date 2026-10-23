"""Example 2 — a multi-turn conversation, in-process.

Every reply comes with a `conversation_id`. Pass that id back on the next call
to continue the SAME conversation, so Copilot remembers earlier turns.

Run it from the project root:

    python examples/02_direct_conversation.py
"""

# Make the project importable when this file is run directly.
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import time

from copilot import CopilotClient

client = CopilotClient()

# Turn 1 — no id is given, so this starts a NEW conversation and returns its id.
first = client.chat("My name is Ada. Remember it.")
print("Copilot:", first.text)
print("conversation_id:", first.conversation_id)

time.sleep(3)  # be gentle — Copilot serves one conversation at a time

# Turn 2 — pass the id back to CONTINUE the same conversation.
second = client.chat("What's my name? Reply with just the name.", first.conversation_id)
print("Copilot:", second.text)  # -> recalls "Ada"
