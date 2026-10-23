"""Emulated tool calling for M365 Copilot.

M365 Copilot has no tool-calling API, so the server fakes it with prompting:

1. The tool list from the request is written into the prompt, with an
   instruction to answer with ``<tool_call>{"name": ..., "arguments": {...}}</tool_call>``
   blocks when a tool is needed (:func:`build_prompt`).
2. The reply is scanned for those blocks and turned into OpenAI ``tool_calls``
   (:func:`parse_reply`). The *client* (e.g. opencode) runs the tools.
3. The client sends results back as ``role: "tool"`` messages, which are written
   into the next prompt as ``<tool_result>`` blocks.

The parser is deliberately forgiving (HTML-escaped tags, code fences, a bare
JSON reply, a missing closing tag), because the model doesn't always follow the
format exactly. Pure functions only, unit-tested in tests/test_tools.py.
"""

import html
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .prompt import content_text
from .schemas import ChatMessage

# Long tool descriptions eat into M365's per-message limit, so they're trimmed.
TOOL_DESC_MAX = int(os.environ.get("TOOL_DESC_MAX", "400"))
PARAM_DESC_MAX = int(os.environ.get("TOOL_PARAM_DESC_MAX", "150"))
# A single tool result (file contents, command output) is cut to this length.
TOOL_RESULT_MAX = int(os.environ.get("TOOL_RESULT_MAX_CHARS", "8000"))
# Optional cap on the whole prompt; oldest history is dropped to fit. 0 = off.
PROMPT_MAX = int(os.environ.get("PROMPT_MAX_CHARS", "0"))

OPEN, CLOSE = "<tool_call>", "</tool_call>"

# Text that may start a tool call. While streaming, output is held back from the
# first of these so a tool call is never sent to the client as plain text.
STREAM_MARKERS = ("<tool_call", "&lt;tool_call", "```")

_INSTRUCTIONS = """# Tools

You can use tools that run on the user's computer. You cannot run them yourself. \
To use one, reply with a tool call; the user's system runs it and sends you the result \
in a <tool_result> block.

To call a tool, reply with one or more blocks in exactly this format:
<tool_call>{"name": "TOOL_NAME", "arguments": {"param": "value"}}</tool_call>

Rules:
- "arguments" must be valid JSON matching the tool's parameters. Include every required parameter.
- Put nothing after your tool calls; stop and wait for the results.
- Don't put tool calls in code blocks, and don't describe a call instead of making it.
- If no tool is needed, answer normally with no <tool_call> block.
- For the user's local files, code, or commands, use these tools rather than search.

Available tools:"""

_REMINDER = ("(To use a tool, reply only with <tool_call> blocks. "
             "Otherwise, answer directly.)")


# -- prompt building ---------------------------------------------------------


