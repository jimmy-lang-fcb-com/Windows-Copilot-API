"""Playwright side of the bridge: sign-in, session capture, and browser chat.

The browser runs on a **persistent** Chromium profile under ``session/profile``
so the Microsoft Entra sign-in (and MFA "stay signed in") survives restarts.

Three pieces:

* :class:`ChatPage` wraps one tab of the M365 Copilot web app. It watches the
  page's own chat WebSocket, records the socket URL (which carries the access
  token) and the ``chat`` request the page sends, and parses the reply frames.
* :func:`login` / :func:`capture_session` sign in and snapshot what the direct
  driver needs into ``session/token.json``.
* :class:`BrowserChat` chats by typing into the real page. Playwright's sync API
  must stay on the thread that started it, so a single worker thread owns the
  browser and callers on any thread hand it jobs through a queue.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
import uuid
from collections import OrderedDict
from typing import Iterator, List, Optional
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from . import config
from .errors import AuthRequired
from .protocol import (
    CONVERSATION,
    DONE,
    ERROR,
    Event,
    ReplyParser,
    invocation_from_frame,
    jwt_expiry,
    token_from_url,
)


def _status(msg: str) -> None:
    print(f"[copilot] {msg}", file=sys.stderr, flush=True)


class ChatPage:
    """One M365 Copilot tab, plus everything learned from its chat socket."""

    def __init__(self, page, chat_url: str = config.CHAT_URL):
        self.page = page
        self.chat_url = chat_url
        self.socket_url: Optional[str] = None
        self.template: Optional[dict] = None
        self.conversation_id: Optional[str] = None
        self._sent = False  # the page sent a `chat` request this turn
        self._parser: Optional[ReplyParser] = None
        self._pending: List[Event] = []
        self._last_frame = 0.0
        page.on("websocket", self._on_socket)

    # -- socket capture --------------------------------------------------------

    def _on_socket(self, ws) -> None:
        if config.CHATHUB_HINT not in ws.url.lower():
            return  # telemetry, presence, etc.
        if token_from_url(ws.url):
            self.socket_url = ws.url
        ws.on("framesent", self._on_sent)
        ws.on("framereceived", self._on_received)

    def _on_sent(self, payload) -> None:
        invocation = invocation_from_frame(payload)
        if invocation is None:
            return
        self.template = invocation
        self._sent = True
        cid = (invocation.get("arguments") or [{}])[0].get("conversationId")
        if cid:
            self.conversation_id = cid

    def _on_received(self, payload) -> None:
        self._last_frame = time.time()
        if self._parser is not None:
            self._pending.extend(self._parser.feed(payload))

    # -- page interaction ------------------------------------------------------

    def goto(self) -> None:
        self.page.goto(self.chat_url, wait_until="domcontentloaded")

    def on_chat_host(self) -> bool:
        try:
            return urlparse(self.page.url).netloc == urlparse(self.chat_url).netloc
        except Exception:
            return False

    def composer(self):
        """The visible chat input box, or ``None``."""
        for selector in config.COMPOSER_SELECTORS:
            try:
                box = self.page.locator(selector).first
                if box.is_visible():
                    return box
            except PlaywrightError:
                continue
        return None

    def ready(self) -> bool:
        """Signed in and the chat composer is on screen."""
        return self.on_chat_host() and self.composer() is not None

    def wait_ready(self, timeout: float, settle_ms: int = 0) -> bool:
        """Wait for the chat box. ``settle_ms`` then gives the app time to finish
        wiring up its handlers: the box can render before Enter actually sends."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.closed():
                return False
            if self.ready():
                if settle_ms:
                    self.page.wait_for_timeout(settle_ms)
                return True
            self.page.wait_for_timeout(500)
        return False

    def click_send(self) -> bool:
        """Click the Send button, if one is visible."""
        for selector in config.SEND_BUTTON_SELECTORS:
            try:
                button = self.page.locator(selector).first
                if button.is_visible() and button.is_enabled():
                    button.click()
                    return True
            except PlaywrightError:
                continue
        return False

    def screenshot(self) -> Optional[str]:
        """Save a screenshot for debugging; returns its path."""
        path = f"{config.SESSION_DIR}/last_error.png"
        try:
            self.page.screenshot(path=path, full_page=True)
            return path
        except Exception:
            return None

    def closed(self) -> bool:
        try:
            return self.page.is_closed()
        except Exception:
            return True

    def turn(
        self,
        prompt: str,
        turn_timeout: float = config.TURN_TIMEOUT,
        idle_timeout: float = config.IDLE_TIMEOUT,
    ) -> Iterator[Event]:
        """Type ``prompt`` into the page and yield the reply as events."""
        box = self.composer()
        if box is None:
            yield (ERROR, "M365 Copilot chat box not found. The page layout may have changed; "
                          "set M365_COMPOSER_SELECTORS or run `python tests/diagnostic.py`.")
            return
        self._parser = ReplyParser()
        self._pending = []
        self._sent = False
        announced = self.conversation_id
        box.click()
        self.page.keyboard.insert_text(prompt)  # insert, not type: newlines would send early
        self.page.keyboard.press("Enter")

        started = self._last_frame = time.time()
        clicked_send = False
        try:
            while True:
                self.page.wait_for_timeout(100)  # lets Playwright deliver socket events
                waited = time.time() - started
                if not self._sent and not clicked_send and waited > config.SEND_RETRY_AFTER:
                    clicked_send = True  # Enter didn't send; try the button once
                    if self.click_send():
                        _status("Enter didn't send the prompt; clicked Send instead.")
                if not self._sent and waited > config.SEND_TIMEOUT:
                    shot = self.screenshot()
                    yield (ERROR, f"The prompt was never sent (no chat request after {config.SEND_TIMEOUT:.0f}s). "
                                  f"Page: {self.page.url}. "
                                  + (f"Screenshot: {shot}. " if shot else "")
                                  + "Try `python -m copilot ask --show ...` to watch it.")
                    return
                if self.conversation_id and self.conversation_id != announced:
                    announced = self.conversation_id
                    yield (CONVERSATION, announced)
                events, self._pending = self._pending, []
                for event in events:
                    yield event
                    if event[0] in (DONE, ERROR):
                        return
                now = time.time()
                if now - self._last_frame > idle_timeout:
                    hint = ("the page never opened its chat socket"
                            if self.socket_url is None else "the chat socket went quiet")
                    yield (ERROR, f"No reply from M365 Copilot after {idle_timeout:.0f}s ({hint}). "
                                  "Run `python tests/diagnostic.py` to capture what the page is doing.")
                    return
                if now - started > turn_timeout:
                    yield (ERROR, f"M365 Copilot reply exceeded {turn_timeout:.0f}s.")
                    return
        finally:
            self._parser = None


