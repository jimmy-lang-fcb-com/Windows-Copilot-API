"""FastAPI app wiring M365 Copilot onto the OpenAI Chat Completions API."""

import json
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

from copilot import AuthRequired, CopilotClient

from .config import (
    CONVERSATION_REUSE,
    MODEL_NAME,
    RATE_LIMIT_BURST,
    RATE_LIMIT_RPM,
    TOOL_CALL_RETRIES,
)
from .conversations import ConversationCache, tools_scope
from .openai_format import (
    completion_response,
    estimate_tokens,
    new_id,
    sse_event,
    stream_chunk,
    tool_call_deltas,
    usage,
)
from .prompt import followup_prompt, messages_to_prompt
from .ratelimit import TokenBucket
from .schemas import ChatCompletionRequest
from .tools import (
    ParsedReply,
    build_followup,
    build_prompt,
    correction_prompt,
    holdback,
    parse_reply,
)

app = FastAPI(title="M365 Copilot OpenAI-compatible API", version="2.0.0")
# The server must never pop a sign-in window mid-request: when the session is
# signed out it answers 503 and an operator re-runs `python -m copilot login`.
# Mode (browser/direct) comes from $COPILOT_MODE.
client = CopilotClient(interactive_login=False)

_AUTH_HELP = (
    "M365 Copilot session is signed out or expired. "
    "Run `python -m copilot login` on this machine, then retry."
)

# Self-imposed rate limit on top of the concurrency lock below: this caps
# requests-per-minute, the lock caps requests-in-flight. See server/ratelimit.py.
_rate_limiter = TokenBucket(RATE_LIMIT_RPM, RATE_LIMIT_BURST)


def _rate_limited_response():
    """Spend a token; return an OpenAI-shaped 429 if none left, else ``None``."""
    allowed, wait = _rate_limiter.try_acquire()
    if allowed:
        return None
    secs = max(1, round(wait))
    return JSONResponse(
        status_code=429,
        headers={"Retry-After": str(secs)},
        content={"error": {
            "message": (
                f"Rate limit exceeded (>{RATE_LIMIT_RPM:g} req/min). "
                f"Retry in {secs}s."
            ),
            "type": "rate_limit_error",
            "code": "rate_limit_exceeded",
        }},
    )

# This server bridges a single signed-in account (and, in browser mode, a single
# browser), so upstream calls are serialized: concurrent HTTP requests queue here
# and run one at a time. Predictable, at the cost of parallelism — fine for a
# proof of concept.
_upstream_lock = threading.Lock()

# History prefix -> M365 conversation, so a chat session continues one M365
# conversation instead of resending everything. See server/conversations.py.
_conversations = ConversationCache()


def _log(msg: str) -> None:
    print(f"[server] {msg}", file=sys.stderr, flush=True)


# -- planning a turn ---------------------------------------------------------------


@dataclass
class Plan:
    """What to send upstream for one request."""

    messages: list
    tools: Optional[List[Dict[str, Any]]]
    full_prompt: str                       # the whole history, flattened
    prompt: str                            # what is actually sent
    conversation_id: Optional[str]         # M365 conversation to continue, if any
    reused: bool = False                   # continuing via the cache (can fall back)
    scope: str = ""


def _plan(req: ChatCompletionRequest) -> Plan:
    tools = req.tools if req.tools and req.tool_choice != "none" else None
    full = (build_prompt(req.messages, tools, req.tool_choice) if tools
            else messages_to_prompt(req.messages))
    scope = tools_scope(tools)
    plan = Plan(req.messages, tools, full, full, req.conversation_id, scope=scope)
    if req.conversation_id is None and CONVERSATION_REUSE:
        hit = _conversations.lookup(req.messages, scope)
        if hit is not None:
            conversation_id, start = hit
            plan.prompt = (build_followup(req.messages, start, tools, req.tool_choice) if tools
                           else followup_prompt(req.messages[start:]))
            plan.conversation_id = conversation_id
            plan.reused = True
    return plan


# -- running a turn ----------------------------------------------------------------


@dataclass
class Result:
    content: str
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    conversation_id: Optional[str] = None
    usage: Dict[str, int] = field(default_factory=dict)


def _resolve_tool_calls(text: str, conversation_id, tools) -> ParsedReply:
    """Parse tool calls from a reply; ask M365 to fix a malformed one.

    The correction is a follow-up in the same M365 conversation, so the model
    sees its own broken reply.
    """
    parsed = parse_reply(text, tools)
    for _ in range(TOOL_CALL_RETRIES):
        if not parsed.error or not conversation_id:
            break
        retry = client.chat(correction_prompt(parsed.error), conversation_id=conversation_id)
        fixed = parse_reply(retry.text, tools)
        if fixed.tool_calls or not fixed.error:
            if not fixed.content:
                fixed.content = parsed.content
            parsed = fixed
    return parsed


