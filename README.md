# M365 Copilot API: an OpenAI-compatible bridge to Microsoft 365 Copilot

**Proof of concept for IT.** It signs in to the Microsoft 365 Copilot web app
([m365.cloud.microsoft/chat](https://m365.cloud.microsoft/chat)) as *your* work
account and exposes it as an API you can call from code. It doesn't use the
official Graph Copilot Chat API. Instead it automates the same web app you use in
the browser, so it follows your account's licence, permissions, and tenant
policies.

You can use it in two ways:

- 🐍 **As a Python library:** `client.chat("Hi")`, with streaming and multi-turn conversations.
- 🔌 **As a local OpenAI-compatible API:** a server at `http://localhost:8000/v1` that speaks the OpenAI format, so the `openai` SDK and OpenAI-compatible tools work against it.

> **Unofficial.** Not affiliated with or endorsed by Microsoft. It drives the
> M365 Copilot web app with your own credentials. Check with your tenant's
> security/compliance owners before running it anywhere but your own machine.

---

## How it works

1. **Sign in once** (`python -m copilot login`). A real Chromium window opens on
   M365 Copilot and you sign in with your work account and MFA. The browser
   profile is saved under `session/profile/`, so later runs stay signed in.
2. **Capture.** After sign-in the bridge sends one warm-up message. It records the
   page's own chat WebSocket (`substrate.office.com/m365Copilot/Chathub/...`):
   the URL with its access token, plus the exact `chat` request the page sent.
   Both are saved to `session/token.json`.
3. **Chat**, in one of two modes:

| Mode | How | Pros | Cons |
| --- | --- | --- | --- |
| `browser` (default) | A headless browser types your prompt into the real page and reads the reply off its chat socket. | Most robust: the page builds every request itself. | Slower (~1 tab per conversation); conversations last only while the process runs. |
| `direct` (experimental) | Replays the captured request over a plain WebSocket with your prompt swapped in. No browser per turn. | Fast, light; conversations resume by id across restarts. | Works only while M365 accepts the captured request shape; refreshing the token (≈ hourly) re-opens a headless browser and sends a warm-up message. |

Pick a mode with `COPILOT_MODE=browser|direct` or `CopilotClient(mode=...)`.

The protocol is SignalR over a WebSocket. Reply text arrives in cumulative
`update` frames, which [copilot/protocol.py](copilot/protocol.py) turns into
streamed deltas.

---

## Requirements

- **Python 3.9+**
- A work account with a **Microsoft 365 Copilot** licence (Copilot Chat in the M365 app)
- macOS, Windows, or Linux with a display for the first sign-in

---

## Setup

```bash
python3 -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\Activate.ps1

pip install -r requirements.txt
playwright install chromium

python -m copilot login             # sign in with your work account (+ MFA)
python -m copilot ask "What meetings do I have tomorrow?"
```

`login` continues by itself once the chat box appears. The warm-up message
("Reply with the single word: ready") shows up in your Copilot history.

---

## Usage 1: In Python (no server)

```python
from copilot import CopilotClient

client = CopilotClient()                 # browser mode; CopilotClient(mode="direct") for direct

reply = client.chat("Summarize the last email from my manager.")
print(reply.text)

# Continue the SAME conversation — pass the id back
reply2 = client.chat("Draft a short reply.", reply.conversation_id)
print(reply2.text)

# Stream the answer as it arrives
for chunk in client.stream("Tell me a short joke"):
    print(chunk, end="", flush=True)

client.close()
```

Pass `headless=False` to watch the browser work. Errors raise
`copilot.CopilotError`; a signed-out session raises `copilot.AuthRequired`.

👉 More: [examples/01_direct_chat.py](examples/01_direct_chat.py), [02_direct_conversation.py](examples/02_direct_conversation.py), [03_direct_stream.py](examples/03_direct_stream.py)

---

## Usage 2: As an OpenAI-compatible server

Start a local server that speaks the OpenAI API, so existing OpenAI tools and SDKs work unchanged.

```bash
python app.py
# -> M365 Copilot OpenAI-compatible API on http://127.0.0.1:8000
```

Then point any OpenAI client at it (the API key is required by the SDK but ignored):

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")

resp = client.chat.completions.create(
    model="m365-copilot",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

Or call it with plain HTTP / `curl`:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Hello!"}]}'
```

**Endpoints**

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/v1/chat/completions` | Chat (supports `"stream": true` and an optional `"conversation_id"`) |
| `GET`  | `/v1/models` | Lists the single `m365-copilot` model |

> Change the address with env vars: `HOST=0.0.0.0 PORT=8080 python app.py`, or run `uvicorn server.api:app --host 0.0.0.0 --port 8080`.

👉 More: [examples/04_server_http.py](examples/04_server_http.py), [05_server_stream.py](examples/05_server_stream.py), [06_server_openai_sdk.py](examples/06_server_openai_sdk.py)

---

## Command line

```bash
python -m copilot login                  # sign in and capture the chat session
python -m copilot ask "Hello!"           # one-shot question (browser mode)
python -m copilot ask --direct "Hello!"  # same, via direct mode
python -m copilot ask --show "Hello!"    # browser mode with a visible window
```

---

## Configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `COPILOT_MODE` | `browser` | `browser` or `direct` |
| `M365_COPILOT_URL` | `https://m365.cloud.microsoft/chat` | Chat page to open |
| `M365_CHATHUB_HINT` | `chathub` | Substring that identifies the chat WebSocket URL |
| `M365_COMPOSER_SELECTORS` | (see [copilot/config.py](copilot/config.py)) | Comma-separated CSS selectors for the chat box |
| `M365_SEND_SELECTORS` | (see [copilot/config.py](copilot/config.py)) | Send button, clicked if Enter doesn't send |
| `M365_BROWSER_CHANNEL` | `chromium` | `chromium` (new headless mode), `msedge` (installed Edge), or empty for Playwright's headless shell |
| `COPILOT_DEBUG` | unset | `1` logs direct-mode frames (redacted) to `session/direct_debug.log` |
| `M365_WARMUP_PROMPT` | `Reply with the single word: ready` | Message used to capture the session |
| `M365_SESSION_DIR` | `session` | Where the profile and token live |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Server address |
| `RATE_LIMIT_RPM` / `RATE_LIMIT_BURST` | `12` / `4` | Server rate limit |

---

## Run with Docker (optional)

```bash
docker compose up --build
```

The container mounts `session/` and runs headless, so sign in on the host first.
**Sign in on a Linux host:** Chromium encrypts a profile's cookies with an
OS-specific key, so a profile created on macOS or Windows looks signed out
inside the container. When the session expires the server answers `503`
(`type: "auth_required"`); sign in again and restart.

---

## Concurrency & stress test

The server bridges a **single** signed-in M365 Copilot account (and, in browser
mode, a single browser). So the server
**serializes** upstream calls: parallel HTTP requests queue behind a lock and run
one at a time (see [server/api.py](server/api.py)). This is intentional, and it
means throughput is sequential, not parallel.

You can measure where it breaks with the included stress test, which fires a
batch of simultaneous requests and **doubles the batch size every successful
round** until the first error:

```bash
# Start the server in one terminal
python app.py

# Ramp concurrency in another (1 → 2 → 4 → 8 → …)
python tests/stress.py
python tests/stress.py --max 64 --timeout 120 --url http://localhost:8000
```

**Sample run** (consumer Copilot, before the M365 retool; re-measure for your tenant):

| Concurrency | Result | Wall time | Latency (min / median / max) |
| --- | --- | --- | --- |
| 1 | ✓ all ok | 3.7s | 3.7 / 3.7 / 3.7s |
| 2 | ✓ all ok | 4.6s | 3.4 / 4.6 / 4.6s |
| 4 | ✓ all ok | 8.3s | 3.7 / 6.7 / 8.3s |
| 8 | ✗ 1 failed (`HTTP 502`) | 13.3s | 3.5 / 9.7 / 13.3s |

**Highest fully-successful concurrency: 4.** Wall time roughly doubles each round
while *minimum* latency stays flat (~3.5s) — the signature of a serialized queue:
one request runs immediately, the rest wait their turn. The failure at 8 is an
upstream `502` (Copilot rejecting requests under load), not a server crash or
timeout — so the exact break point is flaky and may vary between runs.

> Takeaway: keep concurrent in-flight requests low (≈ 1–4). This is a proof of
> concept, not a high-throughput gateway — and it runs as *your* account.

---

## Rate limiting

Concurrency (above) is *how many at once*; the **rate limit** is *how many per
minute, sustained*. M365 Copilot throttles each
user (you'll see a `Throttled` error), so the bridge enforces a self-imposed one with a [token bucket](server/ratelimit.py): it caps
accepted requests per minute and returns a standard `429` + `Retry-After` when
you exceed it. Two env vars tune it:

| Env var | Default | Meaning |
| --- | --- | --- |
| `RATE_LIMIT_RPM` | `12` | Requests/minute the bridge accepts. `0` disables the limit. |
| `RATE_LIMIT_BURST` | `4` | How many requests may go back-to-back before pacing kicks in. |

```bash
RATE_LIMIT_RPM=20 RATE_LIMIT_BURST=5 python app.py   # raise it; 0 to disable
```

To find *your* ceiling, run the server with the limiter
off (`RATE_LIMIT_RPM=0`) and push the probe until failures appear:

```bash
python tests/ratelimit.py --rpm 20 --minutes 3
```

**On the client side, use exponential backoff.** Both `429` (bridge limit) and
the occasional `502` (upstream hiccup) are transient — retry with
growing delays (e.g. 1s, 2s, 4s) and they almost always clear. The official
`openai` SDK does this automatically and honours `Retry-After`; with plain HTTP,
add a few retries yourself.

---

## Project layout

| Path | What it does |
| --- | --- |
| [copilot/client.py](copilot/client.py) | `CopilotClient`: the public API, picks a mode |
| [copilot/browser.py](copilot/browser.py) | Playwright: sign-in, session capture, browser-mode chat (worker thread) |
| [copilot/driver.py](copilot/driver.py) | Direct mode: replays the captured request over a WebSocket |
| [copilot/protocol.py](copilot/protocol.py) | SignalR framing, reply parsing, request templating (pure, unit-tested) |
| [copilot/auth.py](copilot/auth.py) | `session/token.json` load/save and token refresh |
| [copilot/config.py](copilot/config.py) | URLs, selectors, timeouts (all overridable by env var) |
| [server/](server/) | The FastAPI OpenAI-compatible server |
| [examples/](examples/) | Runnable examples |
| [tests/](tests/) | Unit tests, the diagnostic capture tool, stress and rate-limit probes |

Run the offline tests with `python -m unittest tests.test_protocol tests.test_server`.

---

## Notes & limitations

- **Text only.** Replies are returned as text, citation markers (e.g. `[^1^]`) included. Cards, files and images the web app renders aren't surfaced.
- **One prompt per turn.** The web app has no system/role channel, so the server flattens OpenAI `messages` into a single prompt ([server/prompt.py](server/prompt.py)).
- **Tenant policy applies.** Conditional Access rules (compliant device, managed browser) can block the automated Chromium. If sign-in works in your normal browser but not here, that's the likely cause.
- **Your session is a secret.** `session/` holds a signed-in browser profile and a bearer token. It's git-ignored; don't copy it around.
- **Microsoft can change the web app at any time.** If something breaks, run the diagnostic below. Fixes usually mean adjusting a selector, the hub hint, or the reply parser.

---

## Troubleshooting

```bash
python tests/diagnostic.py               # visible browser; send one message, then close it
python tests/diagnostic.py --no-browser  # report only
```

It writes `session/ws_capture.log` (every WebSocket the page opens and every chat
frame sent and received, with tokens/JWTs/emails redacted) and
`session/diagnostic_report.txt` (versions, config, session age, template shape).

| Symptom | Likely fix |
| --- | --- |
| "chat box not found" | Find the composer in the capture/DevTools and set `M365_COMPOSER_SELECTORS`. |
| "page never opened its chat socket" | Check the `OPEN` lines in the capture for the chat URL; set `M365_CHATHUB_HINT`. |
| Replies empty or cut short | Compare `<<` frames with `ReplyParser` in [copilot/protocol.py](copilot/protocol.py). |
| Direct mode `InvalidRequest` / closed socket | Run it with `COPILOT_DEBUG=1` and compare `session/direct_debug.log` (`>>` lines) with the browser's request in `session/ws_capture.log` from the diagnostic. Browser mode is unaffected. |
| `AuthRequired` / HTTP 503 | Run `python -m copilot login`. |
| "The prompt was never sent" | Open `session/last_error.png`; try `ask --show`, or `M365_BROWSER_CHANNEL=msedge`. |
| Windows `WinError 10013` starting the server | Port is taken or reserved (Hyper-V/WSL/Docker). Use another: `PORT=8080 python app.py`. |

---

## License

Released under the [MIT License](LICENSE). As this is an unofficial project, you remain responsible for complying with Microsoft's terms of service.

---

## Star History

<a href="https://www.star-history.com/?repos=sums001%2FWindows-Copilot-API&type=timeline&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=sums001/Windows-Copilot-API&type=timeline&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=sums001/Windows-Copilot-API&type=timeline&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=sums001/Windows-Copilot-API&type=timeline&legend=top-left" />
 </picture>
</a>
