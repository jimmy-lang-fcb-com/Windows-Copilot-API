"""Example 6 — use the official OpenAI SDK against the server.

The whole point of the server is OpenAI compatibility: point any OpenAI client at
it and existing code works unchanged.

Install the SDK first:

    pip install openai

Start the server in another terminal:

    python app.py

Then run this from the project root:

    python examples/06_server_openai_sdk.py
"""

from openai import OpenAI

# Point base_url at the server. api_key is required by the SDK but ignored here.
client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")

completion = client.chat.completions.create(
    model="m365-copilot",
    messages=[{"role": "user", "content": "Say hello in one short sentence."}],
)
print(completion.choices[0].message.content)

# conversation_id is outside OpenAI's schema, so the SDK keeps it in model_extra.
extra = getattr(completion, "model_extra", None) or {}
cid = extra.get("conversation_id")
print("conversation_id:", cid)

# To continue that conversation, send the id back via extra_body:
#
#   client.chat.completions.create(
#       model="m365-copilot",
#       messages=[{"role": "user", "content": "..."}],
#       extra_body={"conversation_id": cid},
#   )
