"""
tail_log.py — Live log viewer for crypto_bot.log
Run in a dedicated terminal:  python tail_log.py
Shows a live dashboard at the top + colored log lines below.
Never stops. Never gets interrupted by bot restarts.
"""

import os
import sys
import time
import json
import argparse
from datetime import datetime

# ── Color codes ───────────────────────────────────────────────────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
RED    = "\033[91m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
GRAY   = "\033[90m"
WHITE  = "\033[97m"
BLUE   = "\033[94m"

# ── Dashboard config ──────────────────────────────────────────────────────────
DASHBOARD_LINES  = 14   # number of lines the dashboard occupies
DASHBOARD_EVERY  = 5.0  # seconds between dashboard refreshes


def load_portfolio(portfolio_file: str) -> dict:
    """Load portfolio state from disk."""
    if not os.path.exists(portfolio_file):
        return {}
    try:
        with open(portfolio_file) as f:
            return json.load(f)
    except Exception:
        return {}


def load_trades(trades_file: str) -> list:
    if not os.path.exists(trades_file):
        return []
    try:
        with open(trades_file) as f:
            return json.load(f)
    except Exception:
        return []


def render_dashboard(base_dir: str, prefix: str = "crypto") -> str:
    """Build the dashboard string from portfolio + trades files."""
    portfolio = load_portfolio(os.path.join(base_dir, f"{prefix}_portfolio.json"))
    trades    = load_trades(os.path.join(base_dir, f"{prefix}_trades.json"))

    now = datetime.now().strftime("%H:%M:%S")
    label = prefix.upper()
    if prefix == "live":
        mode_txt = "LIVE 💸 REAL MONEY"
    elif prefix in ("market", "limit", "crypto"):
        mode_txt = "PAPER MODE"
    else:
        mode_txt = "SIM (real order book, no money)"

    if not portfolio:
        return (
            f"{BOLD}{CYAN}{'─'*65}{RESET}\n"
            f"  {BOLD}{label} BOT  |  {now}  |  {mode_txt}{RESET}\n"
            f"{BOLD}{CYAN}{'─'*65}{RESET}\n"
            f"  {GRAY}No portfolio data yet — waiting for bot to start...{RESET}\n"
            f"{BOLD}{CYAN}{'─'*65}{RESET}\n"
        )

    cash      = portfolio.get("cash", 500.0)
    positions = portfolio.get("positions", {})
    resolved  = portfolio.get("resolved", [])
    total_bets = portfolio.get("total_bets", 0)
    start     = portfolio.get("daily_start", 500.0)
    saved_at  = portfolio.get("saved_at", "")[:19]

    # Portfolio value
    open_value = sum(p.get("bet_size", 0) for p in positions.values())
    total      = cash + open_value
    pnl        = total - start
    pnl_pct    = (pnl / start) * 100 if start else 0
    pnl_color  = GREEN if pnl >= 0 else RED
    pnl_sign   = "+" if pnl >= 0 else ""

    # Win rate
    wins   = sum(1 for t in resolved if t.get("outcome") == "WIN")
    losses = sum(1 for t in resolved if t.get("outcome") == "LOSS")
    total_resolved = wins + losses
    wr     = wins / total_resolved if total_resolved else 0
    wr_color = GREEN if wr >= 0.60 else YELLOW if wr >= 0.50 else RED

    # Recent streak
    streak     = 0
    streak_dir = ""
    for t in reversed(resolved):
        outcome = t.get("outcome")
        if not streak_dir and outcome in ("WIN", "LOSS"):
            streak_dir = outcome
        if outcome == streak_dir:
            streak += 1
        else:
            break
    streak_color = GREEN if streak_dir == "WIN" else RED if streak_dir == "LOSS" else GRAY
    streak_str   = f"{streak_color}{streak}x {streak_dir}{RESET}" if streak_dir else f"{GRAY}—{RESET}"

    # Last 10 trades as W/L dots
    last10 = resolved[-10:]
    dots = "".join(
        f"{GREEN}●{RESET}" if t.get("outcome") == "WIN"
        else f"{RED}●{RESET}"
        for t in last10
    )
    dots = dots or f"{GRAY}no trades yet{RESET}"

    # Profit from trades file (more accurate)
    all_resolved = [t for t in trades if t.get("outcome") in ("WIN","LOSS")]
    realized_pnl = sum(t.get("profit", 0) for t in all_resolved)
    rpnl_color   = GREEN if realized_pnl >= 0 else RED
    rpnl_sign    = "+" if realized_pnl >= 0 else ""

    # Open positions summary
    if positions:
        pos_lines = []
        for pid, pos in list(positions.items())[:4]:
            side  = pos.get("bet_side","?")
            size  = pos.get("bet_size", 0)
            price = pos.get("bet_price", 0)
            q     = pos.get("question","")[:45]
            pos_lines.append(
                f"  {CYAN}{side}{RESET} ${size:.2f} @ {price:.0%} | {GRAY}{q}{RESET}"
            )
        if len(positions) > 4:
            pos_lines.append(f"  {GRAY}... +{len(positions)-4} more open{RESET}")
        pos_str = "\n".join(pos_lines)
    else:
        pos_str = f"  {GRAY}No open positions{RESET}"

    # Fill-rate line — only meaningful for the limit bot
    intended  = portfolio.get("intended", 0)
    pending   = len(portfolio.get("pending_limits", {}))
    fillrate_line = None
    if intended or pending or prefix == "limit":
        fr = (total_bets / intended * 100) if intended else 0
        fr_color = GREEN if fr >= 50 else YELLOW if fr >= 30 else RED
        fillrate_line = (
            f"  Intended: {BOLD}{intended}{RESET}  |  "
            f"Filled: {BOLD}{total_bets}{RESET}  |  "
            f"Pending: {pending}  |  "
            f"FILL RATE: {fr_color}{BOLD}{fr:.0f}%{RESET}"
        )

    lines = [
        f"{BOLD}{CYAN}{'─'*65}{RESET}",
        f"  {BOLD}{label} BOT  |  {now}  |  {mode_txt}{RESET}  {GRAY}(updated {saved_at}){RESET}",
        f"{BOLD}{CYAN}{'─'*65}{RESET}",
        f"  Cash: {BOLD}${cash:.2f}{RESET}  |  "
        f"Portfolio: {BOLD}${total:.2f}{RESET}  |  "
        f"P&L: {pnl_color}{BOLD}{pnl_sign}${pnl:.2f} ({pnl_sign}{pnl_pct:.1f}%){RESET}",
        f"  Bets: {BOLD}{total_bets}{RESET}  |  "
        f"Resolved: {total_resolved}  |  "
        f"Win rate: {wr_color}{BOLD}{wr:.0%}{RESET}  "
        f"({GREEN}{wins}W{RESET}/{RED}{losses}L{RESET})  |  "
        f"Realized P&L: {rpnl_color}{BOLD}{rpnl_sign}${realized_pnl:.2f}{RESET}",
    ]
    if fillrate_line:
        lines.append(fillrate_line)
    lines += [
        f"  Streak: {streak_str}  |  Last 10: {dots}",
        f"{BOLD}{CYAN}{'─'*65}{RESET}",
        f"  OPEN POSITIONS ({len(positions)}):",
        pos_str,
        f"{BOLD}{CYAN}{'─'*65}{RESET}",
        f"  {GRAY}── Live log ──────────────────────────────────────────{RESET}",
    ]

    return "\n".join(lines)


