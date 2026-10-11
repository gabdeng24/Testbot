"""
Polymarket 5-Minute Crypto Prediction Bot
==========================================
Hybrid approach: technical signals as fast pre-filter, Claude as veto layer.

  STEP 1 — Technical signals (fast pre-filter)
    RSI, Momentum, VWAP deviation, Bollinger position, Volume spike.
    Only proceeds if composite score exceeds threshold.

  STEP 2 — Claude veto layer (context + news awareness)
    If signals look good, Claude checks for:
      - Breaking news that overrides the technical signal
      - Macro context (big recent run makes oversold RSI less reliable)
      - Whether the specific market question makes sense to bet given current price
    Claude returns APPROVE or VETO with reasoning.

SETUP:
  pip install requests ccxt pandas anthropic

  export ANTHROPIC_API_KEY="sk-ant-..."
  python crypto_prediction_bot.py --paper
  python crypto_prediction_bot.py --stats
"""

import os
import sys
import json
import time
import logging
import argparse
import requests
from datetime import datetime, timezone, date
from collections import deque
from typing import Optional
import anthropic

# Shared cost model — makes paper trading reflect real Polymarket fees
import polymarket_fees as fees

# This is the MARKET-ORDER bot (Bot A): pays taker fees, fills at the ask.
BOT_MODE = "market"

try:
    import ccxt
    import pandas as pd
    CCXT_AVAILABLE = True
except ImportError:
    CCXT_AVAILABLE = False
    print("ERROR: Run: pip install ccxt pandas")
    sys.exit(1)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("market_bot.log"),
    ],
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
GAMMA_API_URL    = "https://gamma-api.polymarket.com"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SCAN_INTERVAL    = 10        # seconds between scans — needs to be fast for 5-min markets
MAX_BET_USD      = 3.0       # CHANGED: cap at $3 (was $10) — variance was too high on bigger bets
MIN_BET_USD      = 2.0
DAILY_LOSS_LIMIT = 0.10      # CHANGED: pause for 24h if -10% in a day (was 15%)
MAX_POSITIONS    = 8         # more positions OK for short-term

# Signal thresholds — TIGHTENED based on 1,534-trade analysis
# Data showed: composite < 0.20 had no edge over noise (55-58% across all bins)
# Data showed: 3-way agreement won 65%, disagreement won 53% (barely break-even)
MIN_COMPOSITE       = 0.20   # CHANGED: was 0.25 → patched down to 0.001 by supervisor
                              # Set to 0.20 — eliminates the noise floor
MIN_MARKET_EDGE     = 0.05
MIN_PAYOUT_ROI      = 0.10
REQUIRE_AGREES      = True   # CHANGED: was False — now require 5m crowd agreement
MIN_CORROBORATING   = 2      # CHANGED: was 1 — require at least 2/5 signals confirming
MAX_CROWD_PRICE     = 0.57   # skip bets when crowd is already too confident
REQUIRE_3WAY_AGREE  = True   # NEW: only bet when signal + 5m + 15m all agree (65% WR)

# Coins to track
COINS = {
    "BTC": "BTC/USD",
    "ETH": "ETH/USD",
    "SOL": "SOL/USD",
    "XRP": "XRP/USD",
}

STATS_FILE     = "market_stats.json"
TRADES_FILE    = "market_trades.json"
PORTFOLIO_FILE = "market_portfolio.json"   # persists portfolio across restarts

# ── LIVE MODE ─────────────────────────────────────────────────────────────────
# Activated by --live (paper stays the default). In live mode the bot:
#   - uses its OWN state files (live_*) so a paper instance runs alongside
#   - flattens bets to LIVE_BET_USD (Phase-3 validation = flat $1 bets)
#   - routes every approved decision through PolymarketExecutor's safety rails
#   - sends Telegram alerts (fills/errors via executor; outcomes/summary here)
LIVE_MODE     = False
LIVE_BANKROLL = 20.0
LIVE_BET_USD  = 1.0
EXECUTOR      = None      # set in run_bot() when LIVE_MODE

TELEGRAM_CREDS_PATH = os.path.expanduser("~/.telegram_credentials")
_tg = {"loaded": False, "token": None, "chat_id": None}

def telegram_alert(msg: str):
    """Send a Telegram message. Never raises; silently no-ops if unconfigured."""
    try:
        if not _tg["loaded"]:
            _tg["loaded"] = True
            if os.path.exists(TELEGRAM_CREDS_PATH):
                with open(TELEGRAM_CREDS_PATH) as f:
                    c = json.load(f)
                _tg["token"], _tg["chat_id"] = c.get("bot_token"), c.get("chat_id")
            else:
                log.warning(f"{TELEGRAM_CREDS_PATH} not found — Telegram alerts disabled")
        if not (_tg["token"] and _tg["chat_id"]):
            return
        requests.post(
            f"https://api.telegram.org/bot{_tg['token']}/sendMessage",
            json={"chat_id": _tg["chat_id"], "text": msg},
            timeout=10,
        )
    except Exception as e:
        log.debug(f"telegram_alert failed: {e}")

_summary = {"date": None}

def _maybe_daily_summary():
    """Once per day in live mode: Telegram summary of yesterday's results."""
    today = date.today().isoformat()
    if _summary["date"] is None:
        _summary["date"] = today
        return
    if _summary["date"] == today:
        return
    yday, _summary["date"] = _summary["date"], today
    try:
        rs = [r for r in portfolio.get("resolved", [])
              if str(r.get("resolved_at", "")).startswith(yday)]
        if not rs:
            return
        wins = sum(1 for r in rs if r.get("outcome") == "WIN")
        pnl  = sum(r.get("profit", 0) for r in rs)
        telegram_alert(f"📊 Daily summary {yday}\n"
                       f"{wins}W/{len(rs)-wins}L ({wins/len(rs):.0%}) | P&L {pnl:+.2f}")
    except Exception as e:
        log.debug(f"daily summary failed: {e}")


