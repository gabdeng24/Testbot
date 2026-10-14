"""
backtest_collect.py - downloads history for a historical backtest of the
fair-value strategy on Polymarket 5-minute crypto Up/Down markets.

For a random sample of past markets (since 5-min markets launched in Feb 2026)
it saves, per market:
  - the official result (Polymarket gamma API)
  - the market's price history at 1-minute steps (Polymarket CLOB prices-history)
  - the coin's 1-minute candles for the hour before and the 5 minutes of the window
    (Coinbase; Binance.US as fallback)
Reads only public data. Places no orders. Needs no keys.

It also includes the markets your sim bots traded on Oct 13-14, so the
backtest method can be checked against what the bots actually saw.

Resumable: results are appended to backtest_data.jsonl and finished markets
are skipped on restart.

    python backtest_collect.py --probe        # quick check on 4 markets
    python backtest_collect.py --n 3000       # full run (~1 hour)
"""
import argparse, json, os, random, sys, time
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
SLUGS = {"BTC": "btc-updown-5m", "ETH": "eth-updown-5m",
         "SOL": "sol-updown-5m", "XRP": "xrp-updown-5m"}
CB_PRODUCT = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD"}
BN_SYMBOL = {"BTC": "BTCUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "XRP": "XRPUSD"}
OUT = "backtest_data.jsonl"
LOG = "backtest_collect.log"
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
            r = S.get(url, params=params, timeout=15)
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


def market_info(coin, wts):
    r = get(f"{GAMMA}/markets", {"slug": f"{SLUGS[coin]}-{wts}"})
    if r is None or r.status_code != 200:
        return None, f"gamma http {getattr(r, 'status_code', 'none')}"
    data = r.json()
    m = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) and data else None)
    if not m:
        return None, "no such market"
    keep = {k: m.get(k) for k in ("id", "question", "closed", "outcomePrices", "clobTokenIds",
                                  "outcomes", "startDate", "endDate", "volume", "eventStartTime")}
    # Polymarket sometimes exposes the official start price ("price to beat") in event metadata
    ev = (m.get("events") or [{}])[0] if isinstance(m.get("events"), list) else {}
    keep["eventMetadata"] = ev.get("eventMetadata") if isinstance(ev, dict) else None
    return keep, None


def price_history(token, wts):
    r = get(f"{CLOB}/prices-history", {"market": token, "startTs": wts - 120,
                                        "endTs": wts + 330, "fidelity": 1})
    if r is None or r.status_code != 200:
        return None, f"clob http {getattr(r, 'status_code', 'none')}"
    return r.json().get("history", []), None


def candles(coin, wts):
    start, end = wts - 3660, wts + 300
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = get(f"https://api.exchange.coinbase.com/products/{CB_PRODUCT[coin]}/candles",
            {"granularity": 60, "start": iso(start), "end": iso(end)})
    if r is not None and r.status_code == 200 and isinstance(r.json(), list) and r.json():
        # Coinbase rows: [time, low, high, open, close, volume], newest first
        rows = sorted([[c[0], c[3], c[2], c[1], c[4]] for c in r.json()])
        return rows, "coinbase"
    r = get("https://api.binance.us/api/v3/klines",
            {"symbol": BN_SYMBOL[coin], "interval": "1m", "startTime": start * 1000,
             "endTime": end * 1000, "limit": 1000})
    if r is not None and r.status_code == 200 and isinstance(r.json(), list) and r.json():
        rows = [[int(k[0]) // 1000, float(k[1]), float(k[2]), float(k[3]), float(k[4])] for k in r.json()]
        return rows, "binance.us"
    return None, "no candles"


def collect(coin, wts, source):
    rec = {"coin": coin, "wts": wts, "source": source}
    info, err = market_info(coin, wts)
    if err:
        rec["error"] = err
        return rec
    rec["market"] = info
    try:
        toks = info["clobTokenIds"]
        toks = json.loads(toks) if isinstance(toks, str) else toks
        rec["hist_up"], err = price_history(str(toks[0]), wts)
        if err:
            rec["error"] = err
        time.sleep(0.1)
        rec["hist_down"], _ = price_history(str(toks[1]), wts)
    except Exception as e:
        rec["error"] = f"tokens: {e}"
    rec["candles"], rec["candle_src"] = candles(coin, wts)
    return rec


def validation_markets():
    """Markets the sim bots traded, so the method can be checked against reality."""
    out = set()
    for f in ("sim_cl_late_trades.json", "sim_cl_trades.json"):
        if os.path.exists(f):
            try:
                for t in json.load(open(f, encoding="utf-8")):
                    if t.get("_interval", 300) == 300 and t.get("coin") in SLUGS:
                        out.add((t["coin"], int(t["_window_ts"])))
            except Exception:
                pass
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3000, help="random historical markets to sample")
    ap.add_argument("--since", default="2026-02-12", help="earliest date to sample from")
    ap.add_argument("--probe", action="store_true", help="quick check on 4 markets, then stop")
    ap.add_argument("--validation", type=int, default=400, help="max sim-bot markets to include")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    t0 = int(datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc).timestamp())
    t1 = int(time.time()) - 3600
    rng = random.Random(a.seed)
    windows = (t1 - t0) // 300

    if a.probe:
        todo = [(c, t0 + 300 * rng.randrange(windows), "probe") for c in ("BTC", "ETH", "SOL", "XRP")]
        for coin, wts, src in todo:
            r = collect(coin, wts, src)
            h = r.get("hist_up") or []
            c = r.get("candles") or []
            log(f"PROBE {coin} {datetime.fromtimestamp(wts, timezone.utc):%Y-%m-%d %H:%M} UTC | "
                f"market: {'ok' if r.get('market') else r.get('error')} | "
                f"result: {(r.get('market') or {}).get('outcomePrices')} | "
                f"price points: {len(h)} {h[:3]} | candles: {len(c)} from {r.get('candle_src')} | "
                f"price-to-beat field: {(r.get('market') or {}).get('eventMetadata')}")
        return

    done = set()
    if os.path.exists(OUT):
        for line in open(OUT, encoding="utf-8"):
            try:
                x = json.loads(line)
                done.add((x["coin"], x["wts"]))
            except Exception:
                pass

    val = validation_markets()
    rng.shuffle(val)
    todo = [(c, w, "validation") for c, w in val[:a.validation]]
    sample = set()
    while len(sample) < a.n:
        sample.add((rng.choice(list(SLUGS)), t0 + 300 * rng.randrange(windows)))
    todo += [(c, w, "random") for c, w in sorted(sample)]
    todo = [x for x in todo if (x[0], x[1]) not in done]
    log(f"Backtest collector: {len(todo)} markets to fetch ({len(done)} already done). Writing to {OUT}")

    ok = errs = 0
    started = time.time()
    for i, (coin, wts, src) in enumerate(todo, 1):
        rec = collect(coin, wts, src)
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        if rec.get("error") or not rec.get("hist_up") or not rec.get("candles"):
            errs += 1
        else:
            ok += 1
        if i == 25 and ok == 0:
            log("ABORT: the first 25 markets returned no usable price history or candles - "
                "check backtest_collect.log and send it to Claude")
            sys.exit(2)
        if i % 50 == 0 or i == len(todo):
            rate = i / max(1, time.time() - started)
            log(f"{i}/{len(todo)} done | complete {ok} | missing data {errs} | "
                f"~{(len(todo) - i) / max(rate, 0.01) / 60:.0f} min left")
        time.sleep(0.15)
    log("FINISHED")


if __name__ == "__main__":
    main()
