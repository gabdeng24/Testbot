"""
fair_value_bot.py — independent "fair price" strategy for Polymarket crypto
Up/Down markets. Simulation only: places NO real orders.

WHY THIS EXISTS
The original bot only bet when the crowd (5-min and 15-min market prices)
agreed with its technical signals. That means buying at a price that already
reflects the crowd's opinion, so it can't beat the crowd by construction.
It also never looked at the one number these markets resolve on: the coin's
price at the START of the window.

HOW THIS ONE DECIDES
Each market resolves "Up" if the coin ends the window at or above its price
at the window start. So at any moment:

    P(Up) = Φ( ln(now / start) / (σ · √seconds_left) )

where σ is the coin's recent per-second volatility (from the last hour of
1-minute candles) and Φ is the normal CDF. That's the model's own fair price,
computed without looking at what the crowd thinks.

It then reads the REAL order book for both the Up and Down tokens and buys
whichever side the market is selling below fair value, by at least --edge
after fees. No Claude veto: this is pure arithmetic.

CAVEATS
- Polymarket resolves on a Chainlink price feed; this uses Kraken prices,
  which can differ by a few hundredths of a percent. That matters most when
  the coin is sitting right at the start price.
- Fast professional bots price these markets with the same idea, so any
  gaps may be small or brief. The point is to measure whether they exist.

USAGE
    python fair_value_bot.py --tag sim_fv                 # 5-min markets
    python fair_value_bot.py --tag sim_fv15 --interval 15 # 15-min markets
    python sim_report.py sim_fv                           # results
Files: <tag>_portfolio.json, <tag>_trades.json, <tag>_attempts.json,
       <tag>_model.jsonl (every model-vs-market snapshot, for calibration),
       <tag>_bot.log
"""

import argparse
import json
import logging
import math
import os
import sys
import time
from datetime import datetime

# Windows consoles default to a legacy encoding; keep logging from erroring.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import crypto_bot_market as cb
import polymarket_fees as fees

log = logging.getLogger("fair_value")

BET_USD = 1.0


# ── Model ─────────────────────────────────────────────────────────────────────

def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_up(start: float, now: float, sd_1m: float, secs_left: float) -> float:
    """Probability the coin finishes the window at or above `start`."""
    if secs_left <= 0:
        return 1.0 if now >= start else 0.0
    sigma = sd_1m * math.sqrt(secs_left / 60.0)
    if sigma <= 0:
        return 1.0 if now >= start else 0.0
    return norm_cdf(math.log(now / start) / sigma)


def one_minute_vol(closes: list) -> float:
    """Std dev of 1-minute log returns."""
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 20:
        return 0.0
    m = sum(rets) / len(rets)
    return math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))


def fee_per_share(price: float) -> float:
    return fees.taker_fee(1.0, price, "crypto")


# ── Market data (Kraken) with light caching ───────────────────────────────────