def save_portfolio():
    """Save portfolio state to disk so it survives restarts."""
    try:
        state = {
            "cash":                portfolio["cash"],
            "daily_start":         portfolio["daily_start"],
            "total_bets":          portfolio["total_bets"],
            "circuit_broken":      portfolio["circuit_broken"],
            "circuit_break_day":   str(portfolio["circuit_break_day"]) if portfolio["circuit_break_day"] else None,
            "circuit_break_until": portfolio.get("circuit_break_until"),
            "positions":           portfolio["positions"],
            "resolved":            portfolio["resolved"][-200:],
            "saved_at":            datetime.now().isoformat(),
        }
        with open(PORTFOLIO_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log.warning(f"Could not save portfolio state: {e}")


def load_portfolio():
    """Load portfolio state from disk if it exists."""
    if not os.path.exists(PORTFOLIO_FILE):
        log.info("No saved portfolio — starting fresh with $500")
        return

    try:
        with open(PORTFOLIO_FILE) as f:
            state = json.load(f)

        saved_at = state.get("saved_at", "unknown")
        portfolio["cash"]                = state.get("cash", 500.0)
        portfolio["daily_start"]         = state.get("daily_start", 500.0)
        portfolio["total_bets"]          = state.get("total_bets", 0)
        portfolio["circuit_broken"]      = state.get("circuit_broken", False)
        portfolio["circuit_break_until"] = state.get("circuit_break_until")
        portfolio["positions"]           = state.get("positions", {})
        portfolio["resolved"]            = state.get("resolved", [])

        cb_day = state.get("circuit_break_day")
        if cb_day:
            try:
                from datetime import date
                portfolio["circuit_break_day"] = date.fromisoformat(cb_day)
            except Exception:
                pass

        wins   = sum(1 for t in portfolio["resolved"] if t.get("outcome") == "WIN")
        losses = sum(1 for t in portfolio["resolved"] if t.get("outcome") == "LOSS")
        total  = wins + losses
        wr     = f"{wins/total:.0%}" if total else "N/A"

        log.info(
            f"Portfolio restored from {saved_at[:19]}: "
            f"Cash=${portfolio['cash']:.2f} | "
            f"Bets={portfolio['total_bets']} | "
            f"Win rate={wr} ({wins}W/{losses}L)"
        )
    except Exception as e:
        log.warning(f"Could not load portfolio state: {e} — starting fresh")

# ── Paper portfolio ───────────────────────────────────────────────────────────
portfolio = {
    "cash": 500.0,           # smaller starting bankroll for crypto bot
    "daily_start": 500.0,
    "positions": {},
    "circuit_broken": False,
    "circuit_break_day": None,
    "circuit_break_until": None,  # 24h pause timestamp
    "total_bets": 0,
    "resolved": [],
}

# ── Exchange ──────────────────────────────────────────────────────────────────
exchange = ccxt.kraken({"enableRateLimit": True})


# =============================================================================
# TECHNICAL SIGNALS
# =============================================================================

def fetch_ohlcv(symbol: str, timeframe: str = "1m", limit: int = 60) -> Optional[pd.DataFrame]:
    """Fetch recent OHLCV candles from Kraken."""
    try:
        data = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df
    except Exception as e:
        log.warning(f"OHLCV fetch failed for {symbol}: {e}")
        return None


def signal_rsi(df: pd.DataFrame, period: int = 14) -> tuple[float, float]:
    """
    RSI signal. Returns (score, rsi_value).
    score: -1 (overbought, bearish) to +1 (oversold, bullish)
    """
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))
    rsi_val = rsi.iloc[-1]

    if rsi_val < 30:
        score = 1.0    # very oversold → bullish
    elif rsi_val < 40:
        score = 0.5
    elif rsi_val > 70:
        score = -1.0   # very overbought → bearish
    elif rsi_val > 60:
        score = -0.5
    else:
        score = 0.0    # neutral

    return score, round(rsi_val, 1)


def signal_momentum(df: pd.DataFrame, period: int = 5) -> tuple[float, float]:
    """
    Short-term price momentum (rate of change over last N candles).
    Returns (score, momentum_pct).
    """
    if len(df) < period + 1:
        return 0.0, 0.0

    current = df["close"].iloc[-1]
    previous = df["close"].iloc[-period]
    momentum_pct = (current - previous) / previous * 100

    # Strong momentum tends to continue very short-term
    # Lowered thresholds from 0.2%/0.5% to 0.03%/0.10% — previous thresholds caused 94% zero scores
    if momentum_pct > 0.10:
        score = 0.8
    elif momentum_pct > 0.03:
        score = 0.4
    elif momentum_pct < -0.10:
        score = -0.8
    elif momentum_pct < -0.03:
        score = -0.4
    else:
        score = 0.0

    return score, round(momentum_pct, 3)


def signal_vwap(df: pd.DataFrame) -> tuple[float, float]:
    """
    VWAP deviation signal.
    Price above VWAP = bullish, below = bearish (mean reversion).
    Returns (score, deviation_pct).
    """
    try:
        df = df.copy()
        df["typical_price"] = (df["high"] + df["low"] + df["close"]) / 3
        df["cum_vol"] = df["volume"].cumsum()
        df["cum_tp_vol"] = (df["typical_price"] * df["volume"]).cumsum()
        df["vwap"] = df["cum_tp_vol"] / df["cum_vol"]

        current_price = df["close"].iloc[-1]
        vwap = df["vwap"].iloc[-1]
        deviation_pct = (current_price - vwap) / vwap * 100

        # Mean reversion: if far above VWAP, likely to pull back
        if deviation_pct > 0.3:
            score = -0.6   # overbought vs VWAP → bearish
        elif deviation_pct > 0.1:
            score = -0.2
        elif deviation_pct < -0.3:
            score = 0.6    # oversold vs VWAP → bullish
        elif deviation_pct < -0.1:
            score = 0.2
        else:
            score = 0.0

        return score, round(deviation_pct, 3)
    except Exception:
        return 0.0, 0.0


def signal_bollinger(df: pd.DataFrame, period: int = 20, std: float = 2.0) -> tuple[float, float]:
    """
    Bollinger Band position signal.
    Returns (score, band_position) where band_position is 0-1 (0=lower band, 1=upper band).
    """
    try:
        close = df["close"]
        mid = close.rolling(period).mean()
        band_std = close.rolling(period).std()
        upper = mid + std * band_std
        lower = mid - std * band_std

        current = close.iloc[-1]
        upper_val = upper.iloc[-1]
        lower_val = lower.iloc[-1]
        band_range = upper_val - lower_val

        if band_range == 0:
            return 0.0, 0.5

        position = (current - lower_val) / band_range  # 0 = at lower, 1 = at upper

        # Mean reversion: near upper band = bearish, near lower = bullish
        if position > 0.9:
            score = -0.9
        elif position > 0.75:
            score = -0.4
        elif position < 0.1:
            score = 0.9
        elif position < 0.25:
            score = 0.4
        else:
            score = 0.0

        return score, round(position, 3)
    except Exception:
        return 0.0, 0.5


def signal_volume(df: pd.DataFrame, period: int = 10) -> tuple[float, float]:
    """
    Volume spike signal. High volume confirms direction of last candle.
    Returns (score, volume_ratio).
    """
    try:
        avg_volume = df["volume"].iloc[-period-1:-1].mean()
        current_volume = df["volume"].iloc[-1]
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 1.0

        last_candle_bullish = df["close"].iloc[-1] > df["open"].iloc[-1]

        # Lowered thresholds from 1.5x/2.0x to 1.2x/1.5x — previous caused 93% zero scores
        if volume_ratio > 1.5:
            score = 0.7 if last_candle_bullish else -0.7
        elif volume_ratio > 1.2:
            score = 0.3 if last_candle_bullish else -0.3
        else:
            score = 0.0

        return score, round(volume_ratio, 2)
    except Exception:
        return 0.0, 1.0


