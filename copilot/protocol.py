"""The M365 Copilot chat-socket protocol: parsing replies and building requests.

M365 Copilot's web app chats over a SignalR hub (JSON protocol). Every record is
a JSON object terminated by the record separator ``\\x1e``:

    client -> {"protocol":"json","version":1}\\x1e            handshake
    server -> {}\\x1e                                          handshake ack
    client -> {"type":4,"target":"chat","invocationId":"0",
               "arguments":[{... "conversationId", "message":{"text":...}}]}
    server -> {"type":1,"target":"update","arguments":[{"messages":[...]}]}*
    server -> {"type":2,"invocationId":"0","item":{"messages":[...],"result":{...}}}
    server -> {"type":3,"invocationId":"0"}                    completion
    either -> {"type":6}                                       ping

Bot text in ``update`` frames is *cumulative* (each frame repeats the whole
message so far), so :class:`ReplyParser` turns it into deltas.

We deliberately don't hard-code the request body. It carries dozens of
tenant- and build-specific option flags, so instead we record the one the page
sends during login (the "template") and swap in the prompt and ids
(:func:`build_invocation`). If Microsoft changes the shape, re-run
``python -m copilot login`` and the new template is picked up automatically.

Pure functions only — no I/O — so this is unit-tested offline.
"""

import base64
import copy
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

RS = "\x1e"
HANDSHAKE = json.dumps({"protocol": "json", "version": 1}) + RS

# SignalR message types.
INVOCATION, STREAM_ITEM, COMPLETION, STREAM_INVOCATION, PING, CLOSE = 1, 2, 3, 4, 6, 7

# Event kinds yielded by the parser and the drivers.
TEXT = "text"                  # payload: str delta
CONVERSATION = "conversation"  # payload: conversation id
DONE = "done"                  # payload: None
ERROR = "error"                # payload: str message

Event = Tuple[str, Optional[str]]

# Bot messages with one of these types are status/telemetry, not the answer.
# (The answer itself carries no messageType, or "Chat".)
_ANSWER_TYPES = (None, "", "Chat")

# Request fields that must be unique per turn. Regenerated in the template.
_PER_TURN_ID_KEYS = {"requestId", "traceId", "clientCorrelationId", "messageId"}


def split_records(buffer: str) -> Tuple[List[dict], str]:
    """Split a SignalR text buffer into complete JSON records + leftover text."""
    *complete, rest = buffer.split(RS)
    records = []
    for raw in complete:
        raw = raw.strip()
        if not raw:
            continue
        try:
            records.append(json.loads(raw))
        except ValueError:
            continue  # not JSON (shouldn't happen); skip rather than abort the turn
    return records, rest


