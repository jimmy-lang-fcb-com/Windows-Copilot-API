"""FastAPI app wiring M365 Copilot onto the OpenAI Chat Completions API."""

import threading
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse

from copilot import AuthRequired, CopilotClient

from .config import MODEL_NAME, RATE_LIMIT_BURST, RATE_LIMIT_RPM, TOOL_CALL_RETRIES
from .openai_format import (
    completion_response,
    new_id,
    sse_event,
    stream_chunk,
    tool_call_deltas,
)
from .prompt import messages_to_prompt
from .ratelimit import TokenBucket
from .schemas import ChatCompletionRequest
from .tools import ParsedReply, build_prompt, correction_prompt, holdback, parse_reply

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


def _stream(prompt: str, model: str, conversation_id=None):
    """Yield OpenAI ``chat.completion.chunk`` SSE events for ``prompt``.

    ``conversation_id`` continues an existing Copilot thread; ``None`` starts a
    fresh one (its id is emitted on the final chunk).
    """
    cid = new_id()
    created = int(time.time())
    try:
        with _upstream_lock:  # one upstream chat at a time (released on disconnect)
            yield sse_event(stream_chunk(cid, created, model, {"role": "assistant"}))
            stream = client.stream(prompt, conversation_id=conversation_id)
            for piece in stream:
                if isinstance(piece, str) and piece:
                    yield sse_event(stream_chunk(cid, created, model, {"content": piece}))
            # The conversation id is known once the stream has run; emit it
            # on the final chunk so callers can track the upstream thread.
            yield sse_event(
                stream_chunk(
                    cid, created, model, {}, finish="stop",
                    conversation_id=stream.conversation_id,
                )
            )
    except AuthRequired:
        yield sse_event(
            stream_chunk(cid, created, model, {"content": f"\n[error: {_AUTH_HELP}]"}, finish="error")
        )
    except Exception as exc:  # surface errors to the client instead of hanging
        yield sse_event(
            stream_chunk(cid, created, model, {"content": f"\n[error: {exc}]"}, finish="error")
        )
    yield "data: [DONE]\n\n"


def _resolve_tool_calls(text: str, conversation_id, tools) -> ParsedReply:
    """Parse tool calls from a reply; ask M365 to fix a malformed one.

    Runs under the upstream lock. The correction is sent as a follow-up in the
    same M365 conversation, so the model sees its own broken reply.
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


def _stream_with_tools(prompt: str, model: str, tools, conversation_id=None):
    """Like :func:`_stream`, but recognizes emulated tool calls.

    Text is streamed until something that could start a tool call appears; from
    there the reply is buffered, parsed at the end, and sent either as
    ``tool_calls`` or as the remaining text.
    """
    cid = new_id()
    created = int(time.time())
    try:
        with _upstream_lock:
            yield sse_event(stream_chunk(cid, created, model, {"role": "assistant"}))
            stream = client.stream(prompt, conversation_id=conversation_id)
            buffer, sent = "", 0
            for piece in stream:
                if not isinstance(piece, str) or not piece:
                    continue
                buffer += piece
                safe = holdback(buffer)
                if safe > sent:
                    yield sse_event(stream_chunk(cid, created, model, {"content": buffer[sent:safe]}))
                    sent = safe
            parsed = _resolve_tool_calls(buffer, stream.conversation_id, tools)
            if parsed.tool_calls:
                yield sse_event(stream_chunk(
                    cid, created, model, {"tool_calls": tool_call_deltas(parsed.tool_calls)}))
                finish = "tool_calls"
            else:
                if len(buffer) > sent:
                    yield sse_event(stream_chunk(cid, created, model, {"content": buffer[sent:]}))
                finish = "stop"
            yield sse_event(stream_chunk(
                cid, created, model, {}, finish=finish, conversation_id=stream.conversation_id))
    except AuthRequired:
        yield sse_event(
            stream_chunk(cid, created, model, {"content": f"\n[error: {_AUTH_HELP}]"}, finish="error")
        )
    except Exception as exc:
        yield sse_event(
            stream_chunk(cid, created, model, {"content": f"\n[error: {exc}]"}, finish="error")
        )
    yield "data: [DONE]\n\n"


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
    tools = req.tools if req.tools and req.tool_choice != "none" else None
    if tools:
        prompt = build_prompt(req.messages, tools, req.tool_choice)
    else:
        prompt = messages_to_prompt(req.messages)
    if not prompt.strip():
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
        events = (_stream_with_tools(prompt, model, tools, req.conversation_id) if tools
                  else _stream(prompt, model, req.conversation_id))
        return StreamingResponse(events, media_type="text/event-stream")

    try:
        with _upstream_lock:  # serialize: one upstream chat at a time
            reply = client.chat(prompt, conversation_id=req.conversation_id)
            if tools:
                parsed = _resolve_tool_calls(reply.text, reply.conversation_id, tools)
                return completion_response(
                    parsed.content, model, reply.conversation_id, parsed.tool_calls or None)
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
    return completion_response(reply.text, model, reply.conversation_id)


@app.get("/")
def root():
    return {"service": "M365 Copilot OpenAI-compatible API", "mode": client.mode, "endpoints": ["/v1/models", "/v1/chat/completions"]}
