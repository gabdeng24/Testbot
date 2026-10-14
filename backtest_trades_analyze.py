"""
backtest_trades_analyze.py - trade-based backtest of the fair-value strategy.

Inputs (made by the downloaders):
  backtest_data.jsonl    result, coin candles, official start price per market
  backtest_trades.jsonl  every real taker trade in each market's 5-minute window

How a bet is simulated
  * Decisions happen at each minute mark (240/180/120/60 seconds left), because
    coin prices are only available per minute.
  * Model: P(Up) = Phi( ln(now/start) / (sd_1m * sqrt(seconds_left/60)) ).
  * The price the bot sees = the latest real trade that shows what Up (or Down)
    cost to buy, from the previous 5 seconds. No recent trade -> no decision.
  * Same rules as the live bot: edge >= 5c after fees, model and price within 30c.
  * The bet only FILLS if, after the chosen delay, a real trade shows the shares
    were available at or below the bot's price within the next 5 seconds.
    Otherwise it is missed and the bot tries again at the next minute.

A trade showing "Up could be bought at p" is either a taker buying Up at p,
or a taker selling Down at 1-p (on Polymarket the two are the same order book).

LIMITS: decisions only once a minute; a real trade at your price proves the
shares existed, not that you would have got them first.

    python backtest_trades_analyze.py
"""
import glob, json, math, os, sys
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone

import numpy as np
import pandas as pd

PHI = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
FEE_RATE = 0.072
US_DAY_UTC = set(range(13, 21))           # 9pm-4am Hong Kong
LOOKBACK = 5                              # seconds: only trades this recent count as "current price"
FILL_WINDOW = 5                           # seconds an order rests after arriving


def fee_per_share(p):
    return FEE_RATE * p * (1 - p)


def profit_if(price, won):
    shares = 1.0 / price
    net = shares - (FEE_RATE * shares * price * (1 - price)) / price
    return (net - 1.0) if won else -1.0


