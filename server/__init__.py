"""OpenAI-compatible HTTP server for Microsoft 365 Copilot.

Start it:

    from server import app
    app()

(`python app.py` in the project root does exactly this.) The server runs on
http://127.0.0.1:8000 — set HOST / PORT to override. It bridges the OpenAI Chat
Completions shape onto :class:`copilot.CopilotClient`; sign in once first with
``python -m copilot login``.

Code is split by concern:

    config.py         constants
    schemas.py        pydantic request models
    prompt.py         flatten OpenAI messages -> one Copilot prompt
    openai_format.py  build OpenAI response/chunk shapes
    api.py            FastAPI app, routes, upstream serialization
"""

import os

from .api import app as _api


def app(host=None, port=None) -> None:
    """Start the server (blocks while uvicorn runs).

    On first run (never signed in) this opens a browser for interactive sign-in
    before serving, so requests don't fail with a "not signed in" error.
    """
    import uvicorn

    from copilot.auth import signed_in_before

    if host is None:
        host = os.environ.get("HOST", "127.0.0.1")
    if port is None:
        port = int(os.environ.get("PORT", "8000"))

    if not signed_in_before():
        try:
            from copilot.browser import login

            login()
        except Exception as exc:
            print(f"Warning: could not sign in to M365 Copilot: {exc}")

    print(f"M365 Copilot OpenAI-compatible API on http://{host}:{port}  (POST /v1/chat/completions)")
    uvicorn.run(_api, host=host, port=port)


__all__ = ["app"]