class PriceData:
    """Caches 1-minute candles per coin; refreshes on a new window or every 5 min."""

    def __init__(self):
        self.candles = {}      # symbol -> list of ohlcv
        self.fetched_at = {}   # symbol -> unix time

    def candles_for(self, symbol: str, window_ts: int) -> list:
        c = self.candles.get(symbol)
        stale = time.time() - self.fetched_at.get(symbol, 0) > 300
        has_start = c and any(row[0] // 1000 == window_ts for row in c)
        if c is None or stale or not has_start:
            c = cb.exchange.fetch_ohlcv(symbol, "1m", limit=90)
            self.candles[symbol] = c
            self.fetched_at[symbol] = time.time()
        return c

    def start_price(self, symbol: str, window_ts: int):
        for row in self.candles_for(symbol, window_ts):
            if row[0] // 1000 == window_ts:
                return row[1]          # open of the candle at window start
        return None

    def vol(self, symbol: str, window_ts: int) -> float:
        closes = [row[4] for row in self.candles_for(symbol, window_ts)[-61:]]
        return one_minute_vol(closes)


def current_prices(symbols: list) -> dict:
    t = cb.exchange.fetch_tickers(symbols)
    return {s: t[s]["last"] for s in symbols if s in t and t[s].get("last")}


# ── Bookkeeping ───────────────────────────────────────────────────────────────

def log_attempt(rec: dict):
    attempts = []
    if os.path.exists(cb.ATTEMPTS_FILE):
        try:
            with open(cb.ATTEMPTS_FILE) as f:
                attempts = json.load(f)
        except Exception:
            pass
    attempts.append(rec)
    with open(cb.ATTEMPTS_FILE, "w") as f:
        json.dump(attempts[-20000:], f, indent=1)


def log_model(path: str, rec: dict):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def record_fill(market: dict, side: str, token_id: str, ask: float, avg: float,
                p_model: float, edge: float, secs_left: int, start: float, now: float):
    cost = fees.market_order_cost(BET_USD, avg, "crypto")
    p_side = p_model if side == "YES" else 1 - p_model
    rec = {
        "market_id":     market.get("id", ""),
        "question":      market.get("question", ""),
        "coin":          market["_coin"],
        "bet_side":      side,
        "token_id":      token_id,
        "bet_price":     ask,
        "decided_price": ask,
        "best_ask":      ask,
        "bet_size":      BET_USD,
        "fill_price":    round(avg, 4),
        "net_shares":    cost["net_shares"],
        "fee_usd":       cost["fee_usd"],
        "model_prob":    round(p_side, 4),
        "edge":          round(edge, 4),
        "seconds_left":  secs_left,
        "start_price":   start,
        "price_at_bet":  now,
        "opened_at":     datetime.now().isoformat(),
        "_window_ts":    market["_window_ts"],
        "_interval":     market["_interval"],
    }
    cb.portfolio["cash"] -= BET_USD
    cb.portfolio["positions"][rec["market_id"]] = {**rec, "resolved": False}
    cb.portfolio["total_bets"] += 1

    trades = []
    if os.path.exists(cb.TRADES_FILE):
        try:
            with open(cb.TRADES_FILE) as f:
                trades = json.load(f)
        except Exception:
            pass
    trades.append(rec)
    with open(cb.TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)
    cb.save_portfolio()
    log.info(f"[FV-FILL] {market['_coin']} {side} @ {avg:.3f} | model {p_side:.3f} "
             f"| edge {edge:+.3f} | {secs_left}s left | {market.get('question','')[:50]}")


# ── One evaluation per market ─────────────────────────────────────────────────

def evaluate(market: dict, args, prices: dict, pdata: PriceData, model_log: str):
    coin = market["_coin"]
    symbol = cb.COINS[coin]
    window_ts = market["_window_ts"]
    interval = market["_interval"]
    secs_left = int(window_ts + interval - time.time())

    if secs_left > args.window[1] or secs_left < args.window[0]:
        return
    if market.get("id", "") in cb.portfolio["positions"]:
        return

    now = prices.get(symbol)
    start = pdata.start_price(symbol, window_ts)
    sd = pdata.vol(symbol, window_ts)
    if not now or not start or sd <= 0:
        return

    try:
        raw = market.get("clobTokenIds")
        yes_tok, no_tok = (json.loads(raw) if isinstance(raw, str) else raw)[:2]
    except Exception:
        return

    try:
        yes_asks = cb.fetch_asks(str(yes_tok))
        no_asks = cb.fetch_asks(str(no_tok))
    except Exception as e:
        log.warning(f"order book fetch failed for {coin}: {e}")
        return
    if not yes_asks or not no_asks:
        return

    p = prob_up(start, now, sd, secs_left)
    ask_yes, ask_no = yes_asks[0][0], no_asks[0][0]
    edge_yes = p - ask_yes - fee_per_share(ask_yes)
    edge_no = (1 - p) - ask_no - fee_per_share(ask_no)

    log_model(model_log, {
        "ts": int(time.time()), "coin": coin, "window_ts": window_ts,
        "interval": interval, "secs_left": secs_left, "start": start, "now": now,
        "sd_1m": round(sd, 6), "p_up": round(p, 4),
        "ask_yes": ask_yes, "ask_no": ask_no,
    })

    side, ask, book, tok, edge = (("YES", ask_yes, yes_asks, yes_tok, edge_yes)
                                  if edge_yes >= edge_no else
                                  ("NO", ask_no, no_asks, no_tok, edge_no))
    if edge < args.edge:
        return

    p_side = p if side == "YES" else 1 - p
    base = {
        "ts": datetime.now().isoformat(), "market_id": market.get("id"), "coin": coin,
        "bet_side": side, "decided_price": ask, "best_ask": ask, "gap": 0.0,
        "seconds_left": secs_left, "model_prob": round(p_side, 4), "edge": round(edge, 4),
    }

    # Model disagrees wildly with the market: more likely our data is stale or
    # our Kraken price differs from Polymarket's Chainlink feed than a real gift.
    if abs(p_side - ask) > args.max_divergence:
        log_attempt({**base, "status": "blocked",
                     "reason": f"model {p_side:.2f} vs ask {ask:.2f} diverge > {args.max_divergence}"})
        log.info(f"[FV-SKIP] {coin} {side}: model {p_side:.2f} vs ask {ask:.2f} — divergence too large")
        return

    avg, _ = cb.walk_book(book, BET_USD)
    if avg is None:
        log_attempt({**base, "status": "blocked", "reason": "book too thin"})
        return
    # Re-check edge at the actual average fill price (walking the book)
    real_edge = p_side - avg - fee_per_share(avg)
    if real_edge < args.edge:
        log_attempt({**base, "status": "blocked",
                     "reason": f"edge gone after walking book (avg {avg:.3f})"})
        return

    log_attempt({**base, "status": "filled"})
    record_fill(market, side, str(tok), ask, avg, p, real_edge, secs_left, start, now)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Fair-value Polymarket crypto simulator (no real orders)")
    ap.add_argument("--tag", default="sim_fv")
    ap.add_argument("--interval", type=int, choices=[5, 15], default=5)
    ap.add_argument("--edge", type=float, default=0.05,
                    help="Minimum model edge after fees to bet (default 0.05 = 5 cents)")
    ap.add_argument("--window", default=None,
                    help="'MIN,MAX' seconds left during which bets are allowed "
                         "(default 20,280 for 5-min; 60,840 for 15-min)")
    ap.add_argument("--max-divergence", type=float, default=0.30,
                    help="Skip if model and market disagree by more than this")
    ap.add_argument("--scan", type=float, default=5.0, help="Seconds between scans")
    args = ap.parse_args()

    interval = 900 if args.interval == 15 else 300
    if args.window:
        lo, hi = (int(x) for x in args.window.split(","))
    else:
        lo, hi = (60, 840) if interval == 900 else (20, 280)
    args.window = (lo, hi)
    slugs = cb.COIN_SLUGS_15M if interval == 900 else cb.COIN_SLUGS_5M

    # Point the shared bookkeeping at this variant's files
    cb.SIM_MODE = True
    cb.TRADES_FILE = f"{args.tag}_trades.json"
    cb.PORTFOLIO_FILE = f"{args.tag}_portfolio.json"
    cb.STATS_FILE = f"{args.tag}_stats.json"
    cb.ATTEMPTS_FILE = f"{args.tag}_attempts.json"
    model_log = f"{args.tag}_model.jsonl"

    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):
            root.removeHandler(h)
    fh = logging.FileHandler(f"{args.tag}_bot.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))
    root.addHandler(fh)

    log.info(f"Fair-value SIM [{args.tag}] | {interval//60}-min markets | edge >= {args.edge} "
             f"| window {lo}-{hi}s left | max divergence {args.max_divergence} | NO real orders")
    cb.load_portfolio()
    pdata = PriceData()
    symbols = [cb.COINS[c] for c in cb.COINS]
    scans = 0

    while True:
        try:
            scans += 1
            cb.resolve_positions()
            secs_left = cb.get_seconds_until_close(interval)
            if lo <= secs_left <= hi:
                prices = current_prices(symbols)
                for coin in cb.COINS:
                    m = cb.fetch_market_by_slug(coin, slugs, interval)
                    if m:
                        evaluate(m, args, prices, pdata, model_log)
            if scans % 60 == 0:
                res = cb.portfolio.get("resolved", [])
                w = sum(1 for r in res if r.get("outcome") == "WIN")
                pnl = sum(r.get("profit", 0) for r in res)
                log.info(f"[FV-STATUS] scans={scans} bets={cb.portfolio['total_bets']} "
                         f"resolved={len(res)} wins={w} P&L={pnl:+.2f} "
                         f"open={len(cb.portfolio['positions'])}")
            time.sleep(args.scan)
        except KeyboardInterrupt:
            log.info("Stopped.")
            break
        except Exception as e:
            log.error(f"Error: {e}", exc_info=True)
            time.sleep(15)


if __name__ == "__main__":
    main()