def compute_composite(symbol: str) -> Optional[dict]:
    """
    Fetch OHLCV and compute all signals. Returns composite score and details.
    Also fetches 1-hour candles for trend filter — don't bet against the trend.
    """
    df = fetch_ohlcv(symbol, timeframe="1m", limit=60)
    if df is None or len(df) < 20:
        return None

    # ── Higher timeframe trend filter ─────────────────────────────────────────
    # Fetch 1-hour candles to determine macro trend
    # If hourly trend is strongly down, RSI oversold just means "trend is strong"
    # not "bounce incoming" — this was the core bug causing losses
    df_1h = fetch_ohlcv(symbol, timeframe="1h", limit=20)
    trend_direction = "NEUTRAL"
    trend_strength  = 0.0
    if df_1h is not None and len(df_1h) >= 5:
        # Simple trend: compare current close to 5-period EMA
        ema5 = df_1h["close"].ewm(span=5).mean().iloc[-1]
        ema10 = df_1h["close"].ewm(span=10).mean().iloc[-1]
        current_1h = df_1h["close"].iloc[-1]
        pct_vs_ema5 = (current_1h - ema5) / ema5 * 100

        if ema5 > ema10 and pct_vs_ema5 > 0.1:
            trend_direction = "UP"
            trend_strength  = min(abs(pct_vs_ema5) / 0.5, 1.0)
        elif ema5 < ema10 and pct_vs_ema5 < -0.1:
            trend_direction = "DOWN"
            trend_strength  = min(abs(pct_vs_ema5) / 0.5, 1.0)

    rsi_score,  rsi_val  = signal_rsi(df)
    mom_score,  mom_val  = signal_momentum(df)
    vwap_score, vwap_val = signal_vwap(df)
    boll_score, boll_val = signal_bollinger(df)
    vol_score,  vol_val  = signal_volume(df)

    # Weighted composite
    # Momentum weighted highest — most reliable for 5-min direction in trending markets
    # RSI weight reduced — overbought in uptrend doesn't mean reversal
    weights = {
        "rsi":      0.15,   # reduced from 0.25 — RSI alone is unreliable in trends
        "momentum": 0.40,   # increased from 0.30 — most predictive for 5-min
        "vwap":     0.20,
        "bollinger":0.15,
        "volume":   0.10,
    }

    composite = (
        rsi_score  * weights["rsi"] +
        mom_score  * weights["momentum"] +
        vwap_score * weights["vwap"] +
        boll_score * weights["bollinger"] +
        vol_score  * weights["volume"]
    )

    # ── Trend penalty ──────────────────────────────────────────────────────────
    # If composite says UP but trend is strongly DOWN, dampen the signal
    # This prevents the oversold-RSI-in-downtrend trap
    if trend_direction == "DOWN" and composite > 0:
        composite *= (1 - trend_strength * 0.6)
    elif trend_direction == "UP" and composite < 0:
        composite *= (1 - trend_strength * 0.6)

    current_price = df["close"].iloc[-1]

    # Count how many signals agree with composite direction
    direction = "UP" if composite > 0 else "DOWN"
    scores = [rsi_score, mom_score, vwap_score, boll_score, vol_score]
    if direction == "UP":
        corroborating = sum(1 for s in scores if s > 0)
    else:
        corroborating = sum(1 for s in scores if s < 0)

    return {
        "symbol":           symbol,
        "current_price":    current_price,
        "composite":        round(composite, 3),
        "direction":        direction,
        "strength":         abs(composite),
        "trend_direction":  trend_direction,
        "trend_strength":   round(trend_strength, 2),
        "corroborating":    corroborating,
        "signals": {
            "rsi":       {"score": rsi_score,  "value": rsi_val},
            "momentum":  {"score": mom_score,  "value": mom_val},
            "vwap":      {"score": vwap_score, "value": vwap_val},
            "bollinger": {"score": boll_score, "value": boll_val},
            "volume":    {"score": vol_score,  "value": vol_val},
        }
    }


# =============================================================================
# POLYMARKET 5-MIN MARKET FINDER  (deterministic slug approach)
# =============================================================================

# 5-min windows follow fixed Unix timestamps divisible by 300
# Slug format: btc-updown-5m-{window_start_ts}
#              eth-updown-5m-{window_start_ts}
COIN_SLUGS_5M = {
    "BTC": "btc-updown-5m",
    "ETH": "eth-updown-5m",
    "SOL": "sol-updown-5m",
    "XRP": "xrp-updown-5m",
}

COIN_SLUGS_15M = {
    "BTC": "btc-updown-15m",
    "ETH": "eth-updown-15m",
    "SOL": "sol-updown-15m",
    "XRP": "xrp-updown-15m",
}

# Keep backward compat
COIN_SLUGS = COIN_SLUGS_5M

def get_current_window_ts(interval: int = 300) -> int:
    """Return the Unix timestamp of the current window start for a given interval."""
    now = int(time.time())
    return now - (now % interval)


def get_seconds_until_close(interval: int = 300) -> int:
    """Seconds remaining in the current window for a given interval."""
    now = int(time.time())
    window_start = now - (now % interval)
    return interval - (now - window_start)


def fetch_market_by_slug(coin: str, slug_dict: dict, interval: int) -> Optional[dict]:
    """
    Generic market fetcher using deterministic slug approach.
    Works for both 5-min and 15-min markets.
    """
    window_ts   = get_current_window_ts(interval)
    seconds_left = get_seconds_until_close(interval)

    # If less than 15 seconds left, target the NEXT window instead
    if seconds_left < 15:
        window_ts += interval
        log.info(f"  Targeting next {interval//60}m window (only {seconds_left}s left)")

    slug_base = slug_dict.get(coin)
    if not slug_base:
        return None

    slug = f"{slug_base}-{window_ts}"

    try:
        resp = requests.get(
            f"{GAMMA_API_URL}/markets",
            params={"slug": slug},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, list) and len(data) > 0:
            m = data[0]
        elif isinstance(data, dict):
            m = data
        else:
            return None

        if not m.get("active") or m.get("closed"):
            return None

        yes_price, no_price = get_prices(m)
        m["_coin"]        = coin
        m["_yes_price"]   = yes_price
        m["_no_price"]    = no_price
        m["_window_ts"]   = window_ts
        m["_seconds_left"] = get_seconds_until_close(interval)
        m["_interval"]    = interval
        return m

    except Exception as e:
        log.debug(f"Could not fetch {interval//60}m market for {coin}: {e}")
        return None


def fetch_5min_market(coin: str) -> Optional[dict]:
    return fetch_market_by_slug(coin, COIN_SLUGS_5M, 300)


def fetch_15min_market(coin: str) -> Optional[dict]:
    return fetch_market_by_slug(coin, COIN_SLUGS_15M, 900)


def fetch_all_5min_markets() -> list[dict]:
    """Fetch active 5-min markets for all tracked coins."""
    markets = []
    for coin in COIN_SLUGS_5M:
        m = fetch_5min_market(coin)
        if m:
            markets.append(m)
            log.info(f"  Found 5-min market: {m.get('question', '')[:60]} | "
                     f"Yes={m['_yes_price']:.0%} | {m['_seconds_left']}s left")
    return markets


def fetch_all_15min_markets() -> list[dict]:
    """Fetch active 15-min markets for all tracked coins."""
    markets = []
    for coin in COIN_SLUGS_15M:
        m = fetch_15min_market(coin)
        if m:
            markets.append(m)
            log.info(f"  Found 15-min market: {m.get('question', '')[:60]} | "
                     f"Yes={m['_yes_price']:.0%} | {m['_seconds_left']}s left")
    return markets


def get_prices(market: dict) -> tuple[float, float]:
    """Parse yes/no prices from market object."""
    try:
        best_ask = market.get("bestAsk")
        if best_ask and float(best_ask) > 0:
            yes = float(best_ask)
            return yes, round(1 - yes, 4)

        last = market.get("lastTradePrice")
        if last and float(last) > 0:
            yes = float(last)
            return yes, round(1 - yes, 4)

        raw = market.get("outcomePrices", "[]")
        if isinstance(raw, str):
            import json as _json
            prices = _json.loads(raw)
        else:
            prices = raw
        if prices and len(prices) >= 2:
            return float(prices[0]), float(prices[1])
    except Exception:
        pass
    return 0.5, 0.5


