"""
ntfy_notify.py — push notifications via ntfy.sh
================================================
Sends a push notification to the ntfy app on your phone after each run.

Setup:
  1. Install the ntfy app on your phone (iOS / Android — free)
  2. Subscribe to a topic of your choice (e.g. "parth-pm-jobs")
  3. Set the NTFY_TOPIC environment variable (in GitHub Actions secrets)
     OR paste your topic directly in config below as a fallback.

No account needed for basic ntfy.sh usage.
"""

import os
import requests

# ── fallback if env var not set (fine for local testing, use env var on server)
_FALLBACK_TOPIC = "your-ntfy-topic-here"

def push(title: str, message: str, priority: str = "default") -> bool:
    """
    Send a push notification.
    priority: "min" | "low" | "default" | "high" | "urgent"
    Returns True if sent successfully, False otherwise (never raises).
    """
    topic = os.environ.get("NTFY_TOPIC", _FALLBACK_TOPIC)
    if not topic or topic == "your-ntfy-topic-here":
        print(f"  [ntfy] No topic set — skipping notification. ({title})")
        return False
    try:
        r = requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title":    title,
                "Priority": priority,
                "Tags":     "briefcase",
            },
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"  [ntfy] Failed to send notification: {e}")
        return False


def run_summary(script_name: str, n_apply: int, n_maybe: int, n_skip: int):
    """Send a post-run summary notification."""
    title   = f"🗂 {script_name} — run complete"
    message = (
        f"✅ Apply: {n_apply}  |  🤔 Maybe: {n_maybe}  |  ❌ Skip: {n_skip}\n"
        f"Check your Google Sheet for details."
    )
    priority = "high" if n_apply > 0 else "default"
    push(title, message, priority)
