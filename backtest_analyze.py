"""
backtest_analyze.py - replays the fair-value strategy on data saved by
backtest_collect.py and reports how it would have done.

At each whole minute of a 5-minute market (240, 180, 120 and 60 seconds left)
it computes the same model probability the live bot uses:
    P(Up) = Phi( ln(now/start) / (sd_1m * sqrt(seconds_left/60)) )
with start = coin price at the window open and sd_1m = 1-minute volatility
over the previous hour. The market price comes from Polymarket's 1-minute
price history; the price paid is that price plus an assumed half-spread.
The first minute that clears the bot's rules gets a $1 bet (one per market).

LIMITS: no order-book history exists, so the price paid is an estimate, and
1-minute data can't see the second-by-second moves that hurt delayed orders.
Treat results as optimistic.

    python backtest_analyze.py backtest_data.jsonl
"""
import json, math, sys
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, ".")
try:
    import polymarket_fees as fees
    FEE = lambda p: fees.taker_fee(1.0, p, "crypto")
    COST = lambda usd, p: fees.market_order_cost(usd, p, "crypto")
except Exception:                                     # same formula, standalone
    FEE = lambda p: 0.072 * p * (1 - p)
    COST = lambda usd, p: {"net_shares": usd / p - 0.072 * (usd / p) * p * (1 - p) / p}

PHI = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
US_DAY_UTC = set(range(13, 21))                       # 9pm-4am Hong Kong = 13:00-20:59 UTC


