"""Reuse one M365 Copilot conversation across an OpenAI-style chat session.

OpenAI clients resend the whole history every turn. Sending all of it to M365
Copilot each time would start a new M365 chat per request and soon hit the
per-message length limit. Instead, after each reply we remember:

    fingerprint(history + the reply we returned)  ->  M365 conversation id

When a later request's history starts with a remembered prefix, only the new
messages after it (tool results, the next user message) go to that existing
M365 conversation.

Fingerprints ignore system messages (clients often regenerate them) and, for
tool-calling turns, identify the assistant's turn by its tool call ids, which
the server generated and the client echoes unchanged.
"""

import hashlib
import json
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from .prompt import content_text

_IGNORED_ROLES = ("system", "developer")


def _norm(text: Optional[str]) -> str:
    return " ".join((text or "").split())


def _get(m: Any, key: str) -> Any:
    return m.get(key) if isinstance(m, dict) else getattr(m, key, None)


def fingerprint(m: Any) -> Optional[str]:
    """A stable identity for one message, or ``None`` for ignored roles."""
    role = _get(m, "role")
    if role in _IGNORED_ROLES:
        return None
    if role == "assistant":
        calls = _get(m, "tool_calls") or []
        if calls:
            return "assistant:calls:" + ",".join(sorted(str(c.get("id")) for c in calls))
        return "assistant:" + _norm(content_text(_get(m, "content")))
    if role == "tool":
        return f"tool:{_get(m, 'tool_call_id')}:" + _norm(content_text(_get(m, "content")))
    return f"{role}:" + _norm(content_text(_get(m, "content")))


def prefix_keys(messages: List[Any], scope: str) -> List[Tuple[int, str]]:
    """``(index, key)`` for every prefix ``messages[:index]`` (index >= 1).

    ``index`` counts positions in the original list, so callers can slice it.
    ``scope`` (e.g. the tool names) keeps sessions with different tool sets apart.
    """
    keys = []
    h = hashlib.sha256(scope.encode())
    seen = False
    for i, m in enumerate(messages):
        fp = fingerprint(m)
        if fp is None:
            continue
        h.update(b"\x00" + fp.encode())
        seen = True
        keys.append((i + 1, h.copy().hexdigest()))
    return keys if seen else []


def tools_scope(tools: Optional[List[Dict[str, Any]]]) -> str:
    names = sorted((t.get("function", t) or {}).get("name", "") for t in tools or [])
    return json.dumps(names)


class ConversationCache:
    """Thread-safe LRU of history-prefix key -> M365 conversation id."""

    def __init__(self, max_entries: int = 500, ttl: float = 6 * 3600, clock=time.time):
        self.max_entries = max_entries
        self.ttl = ttl
        self._clock = clock
        self._entries: "OrderedDict[str, Tuple[str, float]]" = OrderedDict()
        self._lock = threading.Lock()

    def lookup(self, messages: List[Any], scope: str) -> Optional[Tuple[str, int]]:
        """Longest remembered prefix with new messages after it.

        Returns ``(conversation_id, index)``: send ``messages[index:]`` to that
        conversation. ``None`` if nothing matches.
        """
        keys = prefix_keys(messages, scope)
        now = self._clock()
        with self._lock:
            for index, key in reversed(keys):
                if index >= len(messages):
                    continue  # nothing new after this prefix
                hit = self._entries.get(key)
                if hit is None:
                    continue
                conversation_id, stamp = hit
                if now - stamp > self.ttl:
                    del self._entries[key]
                    continue
                self._entries.move_to_end(key)
                return conversation_id, index
        return None

    def remember(self, messages: List[Any], reply: Dict[str, Any], scope: str,
                 conversation_id: Optional[str]) -> None:
        """Record that ``messages + [reply]`` lives in ``conversation_id``."""
        if not conversation_id:
            return
        keys = prefix_keys(list(messages) + [reply], scope)
        if not keys:
            return
        with self._lock:
            self._entries[keys[-1][1]] = (conversation_id, self._clock())
            self._entries.move_to_end(keys[-1][1])
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def forget(self, conversation_id: str) -> None:
        with self._lock:
            for key in [k for k, (cid, _) in self._entries.items() if cid == conversation_id]:
                del self._entries[key]