# =============================================================================
# MATCH SIGNAL TO MARKET
# =============================================================================

def match_and_decide(signal: dict, market_5m: dict, market_15m: Optional[dict] = None) -> Optional[dict]:
    """
    Given a technical signal, a 5-min market, and optionally a 15-min market,
    decide whether to bet.

    Multi-timeframe confirmation logic:
    - If 15-min market available: only bet when 5-min AND 15-min crowd agree
      with our technical signal. Three-way agreement = highest confidence.
    - If 15-min market unavailable: fall back to 5-min only (existing behavior).

    The crowd agreement check:
      5-min YES=58% + 15-min YES=62% + our signal UP → strong three-way agreement
      5-min YES=58% + 15-min YES=42% + our signal UP → crowd split, skip
    """
    market = market_5m  # primary market for bet placement
    composite    = signal["composite"]
    strength     = signal["strength"]
    direction    = signal["direction"]
    yes_price    = market["_yes_price"]
    no_price     = market["_no_price"]
    seconds_left = market.get("_seconds_left", 300)

    # ── Timing gate ───────────────────────────────────────────────────────────
    if seconds_left > 240 or seconds_left < 30:
        log.debug(f"  Timing gate: {seconds_left}s left — outside bet window")
        return None

    # ── Crowd confidence gate ─────────────────────────────────────────────────
    # Skip when crowd is already too confident in one direction.
    # Data shows: crowd 65%+ → 17% win rate, crowd 50-55% → 66% win rate.
    # Edge comes from uncertainty — when crowd is already certain, edge is gone.
    crowd_conviction = max(yes_price, 1 - yes_price)  # normalize to 0.5-1.0
    if crowd_conviction > MAX_CROWD_PRICE:
        log.info(f"  Skip: crowd too confident ({crowd_conviction:.0%}) — edge already priced in (max={MAX_CROWD_PRICE:.0%})")
        return None

    # ── 5-min crowd direction ─────────────────────────────────────────────────
    market_up_prob  = yes_price
    market_leans_up   = market_up_prob > 0.52
    market_leans_down = market_up_prob < 0.48
    market_is_neutral = not market_leans_up and not market_leans_down

    signal_agrees = (direction == "UP" and market_leans_up) or \
                    (direction == "DOWN" and market_leans_down)

    # ── 5-min crowd agreement (now strict) ────────────────────────────────────
    # Data analysis showed: NO bets won 64% (255 trades), YES bets won 55% (1279 trades)
    # The crowd's lean is one of the strongest predictors. Require agreement.
    if REQUIRE_AGREES:
        if not signal_agrees and not market_is_neutral:
            log.info(f"  Skip: signal ({direction}) disagrees with 5m crowd "
                     f"(yes={yes_price:.0%})")
            return None

    # ── 15-min crowd confirmation ─────────────────────────────────────────────
    # Three-way agreement (signal + 5m crowd + 15m crowd) had 65% win rate
    # in the analysis vs 53% for disagreement. Enforce strictly when configured.
    crowd_15m_agrees = None  # None = no 15m market available
    crowd_15m_leans  = "NEUTRAL"

    if market_15m:
        yes_15m = market_15m["_yes_price"]
        if yes_15m > 0.52:
            crowd_15m_leans = "UP"
        elif yes_15m < 0.48:
            crowd_15m_leans = "DOWN"
        else:
            crowd_15m_leans = "NEUTRAL"

        crowd_15m_agrees = (direction == "UP"   and crowd_15m_leans == "UP") or \
                           (direction == "DOWN"  and crowd_15m_leans == "DOWN")

        if not crowd_15m_agrees:
            if REQUIRE_3WAY_AGREE:
                # Strict mode: skip unless 15m crowd actively agrees (NEUTRAL not enough)
                log.info(f"  Skip: 15-min crowd ({crowd_15m_leans}) does not actively "
                         f"confirm signal ({direction}) — require 3-way agreement")
                return None
            else:
                log.info(f"  ⚠ 15-min crowd ({crowd_15m_leans}) does not confirm signal ({direction}) — proceeding anyway")

        log.info(f"  ✓ 3-way agreement: signal={direction} | "
                 f"5m={'UP' if market_leans_up else 'DOWN' if market_leans_down else 'NEUTRAL'} | "
                 f"15m={crowd_15m_leans} | "
                 f"5m_Yes={yes_price:.0%} | 15m_Yes={yes_15m:.0%}")
    else:
        # No 15-min market available
        if REQUIRE_3WAY_AGREE:
            log.info(f"  Skip: no 15m market available, REQUIRE_3WAY_AGREE=True")
            return None

    # ── RSI + Momentum must agree (new critical gate) ────────────────────────
    # If RSI says DOWN but momentum is positive (or vice versa), the signals
    # are contradicting each other — this is a trending market, not a reversal.
    # Claude has been correctly vetoing these. Block them earlier.
    rsi_score = signal["signals"]["rsi"]["score"]
    mom_score = signal["signals"]["momentum"]["score"]

    rsi_says_down = rsi_score < 0
    rsi_says_up   = rsi_score > 0
    mom_says_down = mom_score < 0
    mom_says_up   = mom_score > 0
    mom_neutral   = mom_score == 0.0

    if rsi_says_down and mom_says_up:
        log.info(f"  Skip: RSI overbought ({signal['signals']['rsi']['value']:.0f}) but momentum positive ({signal['signals']['momentum']['value']:+.3f}%) — trending, not reversing")
        return None
    if rsi_says_up and mom_says_down:
        log.info(f"  Skip: RSI oversold ({signal['signals']['rsi']['value']:.0f}) but momentum negative ({signal['signals']['momentum']['value']:+.3f}%) — trending, not bouncing")
        return None

    # Only bet if signal is strong enough
    if strength < MIN_COMPOSITE:
        log.info(f"  Skip: composite {composite:+.3f} too weak (need ±{MIN_COMPOSITE})")
        return None

    # Require signal agrees with market (84% disagreement was killing us)
    if REQUIRE_AGREES and not signal_agrees and not market_is_neutral:
        log.info(f"  Skip: signal disagrees with market")
        return None

    # Require at least MIN_CORROBORATING signals pointing same direction
    corroborating = signal.get("corroborating", 0)
    if corroborating < MIN_CORROBORATING:
        log.info(f"  Skip: only {corroborating}/5 corroborating signals (need {MIN_CORROBORATING})")
        return None

    # Block bets against the hourly trend only when trend is very strong
    trend_dir = signal.get("trend_direction", "NEUTRAL")
    trend_str = signal.get("trend_strength", 0.0)
    if trend_dir != "NEUTRAL" and trend_dir != direction and trend_str > 0.75:
        log.info(f"  Skip: hourly trend {trend_dir} ({trend_str:.0%}) opposes signal {direction}")
        return None

    # Determine bet side
    if direction == "UP":
        bet_side  = "YES"
        bet_price = yes_price
    else:
        bet_side  = "NO"
        bet_price = no_price

    # Payout check
    roi = (1 - bet_price) / bet_price
    if roi < MIN_PAYOUT_ROI:
        return None

    # True probability — boost slightly when 15m crowd also agrees
    true_prob = 0.50 + (strength * 0.35)
    if crowd_15m_agrees:
        true_prob += 0.04  # small boost for 3-way confirmation
    true_prob = min(true_prob, 0.85)

    # Adjust for time remaining
    time_factor = 1 - (seconds_left / 300)
    true_prob = true_prob + (time_factor * 0.08)
    true_prob = min(true_prob, 0.88)

    edge = true_prob - bet_price
    if edge < MIN_MARKET_EDGE:
        return None

    bet_size = min(MAX_BET_USD, max(MIN_BET_USD, strength * MAX_BET_USD))
    # Slightly larger bet when 3-way confirmation
    if crowd_15m_agrees:
        bet_size = min(MAX_BET_USD, bet_size * 1.2)
    bet_size = round(bet_size, 2)
    if LIVE_MODE:
        bet_size = LIVE_BET_USD  # Phase-3 live validation: flat $1 bets

    # CLOB token ids for live execution. Gamma's clobTokenIds is a JSON string
    # of [YES_token, NO_token] (same order as outcomePrices). The executor
    # always BUYs the token of the outcome being bet.
    yes_token = no_token = None
    try:
        raw_tok = market.get("clobTokenIds")
        if raw_tok:
            tok = json.loads(raw_tok) if isinstance(raw_tok, str) else raw_tok
            if isinstance(tok, list) and len(tok) >= 2:
                yes_token, no_token = str(tok[0]), str(tok[1])
    except Exception:
        pass

    return {
        "market_id":           market.get("id", ""),
        "token_id":            yes_token if bet_side == "YES" else no_token,
        "question":            market.get("question", ""),
        "coin":                market["_coin"],
        "bet_side":            bet_side,
        "bet_price":           bet_price,
        "bet_size":            bet_size,
        "true_prob":           round(true_prob, 3),
        "edge":                round(edge, 3),
        "roi":                 round(roi, 3),
        "composite":           composite,
        "strength":            strength,
        "signal_direction":    direction,
        "seconds_left":        seconds_left,
        "signal_agrees":       signal_agrees,
        "crowd_15m_agrees":    crowd_15m_agrees,
        "crowd_15m_leans":     crowd_15m_leans,
        "corroborating":       signal.get("corroborating", 0),
        "trend_direction":     signal.get("trend_direction", "NEUTRAL"),
        "trend_strength":      signal.get("trend_strength", 0.0),
        "signals":             signal["signals"],
        "current_price":       signal["current_price"],
        "_window_ts":          market.get("_window_ts", 0),
    }


