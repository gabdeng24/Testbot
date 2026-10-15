"""
backtest_hourly_analyze.py - trade-based backtest of the fair-value strategy on
Polymarket HOURLY crypto Up/Down markets (data from backtest_hourly_collect.py).

Same rules as the 5-minute backtest (backtest_trades_analyze.py):
  * model P(Up) = Phi( ln(now/open) / (sd_1m * sqrt(minutes_left)) ), where
    open = Binance's price at the start of the hour (what these markets settle on)
  * decisions every 5 minutes through the hour (55, 50, ... 5 minutes left)
  * price seen = latest real trade showing what that side cost, last 5 seconds
  * bet only if edge >= 5c after fees and model and price are within 30c
  * the bet FILLS only if a real trade at or below the bot's price happens within
    5 seconds after the order arrives (delays tested: 0, 2, 5, 10, 30 s);
    otherwise it is missed and the bot tries again 5 minutes later.

    python backtest_hourly_analyze.py
"""
import json, math, os, sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtest_trades_analyze import (PHI, evidence, latest, first_fill, fee_per_share,
                                     profit_if, stats, FILL_WINDOW)

US_DAY_UTC = set(range(13, 21))
STEP_MIN = 5


def load(path="backtest_hourly.jsonl"):
    out = []
    for line in open(path, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        m = r.get("market") or {}
        if r.get("error") or not r.get("trades") or not r.get("candles") or not m.get("closed"):
            continue
        op = m.get("outcomePrices")
        op = json.loads(op) if isinstance(op, str) else op
        try:
            a, b = float(op[0]), float(op[1])
        except Exception:
            continue
        if max(a, b) < 0.9:
            continue
        c = {row[0]: row for row in r["candles"]}
        ts = r["wts"]
        if ts not in c:
            continue
        prev = [c[t][4] for t in range(ts - 3600, ts, 60) if t in c]
        if len(prev) < 30:
            continue
        sd = float(np.std(np.diff(np.log(prev)), ddof=1))
        if sd <= 0:
            continue
        now = {mm: c[ts + 60 * (mm - 1)][4] for mm in range(STEP_MIN, 60, STEP_MIN) if (ts + 60 * (mm - 1)) in c}
        out.append(dict(coin=r["coin"], wts=ts, up=int(a > b), open=c[ts][1], sd=sd, now=now,
                        ev=evidence(r["trades"]), n_trades=len(r["trades"]),
                        volume=float(m.get("volume") or 0)))
    return out


def run(markets, delay=0, edge=0.05, max_div=0.30, filt=None, check_fill=True, min_left=0, max_left=60):
    bets, points = [], []
    for m in markets:
        hour = datetime.fromtimestamp(m["wts"], timezone.utc).hour
        month = datetime.fromtimestamp(m["wts"], timezone.utc).strftime("%Y-%m")
        for mm in sorted(m["now"]):
            left = 60 - mm
            t = m["wts"] + 60 * mm
            p = PHI(math.log(m["now"][mm] / m["open"]) / (m["sd"] * math.sqrt(left)))
            a_y, a_n = latest(m["ev"]["YES"], t), latest(m["ev"]["NO"], t)
            if a_y is not None:
                points.append((p, a_y, m["up"], left))
            if a_n is not None:
                points.append((1 - p, a_n, 1 - m["up"], left))
            if not (min_left <= left <= max_left):
                continue
            cands = []
            if a_y is not None:
                cands.append(("YES", a_y, p - a_y - fee_per_share(a_y), p))
            if a_n is not None:
                cands.append(("NO", a_n, (1 - p) - a_n - fee_per_share(a_n), 1 - p))
            if not cands:
                continue
            side, ask, e, ps = max(cands, key=lambda x: x[2])
            if e < edge or abs(ps - ask) > max_div:
                continue
            b = dict(coin=m["coin"], wts=m["wts"], left=left, side=side, ask=ask, model=ps, edge=e,
                     us_day=hour in US_DAY_UTC, month=month)
            if filt and not filt(b):
                continue
            if check_fill:
                fill = first_fill(m["ev"][side], t + delay, t + delay + FILL_WINDOW, ask)
                if fill is None:
                    continue
            else:
                fill = ask
            won = (side == "YES") == (m["up"] == 1)
            b.update(price=fill, won=int(won), profit=profit_if(fill, won))
            bets.append(b)
            break
    return pd.DataFrame(bets), points


def main():
    ms = load()
    out = {"markets": len(ms)}
    print(f"usable hourly markets: {len(ms)}")
    if not ms:
        return
    nt = np.array([m["n_trades"] for m in ms])
    print(f"trades per hourly market: median {np.median(nt):.0f}, 10th pct {np.percentile(nt, 10):.0f}, 90th pct {np.percentile(nt, 90):.0f}")
    out["trades_per_market_median"] = float(np.median(nt))

    base, pts = run(ms)
    P = pd.DataFrame(pts, columns=["model", "market", "won", "left"])
    out["brier"] = dict(model=round(float(((P.model - P.won) ** 2).mean()), 4),
                        market=round(float(((P.market - P.won) ** 2).mean()), 4), points=int(len(P)))
    print(f"\n=== Who predicts better (lower = better) === {out['brier']}")
    for lo, hi in ((30, 60), (10, 29), (0, 9)):
        g = P[(P.left >= lo) & (P.left <= hi)]
        if len(g):
            print(f"   {lo}-{hi} min left: model {((g.model - g.won) ** 2).mean():.4f} | market {((g.market - g.won) ** 2).mean():.4f} | n={len(g)}")
    P["b"] = pd.cut(P.model, [0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1])
    print(P.groupby("b", observed=True).agg(n=("won", "size"), model=("model", "mean"),
                                            market=("market", "mean"), actual=("won", "mean")).round(3).to_string())

    print("\n=== Real-trade fills by order delay ===")
    paper, _ = run(ms, check_fill=False)
    out["paper_no_fill_check"] = stats(paper)
    print(f"  paper, always filled at seen price (optimistic - comparison only): {stats(paper)}")
    out["by_delay"] = {}
    for d in (0, 2, 5, 10, 30):
        df, _ = run(ms, delay=d)
        out["by_delay"][f"{d}s"] = s = stats(df)
        print(f"  delay {d:>2}s : {s}")

    print("\n=== By when in the hour the bot may bet (2s delay) ===")
    out["by_window"] = {}
    for lo, hi, name in ((30, 55, "first half (30-55 min left)"), (10, 29, "middle (10-29 min left)"),
                         (5, 9, "last minutes (5-9 min left)")):
        df, _ = run(ms, delay=2, min_left=lo, max_left=hi)
        out["by_window"][name] = s = stats(df)
        print(f"  {name:28s}: {s}")

    print("\n=== Filters (2s delay) ===")
    out["filters"] = {}
    rows = []
    for name, f in {"none": None, "only 30-60c": lambda b: 0.30 <= b["ask"] <= 0.60,
                    "skip US daytime": lambda b: not b["us_day"],
                    "edge >= 10c": lambda b: b["edge"] >= 0.10}.items():
        df, _ = run(ms, delay=2, filt=f)
        out["filters"][name] = s = stats(df)
        print(f"  {name:16s}: {s}")
        for mth, g in (df.groupby("month") if len(df) else []):
            rows.append(dict(filter=name, month=mth, bets=len(g), per_bet_c=round(100 * g.profit.mean(), 1)))
    if rows:
        M = pd.DataFrame(rows)
        print("\n=== Cents per bet by month (2s delay) ===")
        print(M.pivot_table(index="month", columns="filter", values="per_bet_c").round(1).to_string())
        print(M.pivot_table(index="month", columns="filter", values="bets").to_string())
    out["monthly"] = rows
    json.dump(out, open("backtest_hourly_results.json", "w"), indent=1, default=str)
    print("\nSaved backtest_hourly_results.json")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        os.chdir(sys.argv[1])
    main()
