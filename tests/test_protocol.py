"""Offline tests for the chat-socket protocol helpers (no network, no browser)."""

import base64
import json
import time
import unittest

from copilot.protocol import (
    CONVERSATION,
    DONE,
    ERROR,
    RS,
    TEXT,
    ReplyParser,
    build_turn,
    invocation_from_frame,
    jwt_expiry,
    token_from_url,
    token_is_fresh,
)


def rec(obj) -> str:
    return json.dumps(obj) + RS


def update(text, message_type=None, author="bot", mid="m1"):
    msg = {"author": author, "text": text, "messageId": mid}
    if message_type:
        msg["messageType"] = message_type
    return rec({"type": 1, "target": "update", "arguments": [{"messages": [msg]}]})


TEMPLATE = {
    "type": 4,
    "target": "chat",
    "invocationId": "0",
    "arguments": [{
        "source": "officeweb",
        "conversationId": "old-conv-0001",
        "isStartOfSession": True,
        "traceId": "old-trace-0001",
        "message": {"author": "user", "text": "warm-up", "requestId": "old-req-0001",
                    "timestamp": "2026-01-01T00:00:00+00:00"},
    }],
}


URL = ("wss://substrate.office.com/m365Copilot/Chathub/a@b?ClientRequestId=r1"
       "&access_token=OLD&source=%22officeweb%22")


def fake_jwt(exp: float) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{body}.sig"


class ReplyParserTests(unittest.TestCase):
    def test_cumulative_updates_become_deltas(self):
        p = ReplyParser()
        events = p.feed(update("Hel") + update("Hello") + update("Hello there"))
        self.assertEqual([e for e in events if e[0] == TEXT],
                         [(TEXT, "Hel"), (TEXT, "lo"), (TEXT, " there")])

    def test_ignores_user_and_status_messages(self):
        p = ReplyParser()
        events = p.feed(update("me", author="user") + update("Searching...", "InternalSearchQuery", mid="s")
                        + update("Answer"))
        self.assertEqual([e for e in events if e[0] == TEXT], [(TEXT, "Answer")])

    def test_partial_records_are_buffered(self):
        p = ReplyParser()
        frame = update("Hi")
        self.assertEqual(p.feed(frame[:10]), [])
        self.assertEqual(p.feed(frame[10:]), [(TEXT, "Hi")])

    def test_final_item_flushes_remainder_and_completion_ends(self):
        p = ReplyParser()
        p.feed(update("Hello"))
        final = rec({"type": 2, "invocationId": "0", "item": {
            "conversationId": "c1",
            "messages": [{"author": "bot", "text": "Hello world", "messageId": "m1"}],
            "result": {"value": "Success"}}})
        events = p.feed(final + rec({"type": 3, "invocationId": "0"}))
        self.assertEqual(events, [(CONVERSATION, "c1"), (TEXT, " world"), (DONE, None)])

    def test_error_result(self):
        p = ReplyParser()
        events = p.feed(rec({"type": 2, "item": {"result": {"value": "Throttled", "message": "slow"}}}))
        self.assertEqual(events[-1][0], ERROR)
        self.assertIn("throttled", events[-1][1].lower())

    def test_pings_and_handshake_ignored(self):
        self.assertEqual(ReplyParser().feed("{}" + RS + rec({"type": 6})), [])


class RequestTests(unittest.TestCase):
    def test_build_turn_fills_prompt_and_refreshes_ids(self):
        url, out = build_turn(TEMPLATE, URL, "NEW", "new prompt", "conv-9",
                              start_of_session=False, invocation_id="3")
        self.assertTrue(out.endswith(RS))
        sent = json.loads(out[:-1])
        args = sent["arguments"][0]
        self.assertEqual(sent["invocationId"], "3")
        self.assertEqual(args["conversationId"], "conv-9")
        self.assertFalse(args["isStartOfSession"])
        self.assertEqual(args["message"]["text"], "new prompt")
        self.assertNotEqual(args["traceId"], "old-trace-0001")
        self.assertNotEqual(args["message"]["requestId"], "old-req-0001")
        self.assertEqual(TEMPLATE["arguments"][0]["message"]["text"], "warm-up")  # template untouched

    def test_build_turn_keeps_shared_ids_consistent(self):
        # The page reuses one id in the URL and body, and the conversation id
        # in a second field; the replay must keep each pair in agreement.
        template = json.loads(json.dumps(TEMPLATE))
        template["arguments"][0]["clientCorrelationId"] = "SHARED-ID-1234"
        template["arguments"][0]["threadId"] = "old-conv-0001"
        url = URL.replace("ClientRequestId=r1", "ClientRequestId=shared-id-1234")
        new_url, out = build_turn(template, url, "NEW", "p", "conv-9", start_of_session=True)
        args = json.loads(out[:-1])["arguments"][0]
        new_id = args["clientCorrelationId"]
        self.assertNotEqual(new_id.lower(), "shared-id-1234")
        self.assertIn(f"ClientRequestId={new_id}", new_url)
        self.assertEqual(args["threadId"], "conv-9")

    def test_build_turn_url_conversation_matches_body(self):
        # Seen live: the URL carried ConversationId from another socket than the
        # captured request, and M365 answered InvalidRequest until they matched.
        url = URL + "&ConversationId=other-socket-conv-0001&chatsessionid=old-req-0001"
        new_url, out = build_turn(TEMPLATE, url, "NEW", "p", "conv-9", start_of_session=True)
        args = json.loads(out[:-1])["arguments"][0]
        self.assertIn("ConversationId=conv-9", new_url)
        self.assertNotIn("other-socket-conv", new_url)
        self.assertEqual(args["conversationId"], "conv-9")
        # chatsessionid shared the body's requestId, so it gets the same new value.
        self.assertIn(f"chatsessionid={args['message']['requestId']}", new_url)

    def test_build_turn_url_swaps_token_and_request_id(self):
        self.assertEqual(token_from_url(URL), "OLD")
        new, _ = build_turn(TEMPLATE, URL, "NEW", "p", "c", start_of_session=True)
        self.assertEqual(token_from_url(new), "NEW")
        self.assertNotIn("ClientRequestId=r1", new)
        self.assertIn("source=%22officeweb%22", new)

    def test_invocation_from_sent_frame(self):
        self.assertEqual(invocation_from_frame(rec(TEMPLATE))["target"], "chat")
        self.assertIsNone(invocation_from_frame(rec({"type": 6})))
        self.assertIsNone(invocation_from_frame('{"protocol":"json","version":1}' + RS))

    def test_jwt_expiry_and_freshness(self):
        soon, later = time.time() + 60, time.time() + 3600
        self.assertAlmostEqual(jwt_expiry(fake_jwt(later)), later, places=0)
        self.assertTrue(token_is_fresh(fake_jwt(later), margin=300))
        self.assertFalse(token_is_fresh(fake_jwt(soon), margin=300))
        self.assertFalse(token_is_fresh("not-a-jwt", margin=0))


if __name__ == "__main__":
    unittest.main()
