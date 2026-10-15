"""
backtest_hourly_collect.py - downloads past Polymarket HOURLY crypto Up/Down
markets ("Bitcoin Up or Down - April 17, 12PM ET") for a trade-based backtest.

These markets settle on Binance's 1-hour candle (BTC/USDT etc.): Up if the
hour closes at or above its open. For each sampled hour it saves:
  - the market result, conditionId and tokens (gamma API)
  - every taker trade during the hour (data API)
  - Binance 1-minute candles for the hour before and the hour itself
Public data only. No orders, no keys. Resumable.

    python backtest_hourly_collect.py --probe
    python backtest_hourly_collect.py --n 1500
"""
import argparse, json, os, random, sys, time
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import requests
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                       # Windows without the tzdata package
    ET = None

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
NAMES = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "xrp"}
BINANCE = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT", "XRP": "XRPUSDT"}
CB = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD"}
OUT, LOG = "backtest_hourly.jsonl", "backtest_hourly.log"
S = requests.Session()
S.headers["User-Agent"] = "polybot-backtest/1.0"
SLUG_USED = {}


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


def et_parts(ts):
    """(month, day, hour label, year) of an hour start, in New York time."""
    if ET is not None:
        d = datetime.fromtimestamp(ts, ET)
    else:                               # fallback: US daylight time rule
        d = datetime.fromtimestamp(ts, timezone.utc)
        y = d.year
        mar = datetime(y, 3, 8, tzinfo=timezone.utc); mar += timedelta(days=(6 - mar.weekday()) % 7)
        nov = datetime(y, 11, 1, tzinfo=timezone.utc); nov += timedelta(days=(6 - nov.weekday()) % 7)
        d = d - timedelta(hours=4 if mar + timedelta(hours=7) <= d < nov + timedelta(hours=6) else 5)
    h = d.hour % 12 or 12
    return d.strftime("%B").lower(), d.day, f"{h}{'am' if d.hour < 12 else 'pm'}", d.year


def slug_candidates(coin, ts):
    mon, day, hr, yr = et_parts(ts)
    n = NAMES[coin]
    return [f"{n}-up-or-down-{mon}-{day}-{hr}-et",
            f"{n}-up-or-down-{mon}-{day}-{yr}-{hr}-et",
            f"{n}-up-or-down-{mon}-{day}-{hr}"]


def first(d):
    return d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) and d else None)


def find_market(coin, ts):
    for i, slug in enumerate(slug_candidates(coin, ts)):
        for params in ({"slug": slug, "closed": "true"}, {"slug": slug}):
            r = get(f"{GAMMA}/markets", params)
            if r is None or r.status_code != 200:
                continue
            try:
                m = first(r.json())
            except Exception:
                m = None
            if m:
                SLUG_USED[i] = SLUG_USED.get(i, 0) + 1
                return m, slug
        ev = get(f"{GAMMA}/events", {"slug": slug})
        try:
            e = first(ev.json()) if ev is not None and ev.status_code == 200 else None
        except Exception:
            e = None
        if e and e.get("markets"):
            SLUG_USED[f"event{i}"] = SLUG_USED.get(f"event{i}", 0) + 1
            return e["markets"][0], slug
    return None, slug_candidates(coin, ts)[0]


def trades(cid, t0, t1):
    """All taker trades in [t0, t1], paged in 10-minute slices (each has its own offset budget)."""
    out = []
    for s0 in range(t0, t1, 600):
        s1 = min(t1, s0 + 600)
        offset = 0
        while True:
            r = get(f"{DATA}/trades", {"market": cid, "limit": 1000, "offset": offset,
                                       "start": s0, "end": s1 - 1, "takerOnly": "true"})
            if r is None or r.status_code != 200:
                return out, f"data-api http {getattr(r, 'status_code', 'none')}"
            page = r.json()
            if not isinstance(page, list):
                return out, "unexpected response"
            for t in page:
                try:
                    ts = int(t["timestamp"]); ts = ts // 1000 if ts > 10**12 else ts
                    out.append([ts, int(t.get("outcomeIndex", 0)), 1 if t.get("side") == "BUY" else -1,
                                float(t["price"]), float(t.get("size") or 0)])
                except Exception:
                    continue
            if len(page) < 1000 or offset >= 9000:
                break
            offset += 1000
            time.sleep(0.08)
    out = sorted(set(map(tuple, out)))
    return [list(x) for x in out], None