# =============================================================================
# CLAUDE VETO LAYER
# =============================================================================

VETO_PROMPT = """You are a crypto trading risk manager reviewing a proposed bet.
The bot has already passed this through multiple filters — composite threshold,
RSI-momentum gate, timing gate, and crowd confirmation. Your job is to catch
genuinely dangerous bets that slipped through, NOT to second-guess valid signals.

PROPOSED BET:
  Market: {question}
  Bet side: {bet_side} (YES means we predict price going UP, NO means we predict DOWN)
  Market price: {bet_price:.0%}
  Coin: {coin} @ ${current_price:,.2f}
  Seconds left in window: {seconds_left}
  Signal direction: {signal_direction}
  Signal agrees with 5-min market: {signal_agrees}
  15-min crowd agrees: {crowd_15m_agrees} (leans {crowd_15m_leans})
  Hourly trend: {trend_direction} (strength: {trend_strength:.0%})
  Corroborating signals: {corroborating}/5

SIGNALS (composite: {composite:+.3f}):
  RSI={rsi_val}({rsi_score:+.1f})  Mom={mom_val:+.3f}%({mom_score:+.1f})
  VWAP={vwap_val:+.3f}%({vwap_score:+.1f})  Boll={boll_val:.2f}({boll_score:+.1f})  Vol={vol_val:.1f}x({vol_score:+.1f})

VETO criteria — only veto if ONE OR MORE clearly applies:

A. RSI and momentum strongly contradict each other AND composite magnitude is below 0.12.
   This is a noisy signal in a sideways market.

B. Hourly trend strongly opposes the bet direction (trend strength above 75%).
   Don't bet against a strong macro trend.

C. The signal direction does not match the bet side. UP signals should bet YES,
   DOWN signals should bet NO. If signal_direction is UP and bet_side is NO
   (or signal_direction is DOWN and bet_side is YES), that is misaligned.
   NOTE: composite negative + bet_side NO is CORRECT (negative composite means DOWN
   signal, NO bet predicts DOWN — they align).

D. Less than 30 seconds left in window — payout is too poor at this point.

DEFAULT TO APPROVE. Only veto if one of A/B/C/D clearly applies.
If you are unsure, approve. The bot has already done rigorous filtering.

Respond ONLY with JSON:
{{"approve": true, "reason": "one sentence"}}
or
{{"approve": false, "reason": "one sentence naming which criterion (A/B/C/D) was violated"}}
"""


def claude_veto(decision: dict, claude_client: anthropic.Anthropic) -> tuple[bool, str]:
    """
    Ask Claude to approve or veto a bet decision.
    Returns (approved: bool, reason: str).
    Falls back to approve=True if Claude call fails (does not block on API errors).
    """
    if not ANTHROPIC_API_KEY:
        return True, "No API key — skipping veto"

    sig = decision["signals"]
    prompt = VETO_PROMPT.format(
        question         = decision["question"],
        bet_side         = decision["bet_side"],
        bet_price        = decision["bet_price"],
        coin             = decision["coin"],
        current_price    = decision["current_price"],
        composite        = decision["composite"],
        signal_direction = decision["signal_direction"],
        seconds_left     = decision.get("seconds_left", "?"),
        signal_agrees    = decision.get("signal_agrees", "?"),
        crowd_15m_agrees = decision.get("crowd_15m_agrees", "N/A"),
        crowd_15m_leans  = decision.get("crowd_15m_leans", "N/A"),
        corroborating    = decision.get("corroborating", 0),
        trend_direction  = decision.get("trend_direction", "NEUTRAL"),
        trend_strength   = decision.get("trend_strength", 0.0),
        rsi_val          = sig["rsi"]["value"],
        rsi_score        = sig["rsi"]["score"],
        mom_val          = sig["momentum"]["value"],
        mom_score        = sig["momentum"]["score"],
        vwap_val         = sig["vwap"]["value"],
        vwap_score       = sig["vwap"]["score"],
        boll_val         = sig["bollinger"]["value"],
        boll_score       = sig["bollinger"]["score"],
        vol_val          = sig["volume"]["value"],
        vol_score        = sig["volume"]["score"],
    )

    try:
        response = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        return result.get("approve", True), result.get("reason", "")
    except Exception as e:
        log.warning(f"Claude veto call failed: {e} — defaulting to approve")
        return True, "veto unavailable"


# =============================================================================
# PAPER TRADING
# =============================================================================

