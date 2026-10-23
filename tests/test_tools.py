"""Offline tests for emulated tool calling (server/tools.py + the endpoint)."""

import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from server import api
from server.schemas import ChatMessage
from server.tools import build_prompt, holdback, parse_reply

TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file from disk. " + "Very long details. " * 100,
        "parameters": {"type": "object", "required": ["path"], "properties": {
            "path": {"type": "string", "description": "Absolute path"},
            "limit": {"type": "integer"},
            "mode": {"type": "string", "enum": ["text", "binary"]},
        }},
    }},
    {"type": "function", "function": {
        "name": "bash", "description": "Run a shell command",
        "parameters": {"type": "object", "required": ["command"],
                       "properties": {"command": {"type": "string"}}},
    }},
]


def call(name, **args):
    return f'<tool_call>{json.dumps({"name": name, "arguments": args})}</tool_call>'


class PromptTests(unittest.TestCase):
    def test_tools_and_history_rendered(self):
        messages = [
            ChatMessage(role="system", content="You are a coding agent."),
            ChatMessage(role="user", content="What's in a.txt?"),
            ChatMessage(role="assistant", content=None, tool_calls=[{
                "id": "call_1", "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "/a.txt"}'}}]),
            ChatMessage(role="tool", tool_call_id="call_1", content="hello world"),
        ]
        prompt = build_prompt(messages, TOOLS)
        self.assertTrue(prompt.startswith("You are a coding agent."))
        self.assertIn("- read_file: Read a file from disk.", prompt)
        self.assertIn("path (string, required): Absolute path", prompt)
        self.assertIn('mode ("text" | "binary")', prompt)
        self.assertIn('<tool_call>{"name": "read_file", "arguments": {"path": "/a.txt"}}</tool_call>', prompt)
        self.assertIn('<tool_result name="read_file" id="call_1">\nhello world\n</tool_result>', prompt)
        self.assertTrue(prompt.endswith("Assistant:"))
        self.assertLess(len(prompt), 2500)  # the 2,000-char description was trimmed

    def test_required_choice(self):
        prompt = build_prompt([ChatMessage(role="user", content="x")], TOOLS, "required")
        self.assertIn("You must call at least one tool", prompt)


class ParseTests(unittest.TestCase):
    def test_plain_answer(self):
        r = parse_reply("Just an answer.", TOOLS)
        self.assertEqual((r.content, r.tool_calls, r.error), ("Just an answer.", [], None))

    def test_text_then_multiple_calls(self):
        r = parse_reply("Let me look.\n" + call("read_file", path="/a") + "\n" + call("bash", command="ls"), TOOLS)
        self.assertEqual(r.content, "Let me look.")
        self.assertEqual([c["function"]["name"] for c in r.tool_calls], ["read_file", "bash"])
        self.assertEqual(json.loads(r.tool_calls[0]["function"]["arguments"]), {"path": "/a"})
        self.assertTrue(r.tool_calls[0]["id"].startswith("call_"))

    def test_html_escaped_and_fenced_and_unclosed(self):
        escaped = '&lt;tool_call&gt;{"name": "bash", "arguments": {"command": "ls"}}&lt;/tool_call&gt;'
        fenced = '```tool_call\n{"name": "bash", "arguments": {"command": "ls"}}\n```'
        unclosed = '<tool_call>{"name": "bash", "arguments": {"command": "ls"}}'
        inner_fence = '<tool_call>\n```json\n{"name": "bash", "arguments": {"command": "ls"}}\n```\n</tool_call>'
        for text in (escaped, fenced, unclosed, inner_fence):
            r = parse_reply(text, TOOLS)
            self.assertEqual(len(r.tool_calls), 1, text)
            self.assertEqual(r.content, "", text)

    def test_bare_json_reply_and_other_shapes(self):
        r = parse_reply('```json\n{"tool": "bash", "parameters": {"command": "pwd"}}\n```', TOOLS)
        self.assertEqual(json.loads(r.tool_calls[0]["function"]["arguments"]), {"command": "pwd"})
        r = parse_reply('<tool_call>{"function": {"name": "bash", "arguments": "{\\"command\\": \\"pwd\\"}"}}</tool_call>', TOOLS)
        self.assertEqual(json.loads(r.tool_calls[0]["function"]["arguments"]), {"command": "pwd"})

    def test_markdown_escaped_underscore(self):
        r = parse_reply('<tool_call>{"name": "read\\_file", "arguments": {"path": "/a"}}</tool_call> done', TOOLS)
        self.assertEqual(r.tool_calls[0]["function"]["name"], "read_file")
        self.assertEqual(r.content, "done")

    def test_errors(self):
        self.assertIn("unknown tool", parse_reply(call("rm_rf", x=1), TOOLS).error)
        self.assertIn("missing required", parse_reply(call("read_file", limit=3), TOOLS).error)
        self.assertIn("did not contain", parse_reply("<tool_call>oops</tool_call>", TOOLS).error)

    def test_json_in_normal_answer_is_not_a_call(self):
        text = 'Here is config:\n```json\n{"name": "x"}\n```\nThanks.'
        self.assertEqual(parse_reply(text, TOOLS).tool_calls, [])


class HoldbackTests(unittest.TestCase):
    def test_holdback(self):
        self.assertEqual(holdback("hello"), 5)
        self.assertEqual(holdback("hi <tool_call>{"), 3)
        self.assertEqual(holdback("hi <tool_c"), 3)  # partial marker held back
        self.assertEqual(holdback("code: ``"), 6)
        self.assertEqual(holdback("a < b"), 5)


class FakeStream:
    def __init__(self, text, conversation_id="conv-1"):
        self._text = text
        self.conversation_id = conversation_id

    def __iter__(self):
        for i in range(0, len(self._text), 7):
            yield self._text[i:i + 7]


class FakeClient:
    """Stands in for CopilotClient: replays scripted replies in order."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def _next(self, prompt, conversation_id):
        self.prompts.append((prompt, conversation_id))
        return self.replies.pop(0)

    def chat(self, prompt, conversation_id=None):
        text = self._next(prompt, conversation_id)
        return type("Reply", (), {"text": text, "conversation_id": "conv-1"})()

    def stream(self, prompt, conversation_id=None):
        return FakeStream(self._next(prompt, conversation_id))


def request(stream, **extra):
    return {"messages": [{"role": "user", "content": "list files"}], "tools": TOOLS,
            "stream": stream, **extra}


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.http = TestClient(api.app)
        limiter = patch.object(api._rate_limiter, "try_acquire", return_value=(True, 0.0))
        limiter.start()
        self.addCleanup(limiter.stop)

    def post(self, fake, body):
        with patch.object(api, "client", fake):
            return self.http.post("/v1/chat/completions", json=body)

    def test_non_stream_tool_call(self):
        r = self.post(FakeClient("Sure. " + call("bash", command="ls")), request(False)).json()
        choice = r["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(choice["message"]["content"], "Sure.")
        self.assertEqual(choice["message"]["tool_calls"][0]["function"]["name"], "bash")

    def test_non_stream_correction_retry(self):
        fake = FakeClient(call("bsh", command="ls"), call("bash", command="ls"))
        r = self.post(fake, request(False)).json()
        self.assertEqual(r["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "bash")
        self.assertIn("unknown tool 'bsh'", fake.prompts[1][0])
        self.assertEqual(fake.prompts[1][1], "conv-1")  # retried in the same conversation

    def test_stream_text_then_tool_call(self):
        r = self.post(FakeClient("Checking the folder now. " + call("bash", command="ls")), request(True))
        chunks = [json.loads(line[6:]) for line in r.text.splitlines()
                  if line.startswith("data: {")]
        text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
        calls = [c["choices"][0]["delta"]["tool_calls"] for c in chunks
                 if "tool_calls" in c["choices"][0]["delta"]]
        self.assertEqual(text, "Checking the folder now. ")
        self.assertEqual(json.loads(calls[0][0]["function"]["arguments"]), {"command": "ls"})
        self.assertEqual(calls[0][0]["index"], 0)
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "tool_calls")

    def test_stream_plain_answer_with_code(self):
        answer = "Use this:\n```bash\nls -la\n```\nDone."
        r = self.post(FakeClient(answer), request(True))
        chunks = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: {")]
        text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
        self.assertEqual(text, answer)
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")

    def test_tool_choice_none_skips_tools(self):
        fake = FakeClient("plain")
        self.post(fake, request(False, tool_choice="none"))
        self.assertNotIn("<tool_call>", fake.prompts[0][0])


if __name__ == "__main__":
    unittest.main()
