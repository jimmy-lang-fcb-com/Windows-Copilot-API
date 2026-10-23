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
(:func:`build_turn`). If Microsoft changes the shape, re-run
``python -m copilot login`` and the new template is picked up automatically.

Pure functions only — no I/O — so this is unit-tested offline.
"""

import base64
import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

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

# Request fields that must be unique per turn. Regenerated in the template
# (consistently: an id reused elsewhere, e.g. in the socket URL, gets the same
# new value there too).
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


def build_turn(
    template: dict,
    captured_url: str,
    access_token: str,
    prompt: str,
    conversation_id: str,
    start_of_session: bool,
    invocation_id: str = "0",
) -> Tuple[str, str]:
    """Build the socket URL and ``chat`` request for one turn from the capture.

    Returns ``(url, wire_text)``. Ids are swapped *consistently*: each old id
    (the conversation id, every per-turn id in the body, and the URL's
    ClientRequestId) maps to one new value, replaced everywhere it appears. The
    page reuses one id in several places (e.g. ``clientrequestid`` and
    ``chatsessionid`` in the URL match ``requestId``/``traceId`` in the body), and
    the service rejects a turn whose ids disagree.

    The socket URL also carries a ``ConversationId`` of its own, which must match
    the body's ``conversationId`` or the service answers ``InvalidRequest``. The
    captured URL may come from a different socket than the captured request, so
    both conversation ids map to ``conversation_id``.
    """
    args = template["arguments"][0]
    parts = urlparse(captured_url)
    captured_query = parse_qsl(parts.query, keep_blank_values=True)

    mapping: Dict[str, str] = {}
    old_conversations = [args.get("conversationId")] + [
        value for key, value in captured_query if key.lower() == "conversationid"
    ]
    for old in old_conversations:
        if isinstance(old, str) and old:
            mapping[old] = conversation_id
    for old in _per_turn_ids(template):
        mapping.setdefault(old, str(uuid.uuid4()))

    # URL: fresh token; ClientRequestId follows the mapping when the body uses it.
    query = []
    for key, value in captured_query:
        low = key.lower()
        if low in ("access_token", "accesstoken"):
            value = access_token
        elif low == "conversationid":
            value = conversation_id
        elif low == "clientrequestid":
            value = _lookup(mapping, value) or mapping.setdefault(value, str(uuid.uuid4()))
        else:
            value = _remap(value, mapping)
        query.append((key, value))
    url = urlunparse(parts._replace(query=urlencode(query, quote_via=quote)))

    rec = json.loads(_remap(json.dumps(template), mapping))
    rec["invocationId"] = invocation_id
    args = rec["arguments"][0]
    args["conversationId"] = conversation_id
    if "isStartOfSession" in args:
        args["isStartOfSession"] = start_of_session
    args["message"]["text"] = prompt
    _refresh_timestamps(rec)
    return url, json.dumps(rec) + RS


def _per_turn_ids(node: Any) -> List[str]:
    """Values of the per-turn id fields anywhere in ``node``."""
    found: List[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _PER_TURN_ID_KEYS and isinstance(value, str) and len(value) >= 8:
                found.append(value)
            else:
                found.extend(_per_turn_ids(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_per_turn_ids(item))
    return found


def _lookup(mapping: Dict[str, str], value: str) -> Optional[str]:
    for old, new in mapping.items():
        if old.lower() == value.lower():
            return new
    return None


def _remap(text: str, mapping: Dict[str, str]) -> str:
    """Replace every old id in ``text`` (case-insensitively) with its new value."""
    for old, new in mapping.items():
        if len(old) >= 8:
            text = re.sub(re.escape(old), new, text, flags=re.IGNORECASE)
    return text


def _refresh_timestamps(node: Any) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "timestamp" and isinstance(value, str):
                node[key] = datetime.now(timezone.utc).astimezone().isoformat()
            else:
                _refresh_timestamps(value)
    elif isinstance(node, list):
        for item in node:
            _refresh_timestamps(item)


def token_from_url(url: str) -> Optional[str]:
    """The access token a chat-socket URL carries (SignalR uses ``access_token``)."""
    for key, value in parse_qsl(urlparse(url).query, keep_blank_values=True):
        if key.lower() in ("access_token", "accesstoken"):
            return value
    return None


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


# -- debugging ------------------------------------------------------------------

_REDACTORS = [
    (re.compile(r"(access_?token)=[^&\s\"]+", re.I), r"\1=<REDACTED>"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)?"), "<JWT>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<EMAIL>"),
    (re.compile(r"[A-Za-z0-9_\-]{60,}"), "<REDACTED>"),
]


def redact(text: str) -> str:
    """Strip tokens, JWTs, emails and long opaque strings from log text."""
    for pattern, repl in _REDACTORS:
        text = pattern.sub(repl, text)
    return text