def colorize(line: str) -> str:
    """Add color based on line content."""
    l = line.lower()
    if "error" in l or "traceback" in l or "nameerror" in l or "exception" in l:
        return RED + line + RESET
    if "[paper]" in l or "[market]" in l:
        return GREEN + BOLD + line + RESET
    if "[limit-fill]" in l:
        return GREEN + BOLD + line + RESET
    if "[limit-post]" in l:
        return CYAN + line + RESET
    if "[limit-expire]" in l:
        return GRAY + line + RESET
    if "✅" in line or ("win" in l and "❌" not in l and "rate" not in l):
        return GREEN + line + RESET
    if "❌" in line or ("loss" in l and "streak" not in l):
        return RED + line + RESET
    if "vetoed" in l:
        return YELLOW + line + RESET
    if "approved by claude" in l:
        return CYAN + line + RESET
    if "skip:" in l:
        return GRAY + line + RESET
    if "patch" in l or "restarted" in l or "auto-" in l:
        return BLUE + line + RESET
    if "scan #" in l or "check #" in l:
        return BOLD + line + RESET
    if "market_snapshot" in l:
        return GRAY + line + RESET
    if "warning" in l:
        return YELLOW + line + RESET
    return line


def clear_lines(n: int):
    """Move cursor up N lines and clear them."""
    for _ in range(n):
        sys.stdout.write("\033[F\033[K")