def paper_bet(decision: dict):
    """Record a paper trade — MARKET ORDER (taker), fills at ask, pays fee."""
    market_id = decision["market_id"]
    if market_id in portfolio["positions"]:
        return  # already in this position

    # ── Realistic market-order cost model ──────────────────────────────────
    # A taker market order fills at the ASK (slightly worse than the displayed
    # mid/bid), and pays Polymarket's crypto taker fee. We approximate the ask
    # as the bet_price plus half the typical 1c spread.
    SPREAD = 0.01
    ask_price = min(0.99, decision["bet_price"] + SPREAD / 2)

    cost = fees.market_order_cost(decision["bet_size"], ask_price, "crypto")

    portfolio["cash"] -= decision["bet_size"]
    portfolio["positions"][market_id] = {
        **decision,
        "fill_price":   cost["fill_price"],     # what we actually paid
        "net_shares":   cost["net_shares"],     # shares after fee
        "fee_usd":      cost["fee_usd"],         # taker fee paid
        "opened_at":    datetime.now().isoformat(),
        "resolved":     False,
        "_window_ts":   decision.get("_window_ts", 0),
    }
    portfolio["total_bets"] += 1

    # Save to trades file (include cost fields)
    trades = []
    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE) as f:
                trades = json.load(f)
        except Exception:
            pass
    trades.append({
        **decision,
        "fill_price": cost["fill_price"],
        "net_shares": cost["net_shares"],
        "fee_usd":    cost["fee_usd"],
        "opened_at":  datetime.now().isoformat(),
    })
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)

    log.info(
        f"[MARKET] {decision['bet_side']} on '{decision['question'][:50]}' "
        f"| ${decision['bet_size']:.2f} @ ask {cost['fill_price']:.0%} "
        f"| fee ${cost['fee_usd']:.4f} | composite={decision['composite']:+.2f}"
    )
    save_portfolio()


def live_bet(decision: dict):
    """
    LIVE order via PolymarketExecutor (all safety rails inside place_order).
    On a real fill, mirrors the SAME bookkeeping as paper_bet, but with the
    ACTUAL fill price / shares / fee from the exchange response.
    The executor itself sends the fill/error Telegram alerts.
    """
    market_id = decision["market_id"]
    if market_id in portfolio["positions"]:
        return False
    if EXECUTOR is None:
        log.error("live_bet called but executor not initialized")
        return False
    if not decision.get("token_id"):
        log.error(f"live_bet: missing token_id for '{decision['question'][:40]}' — skipped")
        return False

    res = EXECUTOR.place_order(decision)
    if res.get("status") != "filled":
        log.warning(f"[LIVE] order not filled: {res.get('error')}")
        return False

    portfolio["cash"] -= decision["bet_size"]
    portfolio["positions"][market_id] = {
        **decision,
        "fill_price":   res["fill_price"],     # actual exchange fill
        "net_shares":   res["fill_shares"],
        "fee_usd":      res["fee_paid"],
        "order_id":     res.get("order_id"),
        "tx_hash":      res.get("tx_hash"),
        "opened_at":    datetime.now().isoformat(),
        "resolved":     False,
        "_window_ts":   decision.get("_window_ts", 0),
    }
    portfolio["total_bets"] += 1

    trades = []
    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE) as f:
                trades = json.load(f)
        except Exception:
            pass
    trades.append({
        **decision,
        "fill_price": res["fill_price"],
        "net_shares": res["fill_shares"],
        "fee_usd":    res["fee_paid"],
        "order_id":   res.get("order_id"),
        "tx_hash":    res.get("tx_hash"),
        "opened_at":  datetime.now().isoformat(),
    })
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)

    log.info(
        f"[LIVE] {decision['bet_side']} on '{decision['question'][:50]}' "
        f"| ${decision['bet_size']:.2f} @ {res['fill_price']:.0%} "
        f"| fee ${res['fee_paid']:.4f} | order {str(res.get('order_id'))[:14]}…"
    )
    save_portfolio()
    return True


def resolve_positions():
    """
    Check open positions and resolve any whose window has closed.
    Fetches current market state from Polymarket to determine win/loss.
    """
    if not portfolio["positions"]:
        return

    to_remove = []
    now_ts = int(time.time())

    for market_id, pos in portfolio["positions"].items():
        window_ts = pos.get("_window_ts", 0)

        try:
            wts = int(str(window_ts))
            window_close = wts + 300
        except (ValueError, TypeError):
            window_close = now_ts

        seconds_since_close = now_ts - window_close
        log.debug(f"  Checking {pos['question'][:40]} | window_ts={wts} | "
                  f"closes at {window_close} | {seconds_since_close}s since close")

        if now_ts < window_close + 30:
            continue

        # Fetch market to check resolution
        try:
            resp = requests.get(
                f"{GAMMA_API_URL}/markets/{market_id}",
                timeout=10,
            )
            if resp.status_code != 200:
                # Try by slug approach
                continue

            market_data = resp.json()
            if isinstance(market_data, list):
                market_data = market_data[0] if market_data else {}

            resolved = market_data.get("closed") or market_data.get("resolved")
            outcome_prices = market_data.get("outcomePrices", "[]")
            if isinstance(outcome_prices, str):
                outcome_prices = json.loads(outcome_prices)

            if not resolved:
                continue

            # Determine winner: outcomePrices will be ["1", "0"] or ["0", "1"]
            # Index 0 = YES (Up), Index 1 = NO (Down)
            yes_resolved = float(outcome_prices[0]) == 1.0 if outcome_prices else None

            if yes_resolved is None:
                continue

            bet_side  = pos["bet_side"]
            bet_size  = pos["bet_size"]
            # Use net shares after fee (set at bet time); fall back for old records
            net_shares = pos.get("net_shares", bet_size / pos["bet_price"])

            won = (bet_side == "YES" and yes_resolved) or (bet_side == "NO" and not yes_resolved)
            profit = fees.resolve_payout(net_shares, won, bet_size)

            if won:
                portfolio["cash"] += net_shares * 1.0
                resolved_trade = {**pos, "outcome": "WIN", "profit": round(profit, 4), "resolved_at": datetime.now().isoformat()}
                portfolio["resolved"].append(resolved_trade)
                log.info(f"WIN  | {bet_side} on '{pos['question'][:50]}' | +${profit:.2f} (fee ${pos.get('fee_usd',0):.4f})")
            else:
                resolved_trade = {**pos, "outcome": "LOSS", "profit": round(profit, 4), "resolved_at": datetime.now().isoformat()}
                portfolio["resolved"].append(resolved_trade)
                log.info(f"LOSS | {bet_side} on '{pos['question'][:50]}' | ${profit:.2f}")

            if LIVE_MODE and EXECUTOR is not None:
                # Feed realized P&L into the executor's daily-loss cap, and
                # read the REAL balance so we can observe whether winnings
                # auto-credit in V2 (the open redemption question).
                EXECUTOR.record_outcome(profit)
                real_bal = EXECUTOR.sync_balance()
                bal_txt = f"${real_bal:.2f}" if real_bal is not None else "unknown"
                telegram_alert(
                    f"{'✅ WIN' if won else '❌ LOSS'} {bet_side} {pos['question'][:40]}\n"
                    f"P&L {profit:+.2f} | day loss {EXECUTOR.daily_loss_pct():.0%} "
                    f"| real balance {bal_txt}"
                )

            # Write outcome back to trades file so supervisor_agent can read it
            try:
                all_trades = []
                if os.path.exists(TRADES_FILE):
                    with open(TRADES_FILE) as f:
                        all_trades = json.load(f)
                # Update the matching trade record with outcome
                for t in all_trades:
                    if t.get("market_id") == market_id and "outcome" not in t:
                        t["outcome"] = resolved_trade["outcome"]
                        t["profit"]  = resolved_trade["profit"]
                        t["resolved_at"] = resolved_trade["resolved_at"]
                        break
                with open(TRADES_FILE, "w") as f:
                    json.dump(all_trades, f, indent=2)
            except Exception as e:
                log.debug(f"Could not update trades file with outcome: {e}")

            to_remove.append(market_id)

        except Exception as e:
            log.debug(f"Could not resolve position {market_id}: {e}")

    for mid in to_remove:
        portfolio["positions"].pop(mid, None)

    if to_remove:
        save_portfolio()