class ReplyParser:
    """Turn raw chat-socket text into ``(kind, payload)`` events.

    Feed it every received frame with :meth:`feed`. It buffers partial records,
    tracks how much of each bot message has already been emitted, and reports the
    conversation id, text deltas, errors, and end-of-turn.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._emitted: Dict[str, str] = {}  # message key -> text emitted so far
        self._conversation_id: Optional[str] = None
        self.finished = False

    def feed(self, data: Any) -> List[Event]:
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data).decode("utf-8", "replace")
        records, self._buffer = split_records(self._buffer + data)
        events: List[Event] = []
        for rec in records:
            events.extend(self._handle(rec))
        return events

    # -- internals -----------------------------------------------------------

    def _handle(self, rec: dict) -> List[Event]:
        if self.finished:
            return []
        kind = rec.get("type")
        if kind == INVOCATION and rec.get("target") == "update":
            args = (rec.get("arguments") or [{}])[0] or {}
            return self._conversation(args.get("conversationId")) + self._messages(args.get("messages"))
        if kind == STREAM_ITEM:
            item = rec.get("item") or {}
            events = self._conversation(item.get("conversationId"))
            events += self._messages(item.get("messages"))
            result = item.get("result") or {}
            value = result.get("value")
            if value and value != "Success":
                self.finished = True
                detail = result.get("message") or result.get("error") or ""
                return events + [(ERROR, _describe_result(value, detail))]
            return events
        if kind == COMPLETION:
            self.finished = True
            if rec.get("error"):
                return [(ERROR, f"M365 Copilot error: {rec['error']}")]
            return [(DONE, None)]
        if kind == CLOSE:
            self.finished = True
            return [(ERROR, f"M365 Copilot closed the connection: {rec.get('error') or 'no reason given'}")]
        return []  # pings, handshake acks, other targets

    def _conversation(self, cid: Optional[str]) -> List[Event]:
        if cid and cid != self._conversation_id:
            self._conversation_id = cid
            return [(CONVERSATION, cid)]
        return []

    def _messages(self, messages) -> List[Event]:
        events: List[Event] = []
        for i, msg in enumerate(messages or []):
            if not isinstance(msg, dict) or msg.get("author") != "bot":
                continue
            if msg.get("messageType") not in _ANSWER_TYPES:
                continue
            text = msg.get("text")
            if not isinstance(text, str) or not text:
                continue
            key = msg.get("messageId") or msg.get("requestId") or f"#{i}"
            before = self._emitted.get(key, "")
            if text.startswith(before):
                delta = text[len(before):]
            else:
                # The bot rewrote earlier text (rare). We can't un-send what the
                # caller already has, so only emit what extends past it.
                delta = text[len(before):] if len(text) > len(before) else ""
            self._emitted[key] = text
            if delta:
                events.append((TEXT, delta))
        return events


def _describe_result(value: str, detail: str) -> str:
    hints = {
        "Throttled": "M365 Copilot throttled this account; slow down and retry later.",
        "UnauthorizedRequest": "M365 Copilot rejected the token; run `python -m copilot login`.",
        "InvalidRequest": "M365 Copilot rejected the request shape; re-run "
                          "`python -m copilot login` to capture a fresh request template.",
    }
    msg = hints.get(value, f"M365 Copilot returned {value}")
    return f"{msg} {detail}".strip()


# -- requests -----------------------------------------------------------------


def invocation_from_frame(data: Any) -> Optional[dict]:
    """Return the ``chat`` invocation in a frame the page *sent*, else ``None``."""
    if isinstance(data, (bytes, bytearray)):
        data = bytes(data).decode("utf-8", "replace")
    if not isinstance(data, str) or '"chat"' not in data:
        return None
    records, _ = split_records(data if data.endswith(RS) else data + RS)
    for rec in records:
        if rec.get("type") in (INVOCATION, STREAM_INVOCATION) and rec.get("target") == "chat":
            return rec
    return None


def build_invocation(
    template: dict,
    prompt: str,
    conversation_id: str,
    start_of_session: bool,
    invocation_id: str = "0",
) -> str:
    """Fill a captured ``chat`` invocation with a new prompt; return the wire text."""
    rec = copy.deepcopy(template)
    rec["invocationId"] = invocation_id
    args = rec["arguments"][0]
    args["conversationId"] = conversation_id
    if "isStartOfSession" in args:
        args["isStartOfSession"] = start_of_session
    args["message"]["text"] = prompt
    _refresh_ids(args)
    return json.dumps(rec) + RS


def _refresh_ids(node: Any) -> None:
    """Regenerate per-turn ids and timestamps in place."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _PER_TURN_ID_KEYS and isinstance(value, str):
                node[key] = str(uuid.uuid4())
            elif key == "timestamp" and isinstance(value, str):
                node[key] = datetime.now(timezone.utc).astimezone().isoformat()
            else:
                _refresh_ids(value)
    elif isinstance(node, list):
        for item in node:
            _refresh_ids(item)


def token_from_url(url: str) -> Optional[str]:
    """The access token a chat-socket URL carries (SignalR uses ``access_token``)."""
    for key, value in parse_qsl(urlparse(url).query, keep_blank_values=True):
        if key.lower() in ("access_token", "accesstoken"):
            return value
    return None


def socket_url(captured_url: str, access_token: str) -> str:
    """Rebuild a captured chat-socket URL with a fresh token and request id."""
    parts = urlparse(captured_url)
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        low = key.lower()
        if low in ("access_token", "accesstoken"):
            value = access_token
        elif low == "clientrequestid":
            value = str(uuid.uuid4())
        query.append((key, value))
    return urlunparse(parts._replace(query=urlencode(query)))


def jwt_expiry(token: Optional[str]) -> Optional[float]:
    """The ``exp`` claim of a JWT (unverified), or ``None`` if unreadable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return None


def token_is_fresh(token: Optional[str], margin: float) -> bool:
    exp = jwt_expiry(token)
    return bool(token) and exp is not None and exp - margin > time.time()
