"""Example 4 — talk to the server over plain HTTP (OpenAI-compatible).

Use this when the caller is NOT this Python process: another language, a tool,
or a different machine. The server speaks the OpenAI Chat Completions shape.

First, start the server in another terminal:

    python app.py

Then run this from the project root:

    python examples/04_server_http.py

Each response includes Copilot's `conversation_id` as an extra field; send it
back to continue the same thread.
"""

import requests

URL = "http://localhost:8000/v1/chat/completions"

# Turn 1 — new conversation.
first = requests.post(URL, json={
    "model": "m365-copilot",
    "messages": [{"role": "user", "content": "My name is Ada. Remember it."}],
}).json()
print("Copilot:", first["choices"][0]["message"]["content"])

cid = first["conversation_id"]
print("conversation_id:", cid)

# Turn 2 — continue by sending the conversation_id back in the body.
second = requests.post(URL, json={
    "model": "m365-copilot",
    "conversation_id": cid,
    "messages": [{"role": "user", "content": "What's my name? Just the name."}],
}).json()
print("Copilot:", second["choices"][0]["message"]["content"])  # -> recalls "Ada"