def print_signals(coin: str, signal: dict):
    """Pretty print current signals for a coin."""
    s = signal["signals"]
    trend = signal.get("trend_direction", "?")
    trend_str = signal.get("trend_strength", 0)
    corr = signal.get("corroborating", 0)
    print(f"\n  {coin} @ ${signal['current_price']:,.2f}  |  composite: {signal['composite']:+.3f}  ({signal['direction']})  |  trend: {trend} ({trend_str:.0%})  |  corroborating: {corr}/5")
    print(f"    RSI={s['rsi']['value']:.1f}({s['rsi']['score']:+.1f})  "
          f"Mom={s['momentum']['value']:+.3f}%({s['momentum']['score']:+.1f})  "
          f"VWAP={s['vwap']['value']:+.3f}%({s['vwap']['score']:+.1f})  "
          f"Boll={s['bollinger']['value']:.2f}({s['bollinger']['score']:+.1f})  "
          f"Vol={s['volume']['value']:.1f}x({s['volume']['score']:+.1f})")


def print_dashboard():
    """Print portfolio and open positions."""
    cash      = portfolio["cash"]
    positions = portfolio["positions"]
    resolved  = portfolio["resolved"]
    total     = cash + sum(p["bet_size"] for p in positions.values())
    pnl       = total - portfolio["daily_start"]

    wins   = [r for r in resolved if r["outcome"] == "WIN"]
    losses = [r for r in resolved if r["outcome"] == "LOSS"]
    win_rate = len(wins) / len(resolved) * 100 if resolved else 0
    realized_pnl = sum(r["profit"] for r in resolved)

    print("\n" + "="*65)
    print(f"  CRYPTO 5-MIN BOT  |  {datetime.now().strftime('%H:%M:%S')}  |  PAPER MODE")
    print("="*65)
    print(f"  Cash: ${cash:.2f}  |  Portfolio: ${total:.2f}  |  P&L: {pnl:+.2f}  |  Bets: {portfolio['total_bets']}")
    if resolved:
        print(f"  Resolved: {len(resolved)} bets | {len(wins)}W / {len(losses)}L | "
              f"Win rate: {win_rate:.0f}% | Realized P&L: ${realized_pnl:+.2f}")
    if positions:
        print("  OPEN POSITIONS:")
        for mid, pos in positions.items():
            wts = pos.get("_window_ts", "?")
            print(f"    {pos['bet_side']:3s} ${pos['bet_size']:.2f} @ {pos['bet_price']:.0%} | {pos['question'][:45]}")
    print("="*65)


# =============================================================================
# STATS VIEWER
# =============================================================================

def print_stats():
    """Analyze performance from trades file."""
    if not os.path.exists(TRADES_FILE):
        print("No trades recorded yet.")
        return

    with open(TRADES_FILE) as f:
        trades = json.load(f)

    if not trades:
        print("No trades yet.")
        return

    print(f"\n{'='*65}")
    print(f"  PERFORMANCE STATS  ({len(trades)} total bets)")
    print(f"{'='*65}")

    # Signal contribution stats
    signal_names = ["rsi", "momentum", "vwap", "bollinger", "volume"]
    print("\n  SIGNAL DIRECTIONS TAKEN:")
    for sig in signal_names:
        bullish = sum(1 for t in trades if t.get("signals", {}).get(sig, {}).get("score", 0) > 0)
        bearish = sum(1 for t in trades if t.get("signals", {}).get(sig, {}).get("score", 0) < 0)
        neutral = len(trades) - bullish - bearish
        print(f"    {sig:10s}: bullish={bullish} bearish={bearish} neutral={neutral}")

    print(f"\n  BET DIRECTION: YES={sum(1 for t in trades if t['bet_side']=='YES')}  NO={sum(1 for t in trades if t['bet_side']=='NO')}")
    print(f"  AVG COMPOSITE: {sum(t['composite'] for t in trades)/len(trades):+.3f}")
    print(f"  AVG EDGE:      {sum(t['edge'] for t in trades)/len(trades):.1%}")
    print(f"  AVG BET SIZE:  ${sum(t['bet_size'] for t in trades)/len(trades):.2f}")
    print(f"\n  NOTE: Resolution tracking requires manual check of market outcomes.")
    print(f"  Check trades in {TRADES_FILE} and match against Polymarket results.")
    print(f"{'='*65}\n")


# =============================================================================
# CIRCUIT BREAKER
# =============================================================================

def check_circuit_breaker() -> bool:
    """
    Daily loss limit with 24h pause.
    If daily P&L drops below -DAILY_LOSS_LIMIT, halt for 24 hours from trigger time.
    """
    # Check if we're still in a 24h pause from a previous trigger
    cb_until = portfolio.get("circuit_break_until")
    if cb_until:
        try:
            until = datetime.fromisoformat(cb_until)
            if datetime.now() < until:
                remaining = (until - datetime.now()).total_seconds() / 3600
                log.info(f"CIRCUIT BREAKER active — {remaining:.1f}h remaining")
                return True
            else:
                # 24h elapsed — reset
                portfolio["circuit_break_until"] = None
                portfolio["circuit_broken"]     = False
                portfolio["daily_start"]        = portfolio["cash"] + sum(
                    p["bet_size"] for p in portfolio["positions"].values()
                )
                log.info("CIRCUIT BREAKER reset — resuming normal trading")
        except Exception:
            portfolio["circuit_break_until"] = None

    # Reset daily_start at midnight
    today = date.today()
    if portfolio.get("circuit_break_day") != today:
        portfolio["circuit_break_day"] = today
        # Update daily start if it's a new day (and not in pause)
        if not portfolio.get("circuit_break_until"):
            portfolio["daily_start"] = portfolio["cash"] + sum(
                p["bet_size"] for p in portfolio["positions"].values()
            )

    # Check current loss
    total   = portfolio["cash"] + sum(p["bet_size"] for p in portfolio["positions"].values())
    pnl_pct = (total - portfolio["daily_start"]) / portfolio["daily_start"]

    if pnl_pct <= -DAILY_LOSS_LIMIT:
        # Trigger 24h pause
        portfolio["circuit_broken"]      = True
        portfolio["circuit_break_until"] = (datetime.now() + timedelta(hours=24)).isoformat()
        log.warning(
            f"CIRCUIT BREAKER TRIGGERED: Down {pnl_pct:.1%} (limit: -{DAILY_LOSS_LIMIT:.0%}). "
            f"Halting trading for 24 hours."
        )
        save_portfolio()
        return True

    return False


# =============================================================================
# MAIN LOOP
# =============================================================================

