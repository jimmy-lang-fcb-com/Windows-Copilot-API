"""The saved chat session (``session/token.json``) used by direct mode.

It holds what was captured off the page's own chat socket during sign-in: the
socket URL, the access token in it, the token's expiry, and the ``chat`` request
template. When the token is close to expiring it is refreshed from the saved
browser profile, headlessly.
"""

import json
from pathlib import Path
from typing import Optional

from . import config
from .errors import AuthRequired
from .protocol import token_is_fresh


def load_session(path: str = config.SESSION_FILE) -> Optional[dict]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_session(session: dict, path: str = config.SESSION_FILE) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(session, indent=2), encoding="utf-8")
    try:
        p.chmod(0o600)  # it contains a bearer token
    except OSError:
        pass


def signed_in_before(path: str = config.SESSION_FILE) -> bool:
    """True if `python -m copilot login` has completed at least once."""
    return load_session(path) is not None


def ensure_session(
    path: str = config.SESSION_FILE,
    interactive: bool = True,
    force_refresh: bool = False,
) -> dict:
    """Return a session with a fresh token, refreshing or signing in as needed.

    Order: the saved session if its token is still fresh; else a headless refresh
    from the browser profile; else (if ``interactive``) a visible sign-in window.
    """
    from .browser import login, refresh_session

    session = load_session(path)
    if (
        session
        and not force_refresh
        and session.get("template")
        and token_is_fresh(session.get("access_token"), config.TOKEN_REFRESH_MARGIN)
    ):
        return session

    try:
        refreshed = refresh_session(previous=session)
        if refreshed.get("template"):
            return refreshed
    except AuthRequired:
        if not interactive:
            raise
    if not interactive:
        raise AuthRequired("Could not capture a chat request template. Run `python -m copilot login`.")
    return login()
