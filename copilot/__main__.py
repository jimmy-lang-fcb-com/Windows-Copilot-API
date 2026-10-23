"""Command-line entry point.

    python -m copilot login                    # sign in, capture the chat session
    python -m copilot ask "hi"                 # one-shot question (browser mode)
    python -m copilot ask --direct "hi"        # same, via direct mode
    python -m copilot ask --show "hi"          # browser mode with a visible window
"""

import sys


def main(argv) -> int:
    cmd = argv[0] if argv else "ask"
    if cmd == "login":
        from .browser import login

        login()
        return 0
    if cmd == "ask":
        args = argv[1:]
        mode = "direct" if "--direct" in args else None
        headless = "--show" not in args
        prompt = " ".join(a for a in args if a not in ("--direct", "--show")) or "Hello!"
        from .client import CopilotClient

        client = CopilotClient(mode=mode, headless=headless)
        try:
            for chunk in client.stream(prompt):
                print(chunk, end="", flush=True)
            print()
        finally:
            client.close()
        return 0
    print(f"Unknown command: {cmd!r}. Use 'login' or 'ask [--direct] [--show] <prompt>'.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