def load(data_path="backtest_data.jsonl", trades_path="backtest_trades.jsonl"):
    mk = {}
    for line in open(data_path, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        m = r.get("market") or {}
        if r.get("error") or not r.get("candles") or not m.get("closed"):
            continue
        op = m.get("outcomePrices")
        op = json.loads(op) if isinstance(op, str) else op
        try:
            a, b = float(op[0]), float(op[1])
        except Exception:
            continue
        if max(a, b) < 0.9:
            continue
        em = m.get("eventMetadata") or {}
        mk[(r["coin"], r["wts"])] = dict(coin=r["coin"], wts=r["wts"], up=int(a > b), candles=r["candles"],
                                        source=r.get("source", "random"),
                                        ptb=float(em["priceToBeat"]) if em.get("priceToBeat") else None)
    out = []
    for line in open(trades_path, encoding="utf-8"):
        try:
            t = json.loads(line)
        except Exception:
            continue
        k = (t["coin"], t["wts"])
        if t.get("error") or not t.get("trades") or k not in mk:
            continue
        m = dict(mk[k])
        m["trades"] = t["trades"]
        m["source"] = t.get("source") or m["source"]
        out.append(m)
    return out


def evidence(trades):
    """Per side: sorted times and prices at which that side could be bought."""
    ask = {"YES": [], "NO": []}
    for ts, oi, side, p, size in trades:
        if p <= 0 or p >= 1:
            continue
        if (oi == 0 and side == 1) or (oi == 1 and side == -1):
            ask["YES"].append((ts, p if oi == 0 else 1 - p))
        if (oi == 1 and side == 1) or (oi == 0 and side == -1):
            ask["NO"].append((ts, p if oi == 1 else 1 - p))
    res = {}
    for s, v in ask.items():
        v.sort()
        res[s] = ([x[0] for x in v], [x[1] for x in v])
    return res


def latest(ev, t, lookback=LOOKBACK):
    ts, ps = ev
    i = bisect_right(ts, t)
    if i == 0 or t - ts[i - 1] > lookback:
        return None
    return ps[i - 1]


def first_fill(ev, t0, t1, limit):
    ts, ps = ev
    for j in range(bisect_left(ts, t0), bisect_right(ts, t1)):
        if ps[j] <= limit + 1e-9:
            return ps[j]
    return None


def prep(m):
    c = {row[0]: row for row in m["candles"]}
    w = m["wts"]
    if w not in c:
        return None
    prev = [c[t][4] for t in range(w - 3600, w, 60) if t in c]
    if len(prev) < 30:
        return None
    sd = float(np.std(np.diff(np.log(prev)), ddof=1))
    if sd <= 0:
        return None
    now = {k: c[w + 60 * (k - 1)][4] for k in (1, 2, 3, 4) if (w + 60 * (k - 1)) in c}
    return dict(sd=sd, cb_start=c[w][1], now=now, ev=evidence(m["trades"]))


def run(markets, delay=0, start="official", edge=0.05, max_div=0.30, filt=None, check_fill=True):
    bets, points = [], []
    for m in markets:
        P = m.get("_prep")
        if P is None:
            continue
        s0 = m["ptb"] if (start == "official" and m["ptb"]) else P["cb_start"]
        hour = datetime.fromtimestamp(m["wts"], timezone.utc).hour
        month = datetime.fromtimestamp(m["wts"], timezone.utc).strftime("%Y-%m")
        for k in (1, 2, 3, 4):
            if k not in P["now"]:
                continue
            t = m["wts"] + 60 * k
            left = 300 - 60 * k
            p = PHI(math.log(P["now"][k] / s0) / (P["sd"] * math.sqrt(left / 60)))
            a_y, a_n = latest(P["ev"]["YES"], t), latest(P["ev"]["NO"], t)
            if a_y is not None:
                points.append((p, a_y, m["up"]))
            if a_n is not None:
                points.append((1 - p, a_n, 1 - m["up"]))
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
                     us_day=hour in US_DAY_UTC, hour_utc=hour, month=month, src=m["source"])
            if filt and not filt(b):
                continue
            if check_fill:
                fill = first_fill(P["ev"][side], t + delay, t + delay + FILL_WINDOW, ask)
                if fill is None:
                    continue                      # missed: try again next minute
            else:
                fill = ask
            won = (side == "YES") == (m["up"] == 1)
            b.update(price=fill, won=int(won), profit=profit_if(fill, won))
            bets.append(b)
            break
    return pd.DataFrame(bets), points


def p_le0(df, n=3000):
    if len(df) < 10:
        return float("nan")
    w = df.groupby("wts")["profit"].sum().values
    return float((np.random.default_rng(1).choice(w, (n, len(w))).sum(1) <= 0).mean())


def stats(df):
    if df is None or len(df) == 0:
        return dict(bets=0)
    return dict(bets=int(len(df)), won=round(float(df.won.mean()), 3), paid=round(float(df.price.mean()), 3),
                model=round(float(df.model.mean()), 3), pnl=round(float(df.profit.sum()), 2),
                per_bet_c=round(float(100 * df.profit.mean()), 1), p_le0=round(p_le0(df), 3))


def live_price_check(markets):
    """Compare trade-based prices with the live asks the sim bots recorded."""
    snaps = []
    for f in glob.glob("sim_cl*_model.jsonl"):
        for line in open(f, encoding="utf-8"):
            try:
                snaps.append(json.loads(line))
            except Exception:
                pass
    by = {(m["coin"], m["wts"]): m for m in markets if m.get("_prep")}
    errs = {"YES": [], "NO": []}
    for s in snaps:
        m = by.get((s.get("coin"), s.get("window_ts")))
        if not m or s.get("interval") != 300:
            continue
        for side, live in (("YES", s["ask_yes"]), ("NO", s["ask_no"])):
            est = latest(m["_prep"]["ev"][side], s["ts"])
            if est is not None:
                errs[side].append(est - live)
    allv = np.array(errs["YES"] + errs["NO"])
    if len(allv) == 0:
        return None
    return dict(moments=int(len(allv)), median_err_c=round(100 * float(np.median(allv)), 2),
                mean_abs_err_c=round(100 * float(np.abs(allv).mean()), 2),
                within_2c=round(float((np.abs(allv) <= 0.02).mean()), 3),
                within_5c=round(float((np.abs(allv) <= 0.05).mean()), 3))