class BrowserSession:
    """A persistent Chromium context on the session profile."""

    def __init__(self, headless: bool = True, profile_dir: str = config.PROFILE_DIR):
        self.headless = headless
        self.profile_dir = profile_dir
        self._pw = None
        self._context = None

    def start(self) -> "BrowserSession":
        if self._context is not None:
            return self
        try:
            self._pw = sync_playwright().start()
            kwargs = dict(headless=self.headless, viewport={"width": 1280, "height": 900})
            if config.BROWSER_CHANNEL:
                # "chromium" = the full browser's new headless mode, which presents
                # as ordinary Chrome; the default headless shell says
                # "HeadlessChrome", which web apps can treat differently.
                # "msedge" uses the installed Edge.
                kwargs["channel"] = config.BROWSER_CHANNEL
            self._context = self._pw.chromium.launch_persistent_context(self.profile_dir, **kwargs)
        except PlaywrightError as exc:
            self.close()
            raise ConnectionError(f"Failed to start browser: {exc}") from exc
        return self

    def new_page(self) -> ChatPage:
        """Open M365 Copilot in a tab (reusing the blank first tab if there is one)."""
        self.start()
        blank = [p for p in self._context.pages if p.url in ("about:blank", "")]
        page = blank[0] if blank else self._context.new_page()
        page.set_default_timeout(60_000)
        chat = ChatPage(page)
        chat.goto()
        return chat

    def close(self) -> None:
        for closer in (lambda: self._context and self._context.close(),
                       lambda: self._pw and self._pw.stop()):
            try:
                closer()
            except Exception:
                pass
        self._context = None
        self._pw = None

    def __enter__(self) -> "BrowserSession":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()


# -- session capture ------------------------------------------------------------


def capture_session(chat: ChatPage, previous: Optional[dict] = None, socket_wait: float = 8) -> dict:
    """Snapshot what the direct driver needs from a signed-in page.

    Waits briefly in case the page opens its chat socket on load. If that doesn't
    yield a token, or we have never captured a request template, sends one
    warm-up message so the page opens the socket and shows us a real request.
    """
    deadline = time.time() + socket_wait
    while chat.socket_url is None and time.time() < deadline:
        chat.page.wait_for_timeout(500)

    template = chat.template or (previous or {}).get("template")
    if chat.socket_url is None or template is None:
        _status("Sending a warm-up message to capture the chat session...")
        for kind, payload in chat.turn(config.WARMUP_PROMPT, turn_timeout=120):
            if kind == ERROR:
                _status(f"warm-up: {payload}")
        template = chat.template or template

    if chat.socket_url is None:
        raise AuthRequired(
            "Could not capture the M365 Copilot chat socket. Run `python tests/diagnostic.py` "
            "to see which sockets the page opens (and set M365_CHATHUB_HINT if needed)."
        )
    token = token_from_url(chat.socket_url)
    page_url = urlparse(chat.page.url)
    return {
        "socket_url": chat.socket_url,
        "access_token": token,
        "expires_at": jwt_expiry(token),
        "template": template,
        "origin": f"{page_url.scheme}://{page_url.netloc}",
        "saved_at": time.time(),
    }


