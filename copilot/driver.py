"""Direct mode: talk to the M365 Copilot chat socket without a browser.

Replays the request the web page sent during sign-in (see
:func:`copilot.protocol.build_turn`) over a plain WebSocket, using the
captured socket URL and access token. Faster and lighter than driving the page,
but it only works while M365 Copilot accepts the captured request shape.

Set ``COPILOT_DEBUG=1`` to log every frame (redacted) to
``session/direct_debug.log``, for comparing against what the browser sends
(``python tests/diagnostic.py`` writes that to ``session/ws_capture.log``).
"""

import os
import time
import uuid
from typing import Callable, Iterator, Optional

from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.sync.client import connect

from . import config
from .errors import AuthExpired, CopilotError
from .protocol import (
    CONVERSATION,
    DONE,
    ERROR,
    HANDSHAKE,
    PING,
    RS,
    Event,
    ReplyParser,
    build_turn,
    redact,
    split_records,
)

_PING_FRAME = '{"type":%d}' % PING + RS
_PING_EVERY = 15  # seconds; SignalR drops clients that stay silent ~30s
_DEBUG_LOG = f"{config.SESSION_DIR}/direct_debug.log"


def _debug(tag: str, payload) -> None:
    if not os.environ.get("COPILOT_DEBUG"):
        return
    if isinstance(payload, (bytes, bytearray)):
        payload = bytes(payload).decode("utf-8", "replace")
    try:
        with open(_DEBUG_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} {tag} {redact(str(payload))}\n")
    except OSError:
        pass


class DirectChat:
    """Chat over the captured socket. ``get_session(force_refresh)`` supplies auth."""

    def __init__(self, get_session: Callable[[bool], dict]):
        self._get_session = get_session

    def stream(self, prompt: str, conversation_id: Optional[str] = None) -> Iterator[Event]:
        session = self._get_session(False)
        start = conversation_id is None
        cid = conversation_id or str(uuid.uuid4())
        url, request = build_turn(
            session["template"], session["socket_url"], session["access_token"],
            prompt, cid, start_of_session=start,
        )
        _debug("OPEN", url)

        try:
            ws = connect(
                url,
                origin=session.get("origin"),
                user_agent_header=config.USER_AGENT,
                open_timeout=30,
                close_timeout=5,
                max_size=None,
            )
        except InvalidHandshake as exc:
            if any(code in str(exc) for code in ("401", "403")):
                raise AuthExpired(f"M365 Copilot rejected the access token: {exc}") from exc
            raise CopilotError(f"Could not open the M365 Copilot chat socket: {exc}") from exc

        with ws:
            try:
                ws.send(HANDSHAKE)
                raw_ack = ws.recv(timeout=30)
            except ConnectionClosed as exc:
                # Closed before the handshake finished: most often a stale token.
                # AuthExpired makes the client refresh it and retry once.
                _debug("CLOSE", exc)
                raise AuthExpired(f"M365 Copilot closed the chat socket during the handshake: {exc}") from exc
            _debug("<<", raw_ack)
            ack, _ = split_records(raw_ack)
            if ack and ack[0].get("error"):
                raise CopilotError(f"M365 Copilot refused the handshake: {ack[0]['error']}")
            ws.send(request)
            _debug(">>", request)
            if start:
                yield (CONVERSATION, cid)

            parser = ReplyParser()
            began = last_ping = last_frame = time.time()
            while True:
                now = time.time()
                if now - began > config.TURN_TIMEOUT:
                    yield (ERROR, f"M365 Copilot reply exceeded {config.TURN_TIMEOUT}s.")
                    return
                if now - last_ping > _PING_EVERY:
                    ws.send(_PING_FRAME)
                    last_ping = now
                try:
                    data = ws.recv(timeout=_PING_EVERY)
                except TimeoutError:
                    if time.time() - last_frame > config.IDLE_TIMEOUT:
                        yield (ERROR, f"No reply from M365 Copilot after {config.IDLE_TIMEOUT}s.")
                        return
                    continue
                except ConnectionClosed as exc:
                    yield (ERROR, f"M365 Copilot closed the chat socket: {exc}")
                    return
                last_frame = time.time()
                _debug("<<", data)
                for event in parser.feed(data):
                    yield event
                    if event[0] in (DONE, ERROR):
                        return