def tail(log_file: str, base_dir: str, prefix: str = "crypto", poll_interval: float = 0.5):
    log_lines       = []       # buffer of recent log lines to redisplay
    MAX_LOG_LINES   = 30       # how many log lines to keep on screen
    last_dash_time  = 0
    last_size       = 0
    last_inode      = None
    file_handle     = None
    first_render    = True

    # Get terminal height
    try:
        term_rows = os.get_terminal_size().lines
    except Exception:
        term_rows = 40
    MAX_LOG_LINES = max(10, term_rows - DASHBOARD_LINES - 3)

    def redraw():
        """Redraw the full screen: dashboard + log lines."""
        os.system("clear")
        print(render_dashboard(base_dir, prefix))
        for line in log_lines[-MAX_LOG_LINES:]:
            print(colorize(line))

    while True:
        try:
            now = time.time()

            # Refresh dashboard periodically
            if now - last_dash_time >= DASHBOARD_EVERY or first_render:
                redraw()
                last_dash_time = now
                first_render   = False

            # Check log file
            if not os.path.exists(log_file):
                time.sleep(poll_interval)
                continue

            stat      = os.stat(log_file)
            cur_inode = stat.st_ino
            cur_size  = stat.st_size

            # File reopened
            if cur_inode != last_inode:
                if file_handle:
                    file_handle.close()
                file_handle = open(log_file, "r")
                if last_inode is None:
                    file_handle.seek(0, 2)  # first open: seek to end
                else:
                    log_lines.append(f"{BLUE}── Log reopened (bot restarted) ──{RESET}")
                last_inode = cur_inode
                last_size  = file_handle.tell()

            # New content
            if cur_size > last_size:
                new_lines = file_handle.readlines()
                for line in new_lines:
                    line = line.rstrip()
                    if line:
                        log_lines.append(line)
                last_size = file_handle.tell()

                # Redraw with new lines
                redraw()
                last_dash_time = now

            elif cur_size < last_size:
                file_handle.seek(0)
                last_size = 0

            time.sleep(poll_interval)

        except KeyboardInterrupt:
            print(f"\n{GRAY}Log viewer stopped.{RESET}")
            if file_handle:
                file_handle.close()
            sys.exit(0)
        except Exception as e:
            print(f"{RED}Viewer error: {e}{RESET}")
            time.sleep(poll_interval)


if __name__ == "__main__":
    default_dir = os.path.expanduser("~/Desktop/polybot")
    parser = argparse.ArgumentParser(description="Live log viewer with dashboard")
    parser.add_argument(
        "--log",
        default=os.path.join(default_dir, "crypto_bot.log"),
        help="Path to log file"
    )
    parser.add_argument(
        "--dir",
        default=default_dir,
        help="Bot directory containing portfolio/trades JSON files"
    )
    parser.add_argument(
        "--prefix",
        default="crypto",
        help="State-file prefix: 'market', 'limit', or 'crypto' (default)"
    )
    parser.add_argument(
        "--interval",
        type=float, default=0.5,
        help="Poll interval in seconds (default: 0.5)"
    )
    args = parser.parse_args()
    tail(args.log, args.dir, args.prefix, args.interval)

