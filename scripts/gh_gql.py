"""GraphQL against github.com via the `gh` CLI.

`gh` handles auth (GH_TOKEN in CI, keyring locally). Retries transient
failures, sleeps when the rate-limit budget runs low, and paces calls to
stay clear of secondary rate limits.
"""

import json
import subprocess
import sys
import time
from datetime import datetime, timezone

RETRY_BACKOFF = [5, 15, 60, 180]
MIN_RATE_REMAINING = 200
PACE_SECONDS = 0.5

_last_call = 0.0


class GqlError(RuntimeError):
    pass


def _parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _sleep_until(iso_ts, extra=30):
    delta = (_parse_ts(iso_ts) - datetime.now(timezone.utc)).total_seconds() + extra
    if delta > 0:
        print(f"rate limit: sleeping {delta:.0f}s until {iso_ts}", file=sys.stderr)
        time.sleep(delta)


def run(query, variables=None):
    """Execute a GraphQL query, return payload["data"]. Raises GqlError."""
    global _last_call
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in (variables or {}).items():
        if value is None:
            continue
        args += ["-F", f"{key}={value}"]

    last_err = None
    for attempt, backoff in enumerate([0] + RETRY_BACKOFF):
        if backoff:
            print(f"retrying in {backoff}s: {last_err}", file=sys.stderr)
            time.sleep(backoff)

        wait = _last_call + PACE_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()

        proc = subprocess.run(args, capture_output=True, text=True)
        # gh exits non-zero when the response carries GraphQL errors but still
        # prints the payload to stdout — so parse stdout regardless of exit code.
        payload = None
        if proc.stdout.strip():
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError:
                payload = None
        if payload is None:
            last_err = (proc.stderr or "no output from gh").strip()[:300]
            continue
        if "errors" in payload:
            errors = payload["errors"]
            if any(e.get("type") == "RATE_LIMITED" for e in errors):
                reset = (((payload.get("data") or {}).get("rateLimit")) or {}).get("resetAt")
                if reset:
                    _sleep_until(reset)
                else:
                    time.sleep(120)
                last_err = "RATE_LIMITED"
                continue
            last_err = json.dumps(errors)[:300]
            continue

        data = payload["data"]
        rate = data.get("rateLimit")
        if rate and rate["remaining"] < MIN_RATE_REMAINING:
            _sleep_until(rate["resetAt"])
        return data

    raise GqlError(f"giving up after {len(RETRY_BACKOFF) + 1} attempts: {last_err}")
