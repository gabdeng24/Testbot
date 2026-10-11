"""
kill_switch.py — Emergency stop for live trading.

Run from anywhere on the server:
    python kill_switch.py on        # STOP all new real-money orders NOW
    python kill_switch.py off       # resume trading
    python kill_switch.py status    # show current state

HOW IT WORKS:
    "on" creates a file at ~/.killswitch. The live executor checks for this
    file before EVERY order. If it exists, no new order is placed — full stop.
    This is a hard local file check that happens FIRST, before any strategy
    logic, API call, or order construction. Even if the bot is bugged, the
    file check still fires.

    Existing open positions are NOT force-closed (selling 5-min markets is
    messy and risky). They resolve normally. The kill switch only prevents
    NEW positions from opening.

DESIGN PRINCIPLE:
    This is intentionally dead simple and dependency-free. It is the one
    thing standing between you and runaway real-money trading, so it must
    never fail for a complicated reason. No imports beyond the standard
    library. No network. No config. Just a file.
"""

import os
import sys
from datetime import datetime

KILL_FILE = os.path.expanduser("~/.killswitch")


def kill_on():
    """Create the killswitch file — stops all new orders."""
    if os.path.exists(KILL_FILE):
        print(f"⛔ Kill switch ALREADY ON (since {_since()}).")
        print("   No new orders are being placed.")
        return
    with open(KILL_FILE, "w") as f:
        f.write(datetime.now().isoformat())
    print("⛔ KILL SWITCH ENGAGED.")
    print(f"   File created: {KILL_FILE}")
    print("   The bot will NOT place any new orders.")
    print("   Existing positions will resolve normally.")
    print("   Run 'python kill_switch.py off' to resume.")


def kill_off():
    """Remove the killswitch file — resumes trading."""
    if not os.path.exists(KILL_FILE):
        print("✓ Kill switch is already OFF. Trading is allowed.")
        return
    # Show how long it was engaged
    since = _since()
    os.remove(KILL_FILE)
    print("✓ KILL SWITCH RELEASED.")
    print(f"   Was engaged since: {since}")
    print("   The bot may now place orders again (if --live and other checks pass).")


def kill_status():
    """Print the current kill switch state."""
    if os.path.exists(KILL_FILE):
        print(f"⛔ Kill switch is ON (engaged {_since()}).")
        print("   New orders are BLOCKED.")
    else:
        print("✓ Kill switch is OFF.")
        print("   New orders are allowed (subject to other safety checks).")


def _since() -> str:
    """Return a human-readable 'when engaged' string."""
    try:
        with open(KILL_FILE) as f:
            ts = f.read().strip()
        dt = datetime.fromisoformat(ts)
        delta = datetime.now() - dt
        mins = int(delta.total_seconds() / 60)
        if mins < 60:
            ago = f"{mins} min ago"
        elif mins < 1440:
            ago = f"{mins // 60}h {mins % 60}m ago"
        else:
            ago = f"{mins // 1440}d ago"
        return f"{ts[:19]} ({ago})"
    except Exception:
        return "unknown time"


# ── Public helper for the executor to import ────────────────────────────────
def is_killed() -> bool:
    """
    The executor calls this before every order.
    Returns True if the kill switch is engaged.
    """
    return os.path.exists(KILL_FILE)


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("on", "off", "status"):
        print("Usage: python kill_switch.py [on|off|status]")
        print()
        print("  on      Stop all new real-money orders immediately")
        print("  off     Resume trading")
        print("  status  Show current state")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "on":
        kill_on()
    elif cmd == "off":
        kill_off()
    else:
        kill_status()
