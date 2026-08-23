"""Watchdog heartbeat — a dead-man's switch for the unattended deal bot.

The deal bot writes a `run_log` row (and posts a Discord run-log embed) every
scheduled run. But if a run is *silently skipped* by the scheduler (already
observed once per HANDOFF) or crashes before `log_run` executes, nothing
anywhere says so — the only symptom is an absence of posts over days.

This script runs hourly (see .github/workflows/watchdog.yml) and checks the
freshness of `run_log`: if the most recent row is older than `max_hours` (or
there is no row at all), it posts a warning embed to RUN_LOG_WEBHOOK_URL and
exits 0 (alerting is not a watchdog failure). A stale run turns red in the
workflow only via the missing-run alert, never by failing the watchdog itself.
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone

from deal_bot import config, transport
from deal_bot.integrations.discord import _post_webhook
from deal_bot.storage.supabase import _supabase_headers


def fetch_last_run() -> datetime | None:
    """Most recent run_log.ran_at, or None if the query failed or there's no
    run at all. Uses the shared transport (bounded retry). Callers treat
    None as "cannot confirm freshness" — run_watchdog decides what that
    means (stale → alert)."""
    url = f"{config.SUPABASE_URL}/rest/v1/run_log"
    resp = transport.request(
        "GET", url, headers=_supabase_headers(),
        params={"select": "ran_at", "order": "ran_at.desc", "limit": "1"},
        timeout=15,
    )
    if resp is None:
        print("[watchdog] run_log fetch failed after retries")
        return None
    if resp.status_code != 200:
        print(f"[watchdog] run_log fetch returned {resp.status_code}: {resp.text[:300]}")
        return None
    rows = resp.json()
    if not rows or not rows[0].get("ran_at"):
        return None
    try:
        return datetime.fromisoformat(rows[0]["ran_at"].replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        print(f"[watchdog] could not parse ran_at {rows[0].get('ran_at')!r}")
        return None


def _run_is_stale(last_run: datetime | None, max_hours: int) -> bool:
    """True when there's no run at all, or the most recent run is older than
    max_hours. None (no data / fetch failure) is treated as stale."""
    if last_run is None:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_hours)
    return last_run < cutoff


def run_watchdog(max_hours: int = 6) -> bool:
    """Returns True if an alert was posted (or would have been, without a
    configured webhook). Returns False when the last run is fresh — or when
    there's no Supabase config at all: without DB access the watchdog can't
    know anything, and a false "no run in 6h" alarm every hour would just
    train the operator to ignore it (misconfiguration surfaces as loud
    console output in the Actions log instead)."""
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        print("[watchdog] SUPABASE_URL / SUPABASE_SERVICE_KEY not set — nothing to check, skipping alert")
        return False

    last_run = fetch_last_run()
    if not _run_is_stale(last_run, max_hours):
        print(f"[watchdog] last run {last_run.isoformat()} is fresh — no alert")
        return False

    print("[watchdog] no deal-bot run in the last "
          f"{max_hours}h — posting alert")
    if not config.RUN_LOG_WEBHOOK_URL:
        return True

    fields = [
        {"name": "Last run", "value": last_run.isoformat() if last_run else "none found", "inline": True},
        {"name": "Alert window", "value": f"{max_hours}h", "inline": True},
        {"name": "Fix", "value": "Check GitHub Actions for deal_bot.yml — a run may have been silently skipped or crashed before logging.", "inline": False},
    ]
    payload = {
        "embeds": [{
            "title": "⚠️ No deal-bot run in the last %dh" % max_hours,
            "color": 0xE74C3C,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    }
    return _post_webhook(config.RUN_LOG_WEBHOOK_URL, payload, "watchdog")


def main() -> None:
    parser = argparse.ArgumentParser(description="Dead-man's switch for the deal bot")
    parser.add_argument("--max-hours", type=int, default=6, help="alert if no run within this many hours")
    args = parser.parse_args()
    run_watchdog(max_hours=args.max_hours)
    # A stale run is not a watchdog failure — the watchdog succeeds even when
    # it has to alert. It exits 0 either way.
    sys.exit(0)


if __name__ == "__main__":
    main()