def login(timeout: float = 300) -> dict:
    """Interactive sign-in in a visible window; saves and returns the session."""
    from .auth import save_session

    with BrowserSession(headless=False) as browser:
        chat = browser.new_page()
        print(
            "\nA browser window is open at M365 Copilot.\n"
            "Sign in with your work account (and complete MFA).\n"
            "It continues by itself once the chat box appears.\n"
        )
        if not chat.wait_ready(timeout):
            raise AuthRequired("Sign-in was not completed (the chat box never appeared).")
        print("Signed in. Capturing the chat session...")
        session = capture_session(chat)
    save_session(session)
    print(f"Session saved to {config.SESSION_DIR}/")
    return session


def refresh_session(previous: Optional[dict] = None, signin_grace: float = 30) -> dict:
    """Headless refresh using the saved profile. Raises AuthRequired if signed out."""
    from .auth import save_session

    with BrowserSession(headless=True) as browser:
        chat = browser.new_page()
        if not chat.wait_ready(signin_grace):
            raise AuthRequired("The saved browser profile is signed out. Run `python -m copilot login`.")
        session = capture_session(chat, previous=previous)
    save_session(session)
    return session


# -- browser chat -----------------------------------------------------------------

_RAISE = "raise"  # internal event: payload is an exception to re-raise in the caller


class BrowserChat:
    """Chat by driving the real web page, from any thread.

    Each conversation lives in its own tab, kept open so follow-up messages land
    in the same thread. Conversations therefore last as long as this process; at
    most ``max_pages`` stay open (the oldest is closed first).
    """

    def __init__(self, headless: bool = True, max_pages: int = 6):
        self.headless = headless
        self.max_pages = max_pages
        self._jobs: "queue.Queue" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def stream(self, prompt: str, conversation_id: Optional[str] = None) -> Iterator[Event]:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._worker, name="m365-browser", daemon=True)
                self._thread.start()
        out: "queue.Queue" = queue.Queue()
        self._jobs.put((prompt, conversation_id, out))
        while True:
            kind, payload = out.get()
            if kind == _RAISE:
                raise payload
            yield (kind, payload)
            if kind in (DONE, ERROR):
                return

    def close(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._jobs.put(None)
            self._thread.join(timeout=10)

    # -- worker thread ------------------------------------------------------------

    def _worker(self) -> None:
        browser: Optional[BrowserSession] = None
        pages: "OrderedDict[str, ChatPage]" = OrderedDict()
        while True:
            job = self._jobs.get()
            if job is None:
                break
            prompt, conversation_id, out = job
            try:
                if browser is None:
                    browser = BrowserSession(headless=self.headless).start()
                self._run_turn(browser, pages, prompt, conversation_id, out)
            except AuthRequired as exc:
                # Release the profile so `python -m copilot login` can use it.
                browser = self._reset(browser, pages)
                out.put((_RAISE, exc))
            except Exception as exc:  # browser crashed, page closed, ...
                browser = self._reset(browser, pages)
                out.put((ERROR, f"Browser error: {exc}"))
        self._reset(browser, pages)

    def _run_turn(self, browser, pages, prompt, conversation_id, out) -> None:
        if conversation_id is not None:
            chat = pages.get(conversation_id)
            if chat is None or chat.closed():
                pages.pop(conversation_id, None)
                out.put((ERROR, f"Unknown conversation_id {conversation_id!r}. In browser mode, "
                                "conversations last only while this process runs."))
                return
            pages.move_to_end(conversation_id)
        else:
            chat = browser.new_page()
            if not chat.wait_ready(45, settle_ms=3000):
                raise AuthRequired("M365 Copilot is not signed in. Run `python -m copilot login`.")

        announced = conversation_id
        for kind, payload in chat.turn(prompt):
            if kind == CONVERSATION:
                announced = payload
            if kind in (DONE, ERROR) and conversation_id is None:
                # Register the tab before the caller sees the end of the turn.
                if announced is None:
                    announced = f"local-{uuid.uuid4()}"
                    out.put((CONVERSATION, announced))
                pages[announced] = chat
                while len(pages) > self.max_pages:
                    _, oldest = pages.popitem(last=False)
                    try:
                        oldest.page.close()
                    except Exception:
                        pass
            out.put((kind, payload))
            if kind in (DONE, ERROR):
                return

    @staticmethod
    def _reset(browser, pages) -> None:
        pages.clear()
        if browser is not None:
            browser.close()
        return None
