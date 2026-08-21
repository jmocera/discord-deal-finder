"""Shared HTTP transport with bounded retry/backoff.

Every outbound HTTP call in the package routes through `request()`, so
transient failures (network blips, 429/5xx) are retried once, in one place,
instead of each caller re-implementing "try/except + print + return". This
protects dedup correctness: a transient `load_seen`/`upsert_seen_entry`
failure no longer silently becomes an empty seen-map or a dropped write,
either of which risks a double-post on the next run.

Retry policy:
- Retries network errors (requests.RequestException) and retryable statuses.
- Retryable statuses: 408, 425, 429, 500, 502, 503, 504.
- Permanent 4xx (400/401/404) are NOT retried — they'd never succeed.
- Honors a Retry-After header on 429 (seconds).
- Returns the last Response; returns None only after exhausting network
  retries (so callers can distinguish "network failure" from "HTTP response").
"""

import time
from typing import Any

import requests

# Status codes that warrant a retry (transient upstream/Supabase blips).
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (1.0, 2.0)  # sleep between attempts 1->2, 2->3


def request(
    method: str, url: str, *,
    json: Any = None, params: dict | None = None, headers: dict | None = None,
    timeout: int = 15, retryable: frozenset[int] = RETRYABLE_STATUSES,
    attempts: int = MAX_ATTEMPTS, base_sleep: tuple[float, float] = BACKOFF_SECONDS,
) -> requests.Response | None:
    """Issue a request with bounded retry on transient failures.

    Returns the last Response. Returns None only after exhausting all
    network-error retries (i.e. a hard network failure, not an HTTP error
    response). Permanent 4xx and final-attempt statuses are returned as-is.
    """
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.request(method, url, headers=headers, json=json, params=params, timeout=timeout)
        except requests.RequestException as e:
            if attempt == attempts:
                return None
            _log_retry(method, url, attempt, attempts, error=e)
            time.sleep(base_sleep[attempt - 1])
            continue

        if resp.status_code not in retryable:
            return resp
        if attempt == attempts:
            return resp

        wait = base_sleep[attempt - 1]
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = max(wait, float(retry_after))
                except ValueError:
                    pass
        _log_retry(method, url, attempt, attempts, status=resp.status_code)
        time.sleep(wait)

    return None


def _log_retry(method: str, url: str, attempt: int, total: int, *, status: int | None = None, error: Exception | None = None) -> None:
    if error is not None:
        print(f"[transport] {method} {url} failed (attempt {attempt}/{total}): {error}")
    else:
        print(f"[transport] {method} {url} returned {status} (attempt {attempt}/{total}), retrying")


# Re-export for callers that only need the retry loop and not the full
# signature (kept minimal).
__all__ = ["request", "RETRYABLE_STATUSES", "MAX_ATTEMPTS"]