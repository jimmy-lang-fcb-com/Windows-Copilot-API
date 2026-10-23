"""Tests for continuing one M365 conversation across a chat session."""

import itertools
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from copilot import CopilotError
from server import api
from server.conversations import ConversationCache, tools_scope
from tests.test_tools import TOOLS, call


class ScriptedStream:
    def __init__(self, reply, conversation_id):
        self.reply = reply
        self.conversation_id = conversation_id

    def __iter__(self):
        if isinstance(self.reply, Exception):
            raise self.reply
        for i in range(0, len(self.reply), 5):
            yield self.reply[i:i + 5]


class ScriptedClient:
    """Fake CopilotClient. New conversations get ids conv-1, conv-2, ..."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.sent = []  # (prompt, conversation_id)
        self._ids = (f"conv-{n}" for n in itertools.count(1))

    def stream(self, prompt, conversation_id=None):
        self.sent.append((prompt, conversation_id))
        return ScriptedStream(self.replies.pop(0), conversation_id or next(self._ids))

    def chat(self, prompt, conversation_id=None):
        s = self.stream(prompt, conversation_id)
        return type("Reply", (), {"text": "".join(s), "conversation_id": s.conversation_id})()


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.http = TestClient(api.app)
        for patcher in (
            patch.object(api._rate_limiter, "try_acquire", return_value=(True, 0.0)),
            patch.object(api, "_conversations", ConversationCache()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, fake, **body):
        with patch.object(api, "client", fake):
            return self.http.post("/v1/chat/completions", json=body).json()

    def test_agent_loop_continues_one_conversation(self):
        fake = ScriptedClient(call("bash", command="ls"), "There are 2 files.")
        system = {"role": "system", "content": "You are an agent. Date: Monday"}
        user = {"role": "user", "content": "list files"}
        first = self.post(fake, messages=[system, user], tools=TOOLS)
        assistant = first["choices"][0]["message"]
        call_id = assistant["tool_calls"][0]["id"]

        # The client echoes the history, adds the tool result, and regenerates
        # its system prompt (a changed date must not break reuse).
        second = self.post(fake, tools=TOOLS, messages=[
            {"role": "system", "content": "You are an agent. Date: Tuesday"},
            user, assistant,
            {"role": "tool", "tool_call_id": call_id, "content": "a.txt\nb.txt"},
        ])
        self.assertEqual(second["choices"][0]["message"]["content"], "There are 2 files.")
        prompt, conversation_id = fake.sent[1]
        self.assertEqual(conversation_id, "conv-1")
        self.assertIn('<tool_result name="bash"', prompt)
        self.assertNotIn("# Tools", prompt)          # instructions not resent
        self.assertNotIn("list files", prompt)       # old history not resent
        self.assertIn("Tools available: read_file, bash", prompt)

    def test_plain_chat_sends_only_new_message(self):
        fake = ScriptedClient("Hello!", "Sure, more.")
        user = {"role": "user", "content": "hi"}
        first = self.post(fake, messages=[user])
        self.post(fake, messages=[user, first["choices"][0]["message"],
                                  {"role": "user", "content": "tell me more"}])
        self.assertEqual(fake.sent[1], ("tell me more", "conv-1"))

    def test_edited_history_starts_fresh(self):
        fake = ScriptedClient("Hello!", "Fresh.")
        self.post(fake, messages=[{"role": "user", "content": "hi"}])
        self.post(fake, messages=[{"role": "user", "content": "hi"},
                                  {"role": "assistant", "content": "something else"},
                                  {"role": "user", "content": "more"}])
        self.assertIsNone(fake.sent[1][1])

    def test_failed_continuation_falls_back_to_full_history(self):
        fake = ScriptedClient("Hello!", CopilotError("Unknown conversation_id"), "Recovered.")
        user = {"role": "user", "content": "hi"}
        first = self.post(fake, messages=[user])
        second = self.post(fake, messages=[user, first["choices"][0]["message"],
                                           {"role": "user", "content": "more"}])
        self.assertEqual(second["choices"][0]["message"]["content"], "Recovered.")
        self.assertEqual(fake.sent[1][1], "conv-1")      # tried to continue
        self.assertIsNone(fake.sent[2][1])               # then a fresh conversation
        self.assertIn("User: hi", fake.sent[2][0])       # with the whole history

    def test_explicit_conversation_id_bypasses_cache(self):
        fake = ScriptedClient("ok")
        self.post(fake, messages=[{"role": "user", "content": "x"}], conversation_id="mine")
        self.assertEqual(fake.sent[0], ("x", "mine"))

    def test_usage_estimates(self):
        fake = ScriptedClient("A reply of some length.", "streamed reply")
        r = self.post(fake, messages=[{"role": "user", "content": "hello there " * 20}])
        u = r["usage"]
        self.assertGreater(u["prompt_tokens"], 50)
        self.assertEqual(u["completion_tokens"], 6)
        self.assertEqual(u["total_tokens"], u["prompt_tokens"] + u["completion_tokens"])

        with patch.object(api, "client", fake):
            text = self.http.post("/v1/chat/completions", json={
                "messages": [{"role": "user", "content": "q"}], "stream": True}).text
        chunks = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: {")]
        self.assertEqual(chunks[-1]["usage"]["completion_tokens"], 4)


class CacheTests(unittest.TestCase):
    def test_ttl_and_lru(self):
        now = [0.0]
        cache = ConversationCache(max_entries=2, ttl=100, clock=lambda: now[0])
        msgs = lambda n: [{"role": "user", "content": f"m{i}"} for i in range(n)]  # noqa: E731
        reply = {"role": "assistant", "content": "r"}
        cache.remember(msgs(1), reply, "", "a")
        self.assertEqual(cache.lookup(msgs(1) + [reply, {"role": "user", "content": "x"}], ""), ("a", 2))
        now[0] = 101
        self.assertIsNone(cache.lookup(msgs(1) + [reply, {"role": "user", "content": "x"}], ""))
        for name in "bcd":
            cache.remember([{"role": "user", "content": name}], reply, "", name)
        self.assertEqual(len(cache._entries), 2)

    def test_scope_separates_tool_sets(self):
        cache = ConversationCache()
        reply = {"role": "assistant", "content": "r"}
        history = [{"role": "user", "content": "q"}]
        cache.remember(history, reply, tools_scope(TOOLS), "with-tools")
        later = history + [reply, {"role": "user", "content": "next"}]
        self.assertEqual(cache.lookup(later, tools_scope(TOOLS))[0], "with-tools")
        self.assertIsNone(cache.lookup(later, tools_scope([])))


if __name__ == "__main__":
    unittest.main()
