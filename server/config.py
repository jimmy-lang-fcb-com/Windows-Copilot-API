"""Server configuration — shared constants."""

import os

# The single model id this bridge advertises.
MODEL_NAME = "m365-copilot"

# Self-imposed rate limit, so automated callers don't trip M365 Copilot's own
# per-user throttling. Tune with the probe in tests/ratelimit.py.
#   RATE_LIMIT_RPM   requests/minute the bridge will accept; 0 disables limiting.
#   RATE_LIMIT_BURST max requests allowed back-to-back before pacing kicks in.
RATE_LIMIT_RPM = float(os.environ.get("RATE_LIMIT_RPM", "12"))  # 12 rpm ≈ 5s per call
RATE_LIMIT_BURST = int(os.environ.get("RATE_LIMIT_BURST", "4"))

# Emulated tool calling: how many times to ask M365 Copilot to fix a malformed
# tool call before giving up and returning its reply as plain text.
TOOL_CALL_RETRIES = int(os.environ.get("TOOL_CALL_RETRIES", "1"))