def _trim(text: Optional[str], limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _type_of(schema: Dict[str, Any]) -> str:
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = "|".join(str(k) for k in kind)
    if kind == "array":
        items = schema.get("items") or {}
        return f"array of {_type_of(items)}" if items else "array"
    if "enum" in schema:
        return " | ".join(json.dumps(v) for v in schema["enum"])
    if not kind and ("anyOf" in schema or "oneOf" in schema):
        options = schema.get("anyOf") or schema.get("oneOf")
        return " | ".join(_type_of(o) for o in options if isinstance(o, dict))
    return str(kind or "any")


def describe_tool(tool: Dict[str, Any]) -> str:
    fn = tool.get("function", tool)
    lines = [f"- {fn.get('name')}: {_trim(fn.get('description'), TOOL_DESC_MAX)}"]
    params = fn.get("parameters") or {}
    required = set(params.get("required") or [])
    for name, schema in (params.get("properties") or {}).items():
        if not isinstance(schema, dict):
            continue
        flag = ", required" if name in required else ""
        desc = _trim(schema.get("description"), PARAM_DESC_MAX)
        line = f"    - {name} ({_type_of(schema)}{flag})"
        lines.append(f"{line}: {desc}" if desc else line)
    return "\n".join(lines)


def _choice_rule(tool_choice: Any) -> str:
    if tool_choice == "required":
        return "\nYou must call at least one tool in your reply."
    if isinstance(tool_choice, dict):
        name = (tool_choice.get("function") or {}).get("name")
        if name:
            return f"\nYou must call the tool {name!r} in your reply."
    return ""


def _render_call(call: Dict[str, Any]) -> str:
    fn = call.get("function") or {}
    args = fn.get("arguments")
    try:
        args = json.loads(args) if isinstance(args, str) else (args or {})
    except ValueError:
        pass
    return OPEN + json.dumps({"name": fn.get("name"), "arguments": args}) + CLOSE


def _cut(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"\n[... truncated {len(text) - limit} characters]"


def _render_message(m: ChatMessage, call_names: Dict[str, str]) -> str:
    text = content_text(m.content)
    if m.role == "user":
        return f"User: {text}"
    if m.role == "assistant":
        calls = "\n".join(_render_call(c) for c in (m.tool_calls or []))
        return "Assistant: " + "\n".join(p for p in (text, calls) if p)
    if m.role == "tool":
        name = m.name or call_names.get(m.tool_call_id or "", "tool")
        return (f'<tool_result name="{name}" id="{m.tool_call_id or ""}">\n'
                f"{_cut(text, TOOL_RESULT_MAX)}\n</tool_result>")
    return f"{m.role.capitalize()}: {text}"


def build_prompt(
    messages: List[ChatMessage],
    tools: List[Dict[str, Any]],
    tool_choice: Any = None,
) -> str:
    """Flatten a tool-using conversation into one M365 Copilot prompt."""
    system = "\n\n".join(content_text(m.content) for m in messages
                         if m.role in ("system", "developer") and m.content)
    tool_section = _INSTRUCTIONS + "\n" + "\n".join(describe_tool(t) for t in tools)
    tool_section += _choice_rule(tool_choice)

    call_names = _call_names(messages)
    history = [_render_message(m, call_names) for m in messages
               if m.role not in ("system", "developer")]

    def assemble(entries: List[str]) -> str:
        parts = [p for p in (system, tool_section, "# Conversation", "\n\n".join(entries)) if p]
        return "\n\n".join(parts) + "\n\n" + _REMINDER + "\nAssistant:"

    prompt = assemble(history)
    # Over the optional cap: drop the oldest turns (keep the latest one).
    while PROMPT_MAX and len(prompt) > PROMPT_MAX and len(history) > 1:
        history = ["[Earlier conversation omitted to fit the length limit.]"] + history[2:]
        prompt = assemble(history)
    return prompt


def _call_names(messages: List[ChatMessage]) -> Dict[str, str]:
    """tool_call_id -> tool name, so results can be labeled."""
    names = {}
    for m in messages:
        for c in m.tool_calls or []:
            names[c.get("id", "")] = (c.get("function") or {}).get("name", "tool")
    return names


def build_followup(messages: List[ChatMessage], start: int, tools: List[Dict[str, Any]],
                   tool_choice: Any = None) -> str:
    """Prompt for a *continued* M365 conversation: only ``messages[start:]``.

    The tool instructions were sent at the start of the conversation, so this
    only repeats a one-line reminder with the tool names.
    """
    call_names = _call_names(messages)
    parts = [_render_message(m, call_names) for m in messages[start:]
             if m.role not in ("system", "developer")]
    names = ", ".join((t.get("function", t) or {}).get("name", "") for t in tools)
    return ("\n\n".join(parts) + _choice_rule(tool_choice)
            + f"\n\n(Tools available: {names}. To use one, reply only with <tool_call> blocks. "
              "Otherwise, answer directly.)")


def correction_prompt(error: str) -> str:
    return (f"Your last reply had an invalid tool call: {error}\n"
            "Reply again with only the corrected <tool_call> blocks, in exactly this format:\n"
            '<tool_call>{"name": "TOOL_NAME", "arguments": {...}}</tool_call>')


# -- reply parsing -----------------------------------------------------------


@dataclass
class ParsedReply:
    content: str                                   # text outside tool calls
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None                    # set if a call was malformed


_decoder = json.JSONDecoder()
_FENCE = re.compile(r"```[a-zA-Z_]*\s*")


def _decode_object(text: str, start: int) -> Tuple[Optional[dict], int]:
    """Decode the JSON object at/after ``start``; return (obj, end) or (None, start)."""
    i = start
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    fence = _FENCE.match(text, i)  # tolerate a code fence inside the tag
    if fence:
        i = fence.end()
    if i >= len(text) or text[i] != "{":
        return None, start
    tail = text[i:]
    try:
        obj, used = _decoder.raw_decode(tail)
    except ValueError:
        # Markdown sometimes escapes underscores (read\_file), which isn't valid JSON.
        try:
            obj, used_clean = _decoder.raw_decode(tail.replace("\\_", "_"))
        except ValueError:
            return None, start
        used = _escaped_length(tail, used_clean)
    return (obj if isinstance(obj, dict) else None), i + used


def _escaped_length(text: str, clean_length: int) -> int:
    """Length in ``text`` that covers ``clean_length`` chars once ``\\_`` became ``_``."""
    i = n = 0
    while n < clean_length and i < len(text):
        i += 2 if text.startswith("\\_", i) else 1
        n += 1
    return i


def _normalize(obj: dict) -> Tuple[Optional[str], Any]:
    """Accept a few common shapes: {name, arguments}, {tool, parameters}, {function: {...}}."""
    if isinstance(obj.get("function"), dict):
        obj = obj["function"]
    name = obj.get("name") or obj.get("tool") or obj.get("tool_name")
    args = obj.get("arguments", obj.get("parameters", obj.get("args", {})))
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except ValueError:
            pass
    return name, args


def _check(name: Any, args: Any, tools: Dict[str, Dict[str, Any]]) -> Optional[str]:
    if not isinstance(name, str) or not name:
        return "missing tool name"
    if name not in tools:
        return f"unknown tool {name!r}; available: {', '.join(sorted(tools))}"
    if not isinstance(args, dict):
        return f"arguments for {name!r} must be a JSON object"
    required = (tools[name].get("parameters") or {}).get("required") or []
    missing = [p for p in required if p not in args]
    if missing:
        return f"{name!r} is missing required argument(s): {', '.join(missing)}"
    return None


def _make_call(name: str, args: dict) -> Dict[str, Any]:
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def parse_reply(text: str, tools: List[Dict[str, Any]]) -> ParsedReply:
    """Pull tool calls out of a reply. Text outside them becomes ``content``."""
    by_name = {}
    for t in tools:
        fn = t.get("function", t)
        if fn.get("name"):
            by_name[fn["name"]] = fn

    source = html.unescape(text)
    calls: List[Dict[str, Any]] = []
    errors: List[str] = []
    spans: List[Tuple[int, int]] = []

    for match in re.finditer(r"<tool_call>|```tool_call", source):
        if spans and match.start() < spans[-1][1]:
            continue  # inside a block we already consumed
        obj, end = _decode_object(source, match.end())
        if obj is None:
            errors.append("a <tool_call> block did not contain a JSON object")
            continue
        rest = source[end:]
        close = re.match(r"\s*(```\s*)?(</tool_call>)?(\s*```)?", rest)
        end += close.end() if close else 0
        spans.append((match.start(), end))
        name, args = _normalize(obj)
        problem = _check(name, args, by_name)
        if problem:
            errors.append(problem)
        else:
            calls.append(_make_call(name, args))

    if not spans:
        # No tags: accept a reply that is *only* a JSON call (optionally fenced).
        stripped = _FENCE.sub("", source).replace("```", "").strip()
        if stripped.startswith("{"):
            obj, end = _decode_object(stripped, 0)
            if obj is not None and not stripped[end:].strip():
                name, args = _normalize(obj)
                if name in by_name and _check(name, args, by_name) is None:
                    return ParsedReply("", [_make_call(name, args)])

    content = source
    for start, end in reversed(spans):
        content = content[:start] + content[end:]
    content = content.strip() if spans else text
    return ParsedReply(content, calls, "; ".join(errors) or None)


def holdback(buffer: str) -> int:
    """How much of ``buffer`` is safe to stream before a possible tool call.

    Returns an index: everything before it can be sent now. Stops at the first
    marker, and holds back a trailing partial marker (e.g. ``"<tool_c"``).
    """
    first = min((i for i in (buffer.find(m) for m in STREAM_MARKERS) if i >= 0), default=-1)
    if first >= 0:
        return first
    for size in range(min(len(buffer), max(map(len, STREAM_MARKERS))), 0, -1):
        tail = buffer[-size:]
        if any(m.startswith(tail) for m in STREAM_MARKERS):
            return len(buffer) - size
    return len(buffer)
