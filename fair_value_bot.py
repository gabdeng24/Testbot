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
    python fair_value_bot.py --tag sim_cl --price-feed chainlink   # Chainlink prices
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

# Windows: clicking inside a console window starts "QuickEdit" text selection,
# which freezes the program the next time it prints. Turn that off for this window.
if os.name == "nt":
    try:
        import ctypes
        _k32 = ctypes.windll.kernel32
        _h = _k32.GetStdHandle(-10)                 # console input handle
        _mode = ctypes.c_uint32()
        if _k32.GetConsoleMode(_h, ctypes.byref(_mode)):
            _k32.SetConsoleMode(_h, (_mode.value & ~0x0040) | 0x0080)  # QuickEdit off
    except Exception:
        pass
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
PRICE_FEED = "kraken"


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



class ChainlinkFeed:
    """
    Live Chainlink prices from Polymarket's own real-time data socket - the same
    feed these markets settle on. Keeps ~2 hours of ticks per coin so we can look
    up the exact price at each window's start.
    No history is sent on connect, so a window that started before we connected
    is skipped (we can't know its start price).
    """
    URL = "wss://ws-live-data.polymarket.com"
    KEEP_MS = 2 * 3600 * 1000

    def __init__(self, coins, connect=True):
        import threading
        from collections import deque
        self.ticks = {c: deque() for c in coins}          # coin -> (ts_ms, price)
        self.sym_to_coin = {f"{c.lower()}/usd": c for c in coins}
        self.lock = threading.Lock()
        self.connected_since_ms = None
        self.msgs = 0
        self.last_msg_at = time.time()
        if connect:
            threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        import websocket, threading
        sub = json.dumps({"action": "subscribe", "subscriptions": [
            {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}]})

        def on_open(ws):
            ws.send(sub)
            self.connected_since_ms = int(time.time() * 1000)
            log.info("[CHAINLINK] connected and subscribed")

            def ping():
                # Keepalive + watchdog: a dropped connection can hang silently,
                # so force a reconnect if no prices arrive for 30 seconds.
                self.last_msg_at = time.time()
                while ws.sock and ws.sock.connected:
                    try:
                        ws.send("PING")
                    except Exception:
                        break
                    if time.time() - self.last_msg_at > 30:
                        log.warning("[CHAINLINK] no prices for 30s - reconnecting")
                        try:
                            ws.close()
                        except Exception:
                            pass
                        break
                    time.sleep(5)
            threading.Thread(target=ping, daemon=True).start()

        def on_message(ws, raw):
            self._ingest(raw)

        def on_close(ws, *a):
            self.connected_since_ms = None
            log.warning("[CHAINLINK] disconnected - reconnecting")

        while True:
            try:
                websocket.WebSocketApp(self.URL, on_open=on_open, on_message=on_message,
                                       on_close=on_close,
                                       on_error=lambda ws, e: log.warning(f"[CHAINLINK] {e}")
                                       ).run_forever()
            except Exception as e:
                log.warning(f"[CHAINLINK] socket error: {e}")
            self.connected_since_ms = None
            time.sleep(3)

    def _ingest(self, raw):
        try:
            m = json.loads(raw)
        except Exception:
            return                                        # PONG etc.
        if not isinstance(m, dict) or m.get("topic") != "crypto_prices_chainlink":
            return
        p = m.get("payload") or {}
        sym = p.get("symbol", "")
        # Normally one price per message; accept a batch ("data": [...]) too.
        points = p.get("data") if isinstance(p.get("data"), list) else [p]
        coin = self.sym_to_coin.get(str(sym).lower())
        for pt in points:
            c = self.sym_to_coin.get(str(pt.get("symbol", sym)).lower()) or coin
            if not c or pt.get("value") is None:
                continue
            ts = int(pt.get("timestamp") or m.get("timestamp"))
            val = float(pt["value"])
            with self.lock:
                dq = self.ticks[c]
                if not dq or ts >= dq[-1][0]:
                    dq.append((ts, val))
                while dq and dq[0][0] < ts - self.KEEP_MS:
                    dq.popleft()
            self.msgs += 1
            self.last_msg_at = time.time()

    def current(self, coin, max_age_s=10):
        with self.lock:
            dq = self.ticks.get(coin)
            if not dq:
                return None
            ts, val = dq[-1]
        return val if time.time() * 1000 - ts <= max_age_s * 1000 else None

    def start_price(self, coin, window_ts):
        """Last Chainlink price at or before the window start (needs a tick within 5s of it)."""
        start_ms = window_ts * 1000
        with self.lock:
            dq = list(self.ticks.get(coin, ()))
        before = [t for t in dq if t[0] <= start_ms]
        after = [t for t in dq if t[0] > start_ms]
        if not before or not after or start_ms - before[-1][0] > 5000:
            return None
        return before[-1][1]

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
        "price_feed":    PRICE_FEED,
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

def evaluate(market: dict, args, prices: dict, pdata: PriceData, model_log: str, feed=None):
    coin = market["_coin"]
    symbol = cb.COINS[coin]
    window_ts = market["_window_ts"]
    interval = market["_interval"]
    secs_left = int(window_ts + interval - time.time())

    if secs_left > args.window[1] or secs_left < args.window[0]:
        return
    if market.get("id", "") in cb.portfolio["positions"]:
        return

    if feed is not None:
        now = feed.current(coin)
        start = feed.start_price(coin, window_ts)
    else:
        now = prices.get(symbol)
        start = pdata.start_price(symbol, window_ts)
    sd = pdata.vol(symbol, window_ts)            # volatility level from Kraken candles
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
        "ask_yes": ask_yes, "ask_no": ask_no, "feed": "chainlink" if feed else "kraken",
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
    ap.add_argument("--price-feed", choices=["kraken", "chainlink"], default="kraken",
                    help="Where start and current prices come from. 'chainlink' uses "
                         "Polymarket's live Chainlink feed, the one these markets settle on")
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
             f"| window {lo}-{hi}s left | max divergence {args.max_divergence} "
             f"| prices: {args.price_feed} | NO real orders")
    cb.load_portfolio()
    global PRICE_FEED
    PRICE_FEED = args.price_feed
    feed = ChainlinkFeed(list(cb.COINS)) if args.price_feed == "chainlink" else None
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
                        evaluate(m, args, prices, pdata, model_log, feed)
            if scans % 60 == 0:
                res = cb.portfolio.get("resolved", [])
                w = sum(1 for r in res if r.get("outcome") == "WIN")
                pnl = sum(r.get("profit", 0) for r in res)
                if feed is not None:
                    log.info(f"[CHAINLINK] {feed.msgs} price updates so far | "
                             + " ".join(f"{c}={feed.current(c)}" for c in cb.COINS))
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
