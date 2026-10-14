"""
backtest_trades_collect.py - downloads every real trade in past Polymarket
5-minute markets, for a backtest that only counts a bet as filled when a real
trade happened at or below the bot's price.

Uses the markets already found by backtest_collect.py (backtest_data.jsonl):
for each it looks up the market's conditionId (gamma API) and downloads all
taker trades inside the 5-minute window (data API). Trades are stored compactly
as [time, outcome_index, side, price, size].

Public data only. No orders, no keys. Resumable.

    python backtest_trades_collect.py --probe     # check 3 markets
    python backtest_trades_collect.py --max 2000  # full run
"""
import argparse, json, os, sys, time
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
SRC = "backtest_data.jsonl"
OUT = "backtest_trades.jsonl"
LOG = "backtest_trades.log"
S = requests.Session()
S.headers["User-Agent"] = "polybot-backtest/1.0"


def log(msg):
    line = f"{datetime.now().strftime('%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get(url, params=None, tries=4):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(2 + 3 * i)
                continue
            if r.status_code >= 500:
                time.sleep(1 + i)
                continue
            return r
        except requests.RequestException:
            time.sleep(1 + 2 * i)
    return None


def condition_id(rec):
    m = rec.get("market") or {}
    if m.get("conditionId"):
        return m["conditionId"]
    if m.get("id"):
        r = get(f"{GAMMA}/markets/{m['id']}")
        if r is not None and r.status_code == 200:
            try:
                return r.json().get("conditionId")
            except Exception:
                pass
    slug = f"{rec['coin'].lower()}-updown-5m-{rec['wts']}"
    for params in ({"slug": slug, "closed": "true"}, {"slug": slug}):
        r = get(f"{GAMMA}/markets", params)
        if r is not None and r.status_code == 200:
            try:
                d = r.json()
                d = d[0] if isinstance(d, list) and d else d
                if isinstance(d, dict) and d.get("conditionId"):
                    return d["conditionId"]
            except Exception:
                pass
    return None


def trades(cid, wts):
    """All taker trades in [wts-30, wts+330], paged."""
    out, offset = [], 0
    while True:
        r = get(f"{DATA}/trades", {"market": cid, "limit": 1000, "offset": offset,
                                   "start": wts - 30, "end": wts + 330, "takerOnly": "true"})
        if r is None or r.status_code != 200:
            return out, f"data-api http {getattr(r, 'status_code', 'none')}"
        page = r.json()
        if not isinstance(page, list):
            return out, "unexpected response"
        for t in page:
            try:
                ts = int(t["timestamp"])
                ts = ts // 1000 if ts > 10**12 else ts
                out.append([ts, int(t.get("outcomeIndex", 0)), 1 if t.get("side") == "BUY" else -1,
                            float(t["price"]), float(t.get("size") or 0)])
            except Exception:
                continue
        if len(page) < 1000 or offset >= 9000:
            break
        offset += 1000
        time.sleep(0.1)
    out.sort()
    return out, None


def candidates(max_random):
    seen, val, rnd = set(), [], []
    for line in open(SRC, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        key = (r["coin"], r["wts"])
        if key in seen or r.get("error") or not r.get("market") or not r.get("candles"):
            continue
        seen.add(key)
        (val if r.get("source") == "validation" else rnd).append(r)
    return val + rnd[:max_random]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=2000, help="random markets to fetch (validation markets always included)")
    ap.add_argument("--probe", action="store_true")
    a = ap.parse_args()
    if not os.path.exists(SRC):
        log(f"ABORT: {SRC} not found - run RUN_BACKTEST.bat first")
        sys.exit(2)
    todo = candidates(a.max)

    if a.probe:
        for rec in todo[:2] + todo[-1:]:
            cid = condition_id(rec)
            tr, err = trades(cid, rec["wts"]) if cid else ([], "no conditionId")
            inwin = [t for t in tr if rec["wts"] <= t[0] < rec["wts"] + 300]
            log(f"PROBE {rec['coin']} {rec['wts']} [{rec.get('source')}] cid={'ok' if cid else 'MISSING'} "
                f"err={err} trades={len(tr)} in-window={len(inwin)} first={tr[:3]}")
        return

    done = set()
    if os.path.exists(OUT):
        for line in open(OUT, encoding="utf-8"):
            try:
                x = json.loads(line)
                if not x.get("error"):
                    done.add((x["coin"], x["wts"]))
            except Exception:
                pass
    todo = [r for r in todo if (r["coin"], r["wts"]) not in done]
    log(f"Trade downloader: {len(todo)} markets to fetch ({len(done)} already done) -> {OUT}")
    ok = bad = 0
    started = time.time()
    for i, rec in enumerate(todo, 1):
        cid = condition_id(rec)
        out = {"coin": rec["coin"], "wts": rec["wts"], "source": rec.get("source"), "cid": cid}
        if not cid:
            out["error"] = "no conditionId"
        else:
            out["trades"], err = trades(cid, rec["wts"])
            if err:
                out["error"] = err
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(out, separators=(",", ":")) + "\n")
        if out.get("error") or not out.get("trades"):
            bad += 1
        else:
            ok += 1
        if i == 25 and ok == 0:
            log("ABORT: the first 25 markets returned no trades - tell Claude")
            sys.exit(2)
        if i % 50 == 0 or i == len(todo):
            rate = i / max(1, time.time() - started)
            log(f"{i}/{len(todo)} done | with trades {ok} | missing {bad} | "
                f"~{(len(todo) - i) / max(rate, 0.01) / 60:.0f} min left")
        time.sleep(0.12)
    log("FINISHED")


if __name__ == "__main__":
    main()
