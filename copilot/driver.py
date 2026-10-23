"""Direct mode: talk to the M365 Copilot chat socket without a browser.

Replays the request the web page sent during sign-in (see
:func:`copilot.protocol.build_invocation`) over a plain WebSocket, using the
captured socket URL and access token. Faster and lighter than driving the page,
but it only works while M365 Copilot accepts the captured request shape.
"""

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
    build_invocation,
    socket_url,
    split_records,
)

_PING_FRAME = '{"type":%d}' % PING + RS
_PING_EVERY = 15  # seconds; SignalR drops clients that stay silent ~30s


class DirectChat:
    """Chat over the captured socket. ``get_session(force_refresh)`` supplies auth."""

    def __init__(self, get_session: Callable[[bool], dict]):
        self._get_session = get_session

    def stream(self, prompt: str, conversation_id: Optional[str] = None) -> Iterator[Event]:
        session = self._get_session(False)
        start = conversation_id is None
        cid = conversation_id or str(uuid.uuid4())
        url = socket_url(session["socket_url"], session["access_token"])
        request = build_invocation(session["template"], prompt, cid, start_of_session=start)

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
            ws.send(HANDSHAKE)
            ack, _ = split_records(ws.recv(timeout=30))
            if ack and ack[0].get("error"):
                raise CopilotError(f"M365 Copilot refused the handshake: {ack[0]['error']}")
            ws.send(request)
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
                for event in parser.feed(data):
                    yield event
                    if event[0] in (DONE, ERROR):
                        return

