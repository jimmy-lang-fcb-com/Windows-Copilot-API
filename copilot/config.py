"""Settings for the M365 Copilot bridge, in one place.

Everything tenant- or deployment-specific can be overridden with an environment
variable, so the code doesn't need editing to point it at a different host.
"""

import os

# The M365 Copilot web app. Older entry points (microsoft365.com/chat,
# copilot.cloud.microsoft) redirect here.
CHAT_URL = os.environ.get("M365_COPILOT_URL", "https://m365.cloud.microsoft/chat")

# Substring (case-insensitive) that identifies the chat WebSocket among the many
# sockets the page opens. Today it is
#   wss://substrate.office.com/m365Copilot/Chathub/<oid>@<tid>?...&access_token=...
CHATHUB_HINT = os.environ.get("M365_CHATHUB_HINT", "chathub").lower()

# Where the composer lives. Tried in order; the first visible match wins.
COMPOSER_SELECTORS = [
    s.strip()
    for s in os.environ.get(
        "M365_COMPOSER_SELECTORS",
        "#m365-chat-editor-target-element,"
        "[role='textbox'][contenteditable='true'],"
        "div[contenteditable='true'],"
        "textarea",
    ).split(",")
    if s.strip()
]

# Send button, clicked if pressing Enter didn't send the prompt.
SEND_BUTTON_SELECTORS = [
    s.strip()
    for s in os.environ.get(
        "M365_SEND_SELECTORS",
        "button[aria-label='Send'],"
        "button[aria-label*='Send' i],"
        "button[title*='Send' i],"
        "button[type='submit']",
    ).split(",")
    if s.strip()
]

# Playwright browser channel: "chromium" (default; new headless mode that looks
# like normal Chrome), "msedge" (installed Edge), or "" for Playwright's default
# headless shell.
BROWSER_CHANNEL = os.environ.get("M365_BROWSER_CHANNEL", "chromium")

# Session state: persistent browser profile + captured chat session. Secret.
SESSION_DIR = os.environ.get("M365_SESSION_DIR", "session")
PROFILE_DIR = f"{SESSION_DIR}/profile"
SESSION_FILE = f"{SESSION_DIR}/token.json"

# "browser" drives the real web page (default, most robust).
# "direct" replays the captured WebSocket request without a browser (faster,
# experimental: depends on the captured request template still being accepted).
DEFAULT_MODE = os.environ.get("COPILOT_MODE", "browser").lower()

# Message sent to make the page open its chat socket so the token and request
# template can be captured. It appears in your Copilot chat history.
WARMUP_PROMPT = os.environ.get("M365_WARMUP_PROMPT", "Reply with the single word: ready")

# Refresh the access token this many seconds before its JWT `exp`.
TOKEN_REFRESH_MARGIN = 5 * 60

# Desktop Chrome UA for the direct socket (no Cloudflare here, so it only needs
# to look like an ordinary browser).
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

# Timeouts (seconds).
TURN_TIMEOUT = 600   # whole reply
IDLE_TIMEOUT = 90    # silence between frames before giving up
SEND_RETRY_AFTER = 8 # no chat request yet -> click the Send button
SEND_TIMEOUT = 25    # no chat request yet -> give up, save a screenshot
