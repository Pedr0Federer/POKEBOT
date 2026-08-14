"""Best-effort git sync of state.json with the shared GitHub repo.

state.json's `last_checked` timestamp doubles as a heartbeat: the cloud
fallback workflow (GitHub Actions) reads it to tell whether this local
poller is currently alive, and skips its own scan (and alerts) when it is --
that's what stops local and cloud from double-alerting on the same restock.

All git operations here are best-effort: failures are logged and swallowed.
Local detection/alerting must never be blocked by a network or git problem.
"""

import logging
import subprocess
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent

# Full checks (the only thing that touches state.json) happen at most every
# few seconds during a burst of stock changes; there's no need to push a
# heartbeat commit that often.
MIN_PUSH_INTERVAL_SECONDS = 60

_last_push_monotonic = 0.0


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        timeout=30,
    )


def pull_latest(log: logging.Logger) -> None:
    """Fast-forward state.json from origin so a PC that was off doesn't
    re-alert on items the cloud fallback already caught and announced."""
    result = _run(["pull", "--ff-only", "origin", "main"])
    if result.returncode != 0:
        log.warning("state_sync: git pull failed (continuing with local state): %s", result.stderr.strip())


def push_state(log: logging.Logger) -> None:
    """Commit and push the current state.json as this cycle's heartbeat."""
    global _last_push_monotonic
    now = time.monotonic()
    if now - _last_push_monotonic < MIN_PUSH_INTERVAL_SECONDS:
        return
    _last_push_monotonic = now

    _run(["add", "state.json"])
    commit = _run(["commit", "-m", "chore: local heartbeat"])
    if commit.returncode != 0:
        return  # nothing changed since the last push

    push = _run(["push", "origin", "main"])
    if push.returncode == 0:
        return

    log.warning("state_sync: git push failed, retrying after pull --rebase: %s", push.stderr.strip())
    rebase = _run(["pull", "--rebase", "origin", "main"])
    if rebase.returncode != 0:
        log.warning("state_sync: git pull --rebase failed (will retry next cycle): %s", rebase.stderr.strip())
        return
    retry = _run(["push", "origin", "main"])
    if retry.returncode != 0:
        log.warning("state_sync: git push retry failed (will retry next cycle): %s", retry.stderr.strip())
