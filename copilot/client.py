"""High-level M365 Copilot client — the recommended entry point.

    from copilot import CopilotClient

    client = CopilotClient()                       # browser mode by default
    r = client.chat("Summarize my unread email from today.")
    print(r.text, r.conversation_id)

    r2 = client.chat("Only the ones from my manager.", r.conversation_id)

    for chunk in client.stream("Tell me a joke"):
        print(chunk, end="", flush=True)

Sign in once with ``python -m copilot login``. Two modes:

* ``"browser"`` (default): types into the real web app in a headless browser.
  Most robust; conversations last as long as the process.
* ``"direct"``: replays the captured chat request over a plain WebSocket. No
  browser per turn and conversations can be resumed by id later, but it depends
  on the captured request still being accepted (experimental).
"""

from dataclasses import dataclass
from typing import Iterator, Optional

from . import config
from .auth import ensure_session, signed_in_before
from .errors import AuthExpired, AuthRequired, CopilotError
from .protocol import CONVERSATION, DONE, ERROR, TEXT, Event


@dataclass
class ChatReply:
    """The full result of a :meth:`CopilotClient.chat` call."""

    text: str
    conversation_id: Optional[str]


class ChatStream:
    """Iterable of reply text chunks that also exposes the conversation id.

    ``conversation_id`` is known up front when continuing a conversation, and
    filled in during iteration when a new one is started.
    """

    def __init__(self, events: Iterator[Event], conversation_id: Optional[str]):
        self._events = events
        self.conversation_id = conversation_id

    def __iter__(self) -> Iterator[str]:
        for kind, payload in self._events:
            if kind == CONVERSATION:
                self.conversation_id = payload
            elif kind == TEXT:
                yield payload
            elif kind == ERROR:
                raise CopilotError(payload)
            elif kind == DONE:
                return


class CopilotClient:
    """One client, many conversations addressed by id.

    Parameters
    ----------
    mode:
        ``"browser"`` or ``"direct"``. Defaults to ``$COPILOT_MODE`` or ``"browser"``.
    headless:
        Browser mode only: hide the browser window. Set ``False`` to watch it work.
    interactive_login:
        Open a visible sign-in window when there's no usable session. Set
        ``False`` for servers, which then get :class:`AuthRequired` instead.
    """

    def __init__(
        self,
        mode: Optional[str] = None,
        headless: bool = True,
        interactive_login: bool = True,
    ):
        self.mode = (mode or config.DEFAULT_MODE).lower()
        if self.mode not in ("browser", "direct"):
            raise ValueError(f"mode must be 'browser' or 'direct', not {self.mode!r}")
        self._headless = headless
        self._interactive = interactive_login
        self._backend = None

    def stream(self, prompt: str, conversation_id: Optional[str] = None) -> ChatStream:
        """Stream the reply to ``prompt``. Omit ``conversation_id`` to start fresh."""
        return ChatStream(self._events(prompt, conversation_id), conversation_id)

    def chat(self, prompt: str, conversation_id: Optional[str] = None) -> ChatReply:
        """Return the full reply to ``prompt``."""
        s = self.stream(prompt, conversation_id)
        text = "".join(s)
        return ChatReply(text, s.conversation_id)

    def close(self) -> None:
        if self._backend is not None and hasattr(self._backend, "close"):
            self._backend.close()

    # -- internals ---------------------------------------------------------------

    def _events(self, prompt: str, conversation_id: Optional[str]) -> Iterator[Event]:
        backend = self._get_backend()
        if self.mode == "browser":
            yield from backend.stream(prompt, conversation_id)
            return

        # Direct mode: if the token is rejected before any output, refresh it once.
        produced = False
        try:
            for event in backend.stream(prompt, conversation_id):
                produced = produced or event[0] == TEXT
                yield event
        except AuthExpired:
            if produced:
                raise
            ensure_session(interactive=self._interactive, force_refresh=True)
            yield from backend.stream(prompt, conversation_id)

    def _get_backend(self):
        if self._backend is not None:
            return self._backend
        if not signed_in_before():
            if not self._interactive:
                raise AuthRequired("Not signed in. Run `python -m copilot login` first.")
            from .browser import login

            login()
        if self.mode == "browser":
            from .browser import BrowserChat

            self._backend = BrowserChat(headless=self._headless)
        else:
            from .driver import DirectChat

            interactive = self._interactive
            self._backend = DirectChat(
                lambda force: ensure_session(interactive=interactive, force_refresh=force)
            )
        return self._backend