def load(path):
    out = []
    for line in open(path, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        m = r.get("market") or {}
        if r.get("error") or not r.get("hist_up") or not r.get("candles") or not m.get("closed"):
            continue
        op = m.get("outcomePrices")
        op = json.loads(op) if isinstance(op, str) else op
        try:
            up = {(1.0, 0.0): 1, (0.0, 1.0): 0}[(float(op[0]), float(op[1]))]
        except Exception:
            continue                                  # unresolved / 50-50
        r["up"] = up
        out.append(r)
    return out


def price_at(hist, t, not_before=None):
    """Last price recorded at or before t (and not before `not_before`), else None."""
    best = None
    for pt in hist:
        if pt["t"] <= t:
            best = pt
        else:
            break
    if best is None or (not_before is not None and best["t"] < not_before):
        return None
    return float(best["p"])


def features(r):
    """One row per decision minute: model probability, market prices, coin move."""
    wts = r["wts"]
    c = {row[0]: row for row in r["candles"]}         # time -> [t, open, high, low, close]
    if wts not in c:
        return []
    start = c[wts][1]
    prev = [c[t][4] for t in range(wts - 3600, wts, 60) if t in c]
    if len(prev) < 30:
        return []
    rets = np.diff(np.log(prev))
    sd = float(np.std(rets, ddof=1))
    if sd <= 0:
        return []
    hu = sorted(r["hist_up"], key=lambda x: x["t"])
    hd = sorted(r.get("hist_down") or [], key=lambda x: x["t"])
    rows = []
    for k in (1, 2, 3, 4):
        t = wts + 60 * k
        if (t - 60) not in c:
            continue
        now = c[t - 60][4]                            # close of the minute just finished
        left = 300 - 60 * k
        p = PHI(math.log(now / start) / (sd * math.sqrt(left / 60)))
        pu = price_at(hu, t, not_before=wts)          # only prices from inside this window
        if pu is None:
            continue
        pdn = price_at(hd, t, not_before=wts) if hd else None
        pdn = pdn if pdn is not None else 1 - pu
        nxt_u = price_at(hu, t + 60, not_before=wts)
        nxt_d = price_at(hd, t + 60, not_before=wts) if hd else None
        if nxt_d is None and nxt_u is not None:
            nxt_d = 1 - nxt_u
        rows.append(dict(coin=r["coin"], wts=wts, k=k, left=left, p_up=p, mkt_up=pu, mkt_dn=pdn,
                         nxt_up=nxt_u, nxt_dn=nxt_d, move_bps=(now / start - 1) * 1e4,
                         up=r["up"], src=r.get("source", "random")))
    return rows


def simulate(F, half_spread=0.01, edge=0.05, max_div=0.30, lo=20, hi=280,
             filt=None, fill_next_minute=False):
    """First qualifying minute per market -> $1 bet. Returns one row per bet."""
    bets = []
    for (coin, wts), g in F.groupby(["coin", "wts"], sort=False):
        for _, x in g.sort_values("k").iterrows():
            if not (lo <= x.left <= hi):
                continue
            ask_u = min(0.99, x.mkt_up + half_spread)
            ask_d = min(0.99, x.mkt_dn + half_spread)
            e_u = x.p_up - ask_u - FEE(ask_u)
            e_d = (1 - x.p_up) - ask_d - FEE(ask_d)
            side, ask, e, ps = (("YES", ask_u, e_u, x.p_up) if e_u >= e_d
                                else ("NO", ask_d, e_d, 1 - x.p_up))
            if e < edge or abs(ps - ask) > max_div or ask < 0.02:
                continue
            toward = x.move_bps if side == "YES" else -x.move_bps
            hour = datetime.fromtimestamp(wts, timezone.utc).hour
            b = dict(coin=coin, wts=wts, left=x.left, side=side, price=ask, model=ps, edge=e,
                     toward=toward, hour_utc=hour, us_day=hour in US_DAY_UTC, src=x.src,
                     month=datetime.fromtimestamp(wts, timezone.utc).strftime("%Y-%m"))
            if filt and not filt(b):
                continue
            if fill_next_minute:                      # harsh delay: fill a full minute later
                nxt = x.nxt_up if side == "YES" else x.nxt_dn
                if nxt is None:
                    break
                b["price"] = min(0.99, nxt + half_spread)
            won = (side == "YES") == (x.up == 1)
            shares = COST(1.0, b["price"])["net_shares"]
            b["won"] = int(won)
            b["profit"] = (shares - 1.0) if won else -1.0
            bets.append(b)
            break
    return pd.DataFrame(bets)


def boot_p_le0(df, n=4000, seed=1):
    if len(df) < 5:
        return float("nan")
    w = df.groupby("wts")["profit"].sum().values
    sims = np.random.default_rng(seed).choice(w, (n, len(w))).sum(1)
    return float((sims <= 0).mean())


def stats(df):
    if df is None or len(df) == 0:
        return dict(bets=0)
    return dict(bets=int(len(df)), won=round(float(df.won.mean()), 3), paid=round(float(df.price.mean()), 3),
                model=round(float(df.model.mean()), 3), pnl=round(float(df.profit.sum()), 2),
                per_bet_c=round(float(100 * df.profit.mean()), 1), p_le0=round(boot_p_le0(df), 3))


FILTERS = {
    "as run (bot rules)": None,
    "A: skip US daytime": lambda b: not b["us_day"],
    "B: only 30-60c": lambda b: 0.30 <= b["price"] <= 0.60,
    "C: skip if coin >5bps against": lambda b: b["toward"] > -5,
    "A+B": lambda b: (not b["us_day"]) and 0.30 <= b["price"] <= 0.60,
    "A+B+C": lambda b: (not b["us_day"]) and 0.30 <= b["price"] <= 0.60 and b["toward"] > -5,
}


def main(path):
    recs = load(path)
    F = pd.DataFrame([row for r in recs for row in features(r)])
    print(f"usable markets: {len(recs)} | decision points: {len(F)} | "
          f"random sample markets: {F[F.src=='random'].wts.nunique() if len(F) else 0}")
    if F.empty:
        return
    R = F[F.src == "random"]
    out = {}

    # 1) calibration: model vs market vs reality, at every decision point (no betting rules)
    pts = pd.concat([pd.DataFrame(dict(model=R.p_up, market=R.mkt_up, won=R.up)),
                     pd.DataFrame(dict(model=1 - R.p_up, market=R.mkt_dn, won=1 - R.up))])
    pts["bucket"] = pd.cut(pts.model, [0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1])
    cal = pts.groupby("bucket", observed=True).agg(n=("won", "size"), model=("model", "mean"),
                                                    market=("market", "mean"), actual=("won", "mean"))
    print("\n=== Calibration at every decision minute (random sample) ===")
    print(cal.round(3).to_string())
    print(f"Brier score  model {((pts.model - pts.won) ** 2).mean():.4f}  |  market {((pts.market - pts.won) ** 2).mean():.4f}  (lower = better)")
    out["calibration"] = cal.reset_index().astype({"bucket": str}).to_dict("records")

    # 2) strategy results under different price-paid assumptions
    print("\n=== Bot rules on the random sample, by assumed extra cost over the recorded price ===")
    res = {}
    for hs in (0.0, 0.01, 0.02, 0.03):
        res[hs] = s = stats(simulate(R, half_spread=hs))
        print(f"  +{int(hs*100)}c: {s}")
    out["by_spread"] = {str(k): v for k, v in res.items()}

    base = simulate(R, half_spread=0.01)
    out["base"] = stats(base)

    # 3) filters, overall and month by month
    print("\n=== Filters (assumed +1c cost), whole period ===")
    filt_rows = []
    for name, f in FILTERS.items():
        d = simulate(R, half_spread=0.01, filt=f)
        s = stats(d); s["filter"] = name
        print(f"  {name:32s} {s}")
        for mth, g in (d.groupby("month") if len(d) else []):
            filt_rows.append(dict(filter=name, month=mth, bets=len(g), pnl=round(g.profit.sum(), 2),
                                  per_bet_c=round(100 * g.profit.mean(), 1)))
    M = pd.DataFrame(filt_rows)
    if len(M):
        print("\n=== Cents per bet, month by month ===")
        print(M.pivot_table(index="month", columns="filter", values="per_bet_c").round(1).to_string())
        print("\n=== Bets per month ===")
        print(M.pivot_table(index="month", columns="filter", values="bets").to_string())
    out["monthly"] = filt_rows

    # 4) breakdowns of the unfiltered bot
    if len(base):
        for col, bins in (("price", [0, .1, .2, .3, .4, .5, .6, .7, 1]), ("toward", [-1e9, -5, -2, 0, 2, 5, 1e9]),
                          ("left", None), ("coin", None), ("hour_utc", None)):
            key = pd.cut(base[col], bins) if bins else base[col]
            g = base.groupby(key, observed=True).agg(n=("profit", "size"), won=("won", "mean"),
                                                     paid=("price", "mean"), pnl=("profit", "sum"),
                                                     per_bet=("profit", "mean"))
            print(f"\n--- bot rules, by {col} ---"); print(g.round(3).to_string())
            out[f"by_{col}"] = g.reset_index().astype({col: str}).to_dict("records")

    # 5) harsh delay bound: every order fills a full minute late
    d1 = simulate(R, half_spread=0.01, fill_next_minute=True)
    out["fill_next_minute"] = stats(d1)
    print(f"\n=== Harsh delay bound (fill one minute later) === {stats(d1)}")

    # 6) validation: replay on the markets the live sim bots traded, compare
    V = F[F.src == "validation"]
    if len(V):
        vb = simulate(V, half_spread=0.01, lo=20, hi=240)
        out["validation_backtest"] = stats(vb)
        print(f"\n=== Validation: backtest on the sim bots' own markets === {stats(vb)}")
    json.dump(out, open("backtest_results.json", "w"), indent=1, default=str)
    print("\nSaved backtest_results.json")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "backtest_data.jsonl")