def main():
    markets = load()
    for m in markets:
        m["_prep"] = prep(m)
    R = [m for m in markets if m["_prep"] and m["source"] == "random"]
    V = [m for m in markets if m["_prep"] and m["source"] == "validation"]
    out = {"markets_random": len(R), "markets_validation": len(V)}
    print(f"markets with trades: random sample {len(R)} | bots' own markets {len(V)}")
    if not R:
        return

    chk = live_price_check(markets)
    out["live_price_check"] = chk
    print(f"\n=== Data check: trade-based price vs the live price the bots saw === {chk}")

    base, pts = run(R, delay=0)
    P = pd.DataFrame(pts, columns=["model", "market", "won"])
    out["brier"] = dict(model=round(float(((P.model - P.won) ** 2).mean()), 4),
                        market=round(float(((P.market - P.won) ** 2).mean()), 4), points=int(len(P)))
    P["b"] = pd.cut(P.model, [0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1])
    cal = P.groupby("b", observed=True).agg(n=("won", "size"), model=("model", "mean"),
                                            market=("market", "mean"), actual=("won", "mean"))
    print(f"\n=== Who predicts better (lower = better) === {out['brier']}")
    print(cal.round(3).to_string())
    out["calibration"] = cal.reset_index().astype({"b": str}).to_dict("records")

    print("\n=== Paper fill (no fill check) vs real-trade fills by order delay ===")
    paper, _ = run(R, check_fill=False)
    out["paper_no_fill_check"] = stats(paper)
    print(f"  paper, always filled at seen price (optimistic - comparison only): {stats(paper)}")
    out["by_delay"] = {}
    for start in ("official", "exchange"):
        for d in (0, 1, 2, 5, 10):
            df, _ = run(R, delay=d, start=start)
            out["by_delay"][f"{start}_{d}s"] = s = stats(df)
            print(f"  start={start:8s} delay {d:>2}s : {s}")

    filters = {"none": None,
               "skip US daytime": lambda b: not b["us_day"],
               "only 30-60c": lambda b: 0.30 <= b["ask"] <= 0.60,
               "both": lambda b: (not b["us_day"]) and 0.30 <= b["ask"] <= 0.60}
    print("\n=== Filters (official start) at 0s and 2s delay ===")
    out["filters"] = {}
    rows = []
    for name, f in filters.items():
        for d in (0, 2):
            df, _ = run(R, delay=d, filt=f)
            out["filters"][f"{name}_{d}s"] = s = stats(df)
            print(f"  {name:16s} {d}s: {s}")
            for mth, g in (df.groupby("month") if len(df) else []):
                rows.append(dict(filter=name, delay=d, month=mth, bets=len(g),
                                 per_bet_c=round(100 * g.profit.mean(), 1), pnl=round(g.profit.sum(), 2)))
    M = pd.DataFrame(rows)
    if len(M):
        print("\n=== Cents per bet by month (official start) ===")
        print(M.pivot_table(index="month", columns=["filter", "delay"], values="per_bet_c").round(1).to_string())
    out["monthly"] = rows

    if V:
        vb, _ = run(V, delay=0)
        vb2, _ = run(V, delay=2)
        out["validation"] = dict(delay0=stats(vb), delay2=stats(vb2))
        print(f"\n=== Bots' own markets: backtest 0s {stats(vb)} | 2s {stats(vb2)}")
    json.dump(out, open("backtest_trades_results.json", "w"), indent=1, default=str)
    print("\nSaved backtest_trades_results.json")


if __name__ == "__main__":
    os.chdir(sys.argv[1]) if len(sys.argv) > 1 else None
    main()