def run_bot():
    global EXECUTOR
    mode = "LIVE 💸" if LIVE_MODE else "PAPER"
    log.info(f"Starting 5-min crypto prediction bot ({mode} mode)")

    # Restore portfolio state from previous session if available
    load_portfolio()

    if LIVE_MODE:
        from polymarket_executor import PolymarketExecutor
        EXECUTOR = PolymarketExecutor(
            bankroll_cap=LIVE_BANKROLL,
            telegram_fn=telegram_alert,
        )
        # Reconciliation is a HARD GATE: the executor refuses orders until it
        # has successfully fetched real positions from Polymarket once.
        EXECUTOR.reconcile_positions(portfolio["positions"])
        bal = EXECUTOR.sync_balance()
        if not EXECUTOR._reconciled:
            log.error("Reconciliation failed — live trading blocked. Fix and restart.")
            telegram_alert("⛔ Live bot start FAILED: reconciliation error")
            return
        bal_txt = f"${bal:.2f}" if bal is not None else "unknown"
        # Fresh live run (no prior live state): seed the mirror ledger with
        # the REAL balance so the dashboard reflects reality, and write the
        # portfolio file immediately so the dashboard renders from scan #1.
        if bal is not None and portfolio["total_bets"] == 0 and not portfolio["positions"]:
            portfolio["cash"] = round(bal, 2)
            portfolio["daily_start"] = round(bal, 2)
        save_portfolio()
        log.info(f"[LIVE] executor ready | bankroll cap ${LIVE_BANKROLL:.2f} "
                 f"| flat bet ${LIVE_BET_USD:.2f} | real balance {bal_txt}")
        telegram_alert(f"🤖 LIVE bot started\n"
                       f"Bankroll cap ${LIVE_BANKROLL:.2f} | flat ${LIVE_BET_USD:.2f} bets\n"
                       f"Real balance {bal_txt}")

    if not ANTHROPIC_API_KEY:
        log.warning("ANTHROPIC_API_KEY not set — Claude veto layer disabled")
    claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

    scan_count = 0

    while True:
        try:
            scan_count += 1
            log.info(f"── Scan #{scan_count} ──────────────────────────────")

            # Resolve any expired positions first
            resolve_positions()

            if LIVE_MODE:
                _maybe_daily_summary()

            if check_circuit_breaker():
                log.info("Circuit breaker active. Sleeping 1hr...")
                time.sleep(3600)
                continue

            if len(portfolio["positions"]) >= MAX_POSITIONS:
                log.info("Max positions reached. Monitoring only.")
                print_dashboard()
                time.sleep(SCAN_INTERVAL)
                continue

            # Step 1: Compute signals for each coin
            signals = {}
            for coin, symbol in COINS.items():
                signal = compute_composite(symbol)
                if signal:
                    signals[coin] = signal
                    print_signals(coin, signal)

            if not signals:
                log.warning("No signals computed. Retrying...")
                time.sleep(SCAN_INTERVAL)
                continue

            # Step 2: Find active 5-min AND 15-min markets
            seconds_left = get_seconds_until_close(300)
            log.info(f"Current 5m window: {seconds_left}s remaining")
            markets_5m  = fetch_all_5min_markets()
            markets_15m = fetch_all_15min_markets()

            # Build lookup dict for 15-min markets by coin
            markets_15m_by_coin = {m["_coin"]: m for m in markets_15m}

            # ── Structured market snapshot log (read by supervisor) ────────────
            # Format: MARKET_SNAPSHOT | coin | 5m_yes | 5m_leans | 15m_yes | 15m_leans | signal_dir | composite
            for coin in list(COINS.keys()):
                m5  = next((m for m in markets_5m  if m["_coin"] == coin), None)
                m15 = markets_15m_by_coin.get(coin)
                sig = signals.get(coin)

                yes_5m    = m5["_yes_price"]  if m5  else 0.5
                yes_15m   = m15["_yes_price"] if m15 else 0.5
                leans_5m  = "UP" if yes_5m  > 0.52 else "DOWN" if yes_5m  < 0.48 else "NEUTRAL"
                leans_15m = "UP" if yes_15m > 0.52 else "DOWN" if yes_15m < 0.48 else "NEUTRAL"
                sig_dir   = sig["direction"]  if sig else "NONE"
                composite = sig["composite"]  if sig else 0.0

                log.info(
                    f"MARKET_SNAPSHOT | {coin} | "
                    f"5m_yes={yes_5m:.2f} 5m={leans_5m} | "
                    f"15m_yes={yes_15m:.2f} 15m={leans_15m} | "
                    f"signal={sig_dir} composite={composite:+.3f}"
                )

            if not markets_5m:
                log.info("No 5-min markets found. Will retry next scan.")
                print_dashboard()
                time.sleep(SCAN_INTERVAL)
                continue

            # Step 3: Match signals → multi-timeframe confirmation → Claude veto → bet
            bets_this_scan = 0
            for market in markets_5m:
                coin = market["_coin"]
                if coin not in signals:
                    continue

                if market.get("id", "") in portfolio["positions"]:
                    continue

                signal   = signals[coin]
                m15      = markets_15m_by_coin.get(coin)  # may be None
                decision = match_and_decide(signal, market, m15)

                if not decision:
                    continue

                # Claude veto layer
                if claude_client:
                    approved, reason = claude_veto(decision, claude_client)
                    decision["claude_approved"] = approved
                    decision["claude_reason"] = reason

                    if not approved:
                        log.info(f"  VETOED by Claude: {reason}")
                        continue
                    else:
                        log.info(f"  APPROVED by Claude: {reason}")
                else:
                    decision["claude_approved"] = None
                    decision["claude_reason"] = "veto disabled"

                if LIVE_MODE:
                    if live_bet(decision):
                        bets_this_scan += 1
                else:
                    paper_bet(decision)
                    bets_this_scan += 1

            print_dashboard()
            log.info(f"Scan done. {bets_this_scan} bets. Sleeping {SCAN_INTERVAL}s...")
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log.info("Stopped.")
            print_dashboard()
            print_stats()
            break
        except Exception as e:
            log.error(f"Error: {e}", exc_info=True)
            time.sleep(30)


# =============================================================================
# ENTRY
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="5-min Crypto Prediction Bot")
    parser.add_argument("--paper", action="store_true", help="Run paper trading (default)")
    parser.add_argument("--live", action="store_true",
                        help="REAL MONEY: route bets through PolymarketExecutor")
    parser.add_argument("--bankroll", type=float, default=20.0,
                        help="Live bankroll cap in USD (drives the executor's sizing rails)")
    parser.add_argument("--stats", action="store_true", help="Print performance stats and exit")
    parser.add_argument("--signals", action="store_true", help="Print current signals and exit")
    args = parser.parse_args()

    if args.live:
        LIVE_MODE     = True
        LIVE_BANKROLL = args.bankroll
        # Separate state + log files so the paper instance runs alongside
        TRADES_FILE    = "live_trades.json"
        PORTFOLIO_FILE = "live_portfolio.json"
        STATS_FILE     = "live_stats.json"
        _root = logging.getLogger()
        for _h in list(_root.handlers):
            if isinstance(_h, logging.FileHandler):
                _root.removeHandler(_h)
        _root.addHandler(logging.FileHandler("live_bot.log"))
        print("=" * 62)
        print("⚠️  LIVE MODE — REAL MONEY ORDERS WILL BE PLACED")
        print(f"   Bankroll cap ${args.bankroll:.2f} | flat ${LIVE_BET_USD:.2f} bets "
              f"| executor max bet ${args.bankroll*0.05:.2f} (5%)")
        print(f"   Emergency stop anytime:  python3 kill_switch.py on")
        print("=" * 62)
        confirm = input("Type LIVE to confirm: ").strip()
        if confirm != "LIVE":
            print("Aborted.")
            sys.exit(0)

    if args.stats:
        print_stats()
    elif args.signals:
        print("\nCurrent signals:")
        for coin, symbol in COINS.items():
            s = compute_composite(symbol)
            if s:
                print_signals(coin, s)
    else:
        run_bot()
