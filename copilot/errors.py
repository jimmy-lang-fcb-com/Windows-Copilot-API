"""Exceptions raised by the M365 Copilot bridge."""


class CopilotError(RuntimeError):
    """M365 Copilot answered with an error, or the turn failed."""


class AuthRequired(CopilotError):
    """No usable signed-in session. Run ``python -m copilot login``."""


class AuthExpired(AuthRequired):
    """The captured access token was rejected; a refresh may fix it."""
