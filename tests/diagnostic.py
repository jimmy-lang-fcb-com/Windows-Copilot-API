"""Capture what the M365 Copilot web app does on the wire — for debugging.

Opens your signed-in browser profile (visible). You send ONE message in the
chat; every frame on the chat socket (sent and received) is written, redacted,
to ``session/ws_capture.log``, along with the URL of every WebSocket the page
opens. A short report goes to ``session/diagnostic_report.txt``.

Use it when:
  * the bridge can't find the chat socket (check which socket URLs appear and
    set M365_CHATHUB_HINT), or the chat box (set M365_COMPOSER_SELECTORS);
  * replies come back empty (compare the received frames with
    copilot/protocol.py's ReplyParser);
  * direct mode is rejected (compare the sent `chat` request with the template
    in session/token.json).

Run::

    python tests/diagnostic.py              # browser capture + report
    python tests/diagnostic.py --no-browser # report only (headless machines)

Tokens, JWTs, emails and long opaque ids are redacted before anything is
written, but skim both files before sharing them outside IT.
"""

import argparse
import json
import platform
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from copilot import config  # noqa: E402
from copilot.auth import load_session  # noqa: E402
from copilot.protocol import jwt_expiry  # noqa: E402

SESSION_DIR = Path(config.SESSION_DIR)
WS_LOG = SESSION_DIR / "ws_capture.log"
REPORT = SESSION_DIR / "diagnostic_report.txt"
CAPTURE_SECONDS = 300

_REDACTORS = [
    (re.compile(r"(access_?token)=[^&\s\"]+", re.I), r"\1=<REDACTED>"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)?"), "<JWT>"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<EMAIL>"),
    (re.compile(r"[A-Za-z0-9_\-]{60,}"), "<REDACTED>"),
]


def redact(text: str) -> str:
    for pattern, repl in _REDACTORS:
        text = pattern.sub(repl, text)
    return text


def _text(payload) -> str:
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload).decode("utf-8", "replace")
    return str(payload)


def report_lines():
    yield f"python    : {platform.python_version()} on {platform.platform()}"
    for pkg in ("playwright", "websockets", "fastapi", "uvicorn"):
        try:
            from importlib.metadata import version

            yield f"{pkg:<10}: {version(pkg)}"
        except Exception:
            yield f"{pkg:<10}: not installed"
    yield f"chat url  : {config.CHAT_URL}"
    yield f"hub hint  : {config.CHATHUB_HINT}"
    yield f"mode      : {config.DEFAULT_MODE}"
    session = load_session()
    if not session:
        yield "session   : none (run `python -m copilot login`)"
        return
    exp = jwt_expiry(session.get("access_token"))
    left = f"{(exp - time.time()) / 60:.0f} min left" if exp else "unknown expiry"
    yield f"session   : saved {time.ctime(session.get('saved_at', 0))}; token {left}"
    yield f"socket    : {redact(session.get('socket_url') or 'none')}"
    template = session.get("template")
    if template:
        args = (template.get("arguments") or [{}])[0]
        yield f"template  : target={template.get('target')} type={template.get('type')} keys={sorted(args)}"
    else:
        yield "template  : none captured"


def browser_capture() -> list:
    from copilot.browser import BrowserSession

    lines = []
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    with WS_LOG.open("w", encoding="utf-8") as log, BrowserSession(headless=False) as browser:
        def write(tag: str, payload) -> None:
            log.write(f"{time.strftime('%H:%M:%S')} {tag} {redact(_text(payload))}\n")
            log.flush()

        def on_socket(ws) -> None:
            is_chat = config.CHATHUB_HINT in ws.url.lower()
            write("OPEN" + (" [chat]" if is_chat else ""), ws.url)
            lines.append(("chat socket: " if is_chat else "other socket: ") + redact(ws.url))
            if is_chat:
                ws.on("framesent", lambda p: write(">>", p))
                ws.on("framereceived", lambda p: write("<<", p))
                ws.on("close", lambda _: write("CLOSE", ws.url))

        chat = browser.new_page()
        chat.page.on("websocket", on_socket)
        print("\nSign in if asked, then send ONE short message in the chat.\n"
              "Close the window when the reply has finished (or wait 5 minutes).\n")
        deadline = time.time() + CAPTURE_SECONDS
        while time.time() < deadline and not chat.closed():
            try:
                chat.page.wait_for_timeout(500)
            except Exception:
                break
        lines.append(f"composer found: {'yes' if chat.composer() is not None else 'no'}"
                     if not chat.closed() else "window closed by user")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-browser", action="store_true", help="report only")
    args = parser.parse_args()

    out = list(report_lines())
    if not args.no_browser:
        out.append("")
        out.extend(browser_capture())
        out.append(f"frames written to {WS_LOG}")
    text = "\n".join(out) + "\n"
    print(text)
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(text, encoding="utf-8")
    print(f"Report saved to {REPORT}")


if __name__ == "__main__":
    main()
