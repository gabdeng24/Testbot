"""
sim_report.py — compare sim variants (and the real live run) side by side.

USAGE:
    python3 sim_report.py                      # auto-finds every *_attempts.json
    python3 sim_report.py simA simB live       # specific tags

For each tag it reads <tag>_portfolio.json (resolved trades) and, if present,
<tag>_attempts.json (every approved decision, filled or blocked).

The "live" tag works on the real live run's files (live_portfolio.json); live
has no attempts file, so its block rate is shown as "-".
"""

import glob
import json
import math
import os
import sys


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def summarize(tag):
    port = load(f"{tag}_portfolio.json", {})
    resolved = port.get("resolved", []) if isinstance(port, dict) else []
    attempts = load(f"{tag}_attempts.json", [])

    n = len(resolved)
    wins = sum(1 for r in resolved if r.get("outcome") == "WIN")
    pnl = sum(r.get("profit", 0) for r in resolved)
    stake = sum(r.get("bet_size", 0) for r in resolved) or 1
    fills = [r.get("fill_price") for r in resolved if r.get("fill_price")]
    avg_fill = sum(fills) / len(fills) if fills else None

    filled = sum(1 for a in attempts if a.get("status") == "filled")
    blocked = sum(1 for a in attempts if a.get("status") == "blocked")
    tried = filled + blocked

    wr = wins / n if n else None
    ci = 1.96 * math.sqrt(wr * (1 - wr) / n) if n else None
    # breakeven win rate at the average fill price (ignoring fee for clarity)
    be = avg_fill if avg_fill else None

    return {
        "tag": tag,
        "n": n,
        "wr": wr,
        "ci": ci,
        "breakeven": be,
        "pnl": pnl,
        "roi": pnl / stake if n else None,
        "avg_fill": avg_fill,
        "block_rate": blocked / tried if tried else None,
        "tried": tried,
    }


def pct(x):
    return "-" if x is None else f"{x:.0%}"


def main():
    tags = sys.argv[1:]
    if not tags:
        tags = sorted({os.path.basename(p)[: -len("_attempts.json")]
                       for p in glob.glob("*_attempts.json")})
        if os.path.exists("live_portfolio.json"):
            tags.append("live")
    if not tags:
        print("No sim data found yet. Start a sim with: python crypto_bot_market.py --sim")
        return

    rows = [summarize(t) for t in tags]
    print(f"\n{'tag':<14}{'trades':>7}{'win%':>7}{'±95%':>7}{'b/even':>8}"
          f"{'avg fill':>10}{'P&L $':>9}{'ROI':>7}{'blocked':>9}")
    print("-" * 78)
    for r in rows:
        fill_txt = "-" if r["avg_fill"] is None else f"{r['avg_fill']:.2f}"
        print(f"{r['tag']:<14}{r['n']:>7}{pct(r['wr']):>7}{pct(r['ci']):>7}"
              f"{pct(r['breakeven']):>8}{fill_txt:>10}"
              f"{r['pnl']:>+9.2f}{pct(r['roi']):>7}{pct(r['block_rate']):>9}")
    print("\nwin% must beat b/even (≈ avg fill price) to make money.")
    print("Small samples are noisy — compare variants only after ~100+ trades each.\n")

    # Adverse-move breakdown: did the price fall between decision and fill?
    for t in tags:
        port = load(f"{t}_portfolio.json", {})
        res = port.get("resolved", []) if isinstance(port, dict) else []
        res = [r for r in res if r.get("fill_price") and r.get("bet_price")]
        if len(res) < 10:
            continue
        print(f"[{t}] win rate by price move between decision and fill:")
        buckets = [("dropped >20c", -9, -0.20), ("dropped 10-20c", -0.20, -0.10),
                   ("dropped 2-10c", -0.10, -0.02), ("flat or up", -0.02, 9)]
        for name, lo, hi in buckets:
            g = [r for r in res if lo <= r["fill_price"] - r.get("decided_price", r["bet_price"]) < hi]
            if g:
                w = sum(1 for r in g if r.get("outcome") == "WIN")
                p = sum(r.get("profit", 0) for r in g)
                print(f"   {name:<15} n={len(g):<4} win {w/len(g):.0%}  P&L {p:+.2f}")
        print()


if __name__ == "__main__":
    main()