def _turn(plan: Plan):
    """Run one turn upstream. Yields ``("text", piece)`` then ``("result", Result)``.

    Call with the upstream lock held. When continuing a cached conversation
    fails before any text arrives (e.g. a browser tab was closed), it falls back
    to sending the whole history to a fresh conversation.
    """
    attempts = [(plan.prompt, plan.conversation_id)]
    if plan.reused:
        attempts.append((plan.full_prompt, None))
    for n, (prompt, conversation_id) in enumerate(attempts):
        parts: List[str] = []
        try:
            stream = client.stream(prompt, conversation_id=conversation_id)
            for piece in stream:
                if isinstance(piece, str) and piece:
                    parts.append(piece)
                    yield ("text", piece)
            break
        except AuthRequired:
            raise
        except Exception as exc:
            if parts or n == len(attempts) - 1:
                raise
            _log(f"Couldn't continue conversation {conversation_id} ({exc}); "
                 "resending the full history to a new one.")
            _conversations.forget(conversation_id)

    text = "".join(parts)
    conversation_id = stream.conversation_id
    parsed = (_resolve_tool_calls(text, conversation_id, plan.tools) if plan.tools
              else ParsedReply(text, []))

    reply = {"role": "assistant", "content": parsed.content, "tool_calls": parsed.tool_calls}
    if CONVERSATION_REUSE:
        _conversations.remember(plan.messages, reply, plan.scope, conversation_id)
    # Prompt tokens estimate the whole conversation (the context the client is
    # tracking), even when only the new messages were sent.
    completion = parsed.content + (json.dumps(parsed.tool_calls) if parsed.tool_calls else "")
    yield ("result", Result(parsed.content, parsed.tool_calls, conversation_id,
                            usage(estimate_tokens(plan.full_prompt), estimate_tokens(completion))))


def _stream(plan: Plan, model: str):
    """Yield OpenAI ``chat.completion.chunk`` SSE events for one turn.

    With tools, text is streamed until something that could start a tool call
    appears; the rest is buffered and sent at the end either as ``tool_calls``
    or as the remaining text.
    """
    cid = new_id()
    created = int(time.time())
    try:
        with _upstream_lock:  # one upstream chat at a time (released on disconnect)
            yield sse_event(stream_chunk(cid, created, model, {"role": "assistant"}))
            buffer, sent = "", 0
            for kind, value in _turn(plan):
                if kind == "text":
                    buffer += value
                    safe = holdback(buffer) if plan.tools else len(buffer)
                    if safe > sent:
                        yield sse_event(stream_chunk(cid, created, model, {"content": buffer[sent:safe]}))
                        sent = safe
                    continue
                result: Result = value
                if result.tool_calls:
                    yield sse_event(stream_chunk(
                        cid, created, model, {"tool_calls": tool_call_deltas(result.tool_calls)}))
                elif len(buffer) > sent:
                    yield sse_event(stream_chunk(cid, created, model, {"content": buffer[sent:]}))
                yield sse_event(stream_chunk(
                    cid, created, model, {},
                    finish="tool_calls" if result.tool_calls else "stop",
                    conversation_id=result.conversation_id,
                    usage_info=result.usage,
                ))
    except AuthRequired:
        yield sse_event(
            stream_chunk(cid, created, model, {"content": f"\n[error: {_AUTH_HELP}]"}, finish="error")
        )
    except Exception as exc:  # surface errors to the client instead of hanging
        yield sse_event(
            stream_chunk(cid, created, model, {"content": f"\n[error: {exc}]"}, finish="error")
        )
    yield "data: [DONE]\n\n"


# -- routes ------------------------------------------------------------------------


@app.get("/v1/models")
def list_models():
    return {
        "object": "list",
        "data": [
            {"id": MODEL_NAME, "object": "model", "created": 0, "owned_by": "microsoft"}
        ],
    }


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionRequest):
    plan = _plan(req)
    if not plan.full_prompt.strip():
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "no text content in messages", "type": "invalid_request_error"}},
        )
    model = req.model or MODEL_NAME

    # Enforce the per-minute ceiling before touching the upstream lock, so excess
    # callers get a fast 429 instead of piling up behind the serialized queue.
    limited = _rate_limited_response()
    if limited is not None:
        return limited

    if req.stream:
        return StreamingResponse(_stream(plan, model), media_type="text/event-stream")

    try:
        with _upstream_lock:  # serialize: one upstream chat at a time
            result = None
            for kind, value in _turn(plan):
                if kind == "result":
                    result = value
    except AuthRequired:
        return JSONResponse(
            status_code=503,
            content={"error": {"message": _AUTH_HELP, "type": "auth_required"}},
        )
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(exc), "type": "upstream_error"}},
        )
    return completion_response(result.content, model, result.conversation_id,
                               result.tool_calls or None, result.usage)


@app.get("/")
def root():
    return {"service": "M365 Copilot OpenAI-compatible API", "mode": client.mode,
            "endpoints": ["/v1/models", "/v1/chat/completions"]}