def candles(coin, ts):
    start, end = ts - 3600, ts + 3600
    for base in ("https://data-api.binance.vision", "https://api.binance.com"):
        r = get(f"{base}/api/v3/klines", {"symbol": BINANCE[coin], "interval": "1m",
                                          "startTime": start * 1000, "endTime": end * 1000 - 1, "limit": 1000})
        if r is not None and r.status_code == 200 and isinstance(r.json(), list) and r.json():
            return [[int(k[0]) // 1000, float(k[1]), float(k[2]), float(k[3]), float(k[4])] for k in r.json()], "binance"
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = get(f"https://api.exchange.coinbase.com/products/{CB[coin]}/candles",
            {"granularity": 60, "start": iso(start), "end": iso(end - 60)})
    if r is not None and r.status_code == 200 and isinstance(r.json(), list) and r.json():
        return sorted([[c[0], c[3], c[2], c[1], c[4]] for c in r.json()]), "coinbase"
    return None, "no candles"


def collect(coin, ts, src):
    rec = {"coin": coin, "wts": ts, "interval": 3600, "source": src}
    m, slug = find_market(coin, ts)
    rec["slug"] = slug
    if not m:
        rec["error"] = "no such market"
        return rec
    rec["market"] = {k: m.get(k) for k in ("id", "question", "closed", "outcomePrices", "clobTokenIds",
                                           "conditionId", "volume", "startDate", "endDate")}
    cid = m.get("conditionId")
    if not cid:
        rec["error"] = "no conditionId"
        return rec
    rec["trades"], err = trades(cid, ts - 30, ts + 3630)
    if err:
        rec["error"] = err
    rec["candles"], rec["candle_src"] = candles(coin, ts)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1500)
    ap.add_argument("--since", default="2026-02-12")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()
    t0 = int(datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc).timestamp()) // 3600 * 3600
    t1 = int(time.time()) // 3600 * 3600 - 2 * 3600
    rng = random.Random(a.seed)
    hours = (t1 - t0) // 3600

    if a.probe:
        known = int(datetime(2026, 4, 17, 16, tzinfo=timezone.utc).timestamp())   # "April 17, 12PM ET"
        todo = [("BTC", known, "probe-known"), ("ETH", t1 - 24 * 3600, "probe"),
                ("SOL", t0 + 3600 * rng.randrange(hours), "probe")]
        for coin, ts, src in todo:
            r = collect(coin, ts, src)
            tr = r.get("trades") or []
            log(f"PROBE[{src}] {coin} {datetime.fromtimestamp(ts, timezone.utc):%Y-%m-%d %H:00} UTC slug={r['slug']} | "
                f"market: {'ok' if r.get('market') else r.get('error')} | "
                f"title: {(r.get('market') or {}).get('question')} | result: {(r.get('market') or {}).get('outcomePrices')} | "
                f"trades: {len(tr)} | candles: {len(r.get('candles') or [])} from {r.get('candle_src')}")
        log(f"PROBE slug formats that worked: {SLUG_USED or 'none'}")
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
    sample = set()
    while len(sample) < a.n:
        sample.add((rng.choice(list(NAMES)), t0 + 3600 * rng.randrange(hours)))
    todo = [x for x in sorted(sample) if x not in done]
    log(f"Hourly collector: {len(todo)} markets to fetch ({len(done)} already done) -> {OUT}")
    ok = bad = 0
    started = time.time()
    for i, (coin, ts) in enumerate(todo, 1):
        rec = collect(coin, ts, "random")
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        if rec.get("error") or not rec.get("trades") or not rec.get("candles"):
            bad += 1
        else:
            ok += 1
        if i == 25 and ok == 0:
            log("ABORT: the first 25 markets returned no usable data - tell Claude")
            sys.exit(2)
        if i % 25 == 0 or i == len(todo):
            rate = i / max(1, time.time() - started)
            log(f"{i}/{len(todo)} done | complete {ok} | missing {bad} | slugs {SLUG_USED} | "
                f"~{(len(todo) - i) / max(rate, 0.01) / 60:.0f} min left")
        time.sleep(0.1)
    log("FINISHED")


if __name__ == "__main__":
    main()
