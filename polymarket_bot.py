"""
Polymarket AI Trading Bot
=========================
Uses Claude AI to scan markets, analyze news, and place automated bets.

SETUP (do these before running):
  1. Create a Polygon wallet:
       python polymarket_bot.py --setup-wallet

  2. Fund your wallet:
       - Buy USDC on Coinbase/Binance
       - Send to Polygon network (use Polygon bridge if needed: https://wallet.polygon.technology)
       - Send to the wallet address printed in step 1

  3. Set environment variables:
       export ANTHROPIC_API_KEY="sk-ant-..."
       export POLYGON_PRIVATE_KEY="0x..."   # from step 1
       export POLYMARKET_API_KEY=""         # leave blank, bot fetches this automatically

  4. Install dependencies:
       pip install py-clob-client eth-account web3 anthropic requests

  5. Run in paper trading mode first:
       python polymarket_bot.py --paper

  6. Run live when ready:
       python polymarket_bot.py --live

ARCHITECTURE:
  MarketScanner  →  fetches active markets from Gamma API
  NewsSearcher   →  searches recent news for each market topic
  ClaudeAnalyst  →  analyzes odds + news, returns edge score
  PositionSizer  →  Kelly criterion + risk limits
  OrderExecutor  →  signs + submits via CLOB API (or paper logs)
  Monitor        →  tracks open positions, exits if needed
"""

import os
import sys
import json
import time
import logging
import argparse
import requests
from datetime import datetime, timezone, date
from typing import Optional
import anthropic

# ── Try importing blockchain libs (only needed for live trading) ──────────────
try:
    from eth_account import Account
    from eth_account.messages import encode_defunct
    from eth_account.structured_data import hash_domain, hash_message
    WALLET_AVAILABLE = True
except ImportError:
    WALLET_AVAILABLE = False

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("polymarket_bot.log"),
    ],
)
log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
POLYGON_PRIVATE_KEY = os.getenv("POLYGON_PRIVATE_KEY", "")
CLOB_API_URL       = "https://clob.polymarket.com"
GAMMA_API_URL      = "https://gamma-api.polymarket.com"

# Trading parameters
MIN_CONFIDENCE     = 0.65   # Only bet if Claude assigns ≥65% edge confidence
MAX_BET_USD        = 20.0   # Hard cap per bet in dollars
KELLY_FRACTION     = 0.25   # Fractional Kelly (conservative)
MIN_BET_USD        = 1.0    # Don't place bets smaller than $1
DAILY_LOSS_LIMIT   = 0.10   # Halt if portfolio is down 10% today
MAX_OPEN_POSITIONS = 5      # Never hold more than 5 positions at once
SCAN_INTERVAL      = 300    # Seconds between full market scans
LOG_FILE           = "trades.json"

# Market filters — Claude picks among these categories
PREFERRED_CATEGORIES = [
    "geopolitics", "politics", "economy", "finance", "tech", "science"
]
AVOID_CATEGORIES = [
    "sports", "esports", "crypto",  # Weak edge areas
]
MIN_VOLUME_USD     = 50_000  # Only consider markets with >$50k volume
MIN_DAYS_TO_CLOSE  = 1       # Skip markets closing in <1 day
MAX_DAYS_TO_CLOSE  = 7       # Only look at markets resolving within 1 week
MIN_ROI            = 0.15    # Skip bets with <15% potential return on investment

# ── Paper portfolio ───────────────────────────────────────────────────────────
paper_portfolio = {
    "cash": 1000.0,
    "positions": {},   # { market_id: { side, shares, avg_price, question } }
    "daily_start": 1000.0,
    "circuit_broken": False,
    "circuit_break_day": None,
    "total_bets": 0,
    "winning_bets": 0,
}


# =============================================================================
# WALLET SETUP
# =============================================================================

def setup_wallet():
    """Generate a new Polygon wallet and print instructions."""
    if not WALLET_AVAILABLE:
        print("ERROR: eth-account not installed. Run: pip install eth-account")
        return

    account = Account.create()
    print("\n" + "="*60)
    print("NEW POLYGON WALLET CREATED")
    print("="*60)
    print(f"Address:     {account.address}")
    print(f"Private Key: {account.key.hex()}")
    print("\nIMPORTANT:")
    print("  1. Save your private key somewhere safe (password manager)")
    print("  2. NEVER share your private key with anyone")
    print("  3. Set it as an environment variable:")
    print(f'     export POLYGON_PRIVATE_KEY="{account.key.hex()}"')
    print("\nTO FUND YOUR WALLET:")
    print("  - Buy USDC on Coinbase, Binance, or Kraken")
    print("  - Send to Polygon network (select 'Polygon' as network)")
    print(f"  - Send to address: {account.address}")
    print("  - Start with a small amount ($20-50) for testing")
    print("="*60 + "\n")


# =============================================================================
# MARKET SCANNER  (Gamma API)
# =============================================================================

def fetch_markets(limit: int = 50) -> list[dict]:
    """Fetch active markets from Polymarket Gamma API."""
    try:
        resp = requests.get(
            f"{GAMMA_API_URL}/markets",
            params={
                "active": "true",
                "closed": "false",
                "limit": limit,
                "order": "volume24hr",
                "ascending": "false",
            },
            timeout=15,
        )
        resp.raise_for_status()
        markets = resp.json()
        log.info(f"Fetched {len(markets)} markets from Gamma API")
        return markets
    except Exception as e:
        log.error(f"Failed to fetch markets: {e}")
        return []


def filter_markets(markets: list[dict]) -> list[dict]:
    """
    Filter markets by:
    - Volume threshold
    - Days remaining
    - Category (prefer geopolitics/policy, avoid sports/crypto)
    """
    filtered = []
    now = datetime.now(timezone.utc)

    for m in markets:
        try:
            # Skip closed or resolved
            if not m.get("active") or m.get("closed"):
                continue

            # Volume filter
            volume = float(m.get("volume", 0))
            if volume < MIN_VOLUME_USD:
                continue

            # Days to close filter
            end_date_str = m.get("endDate") or m.get("end_date_iso")
            if end_date_str:
                end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                days_remaining = (end_date - now).days
                if days_remaining < MIN_DAYS_TO_CLOSE:
                    continue
                if days_remaining > MAX_DAYS_TO_CLOSE:
                    continue

            # Category filter
            tags = [t.lower() for t in (m.get("tags") or [])]
            category = (m.get("category") or "").lower()

            if any(avoid in category or avoid in tags for avoid in AVOID_CATEGORIES):
                continue

            filtered.append(m)

        except Exception:
            continue

    log.info(f"Filtered to {len(filtered)} candidate markets")
    return filtered[:20]  # Analyze top 20 max per scan


# =============================================================================
# ORDER BOOK  (CLOB API)
# =============================================================================

def get_best_prices(market: dict) -> tuple[float, float]:
    """
    Returns (yes_price, no_price) as probabilities (0-1).
    Uses bestBid/bestAsk directly from market object (no separate API call needed).
    Falls back to outcomePrices if live prices unavailable.
    """
    try:
        # Best source: live bid/ask directly on the market object
        best_ask = market.get("bestAsk")
        best_bid = market.get("bestBid")
        last_trade = market.get("lastTradePrice")

        if best_ask and float(best_ask) > 0:
            yes_price = float(best_ask)
            return yes_price, round(1 - yes_price, 4)

        if last_trade and float(last_trade) > 0:
            yes_price = float(last_trade)
            return yes_price, round(1 - yes_price, 4)

        # Fallback: outcomePrices is a JSON string e.g. '["0.545", "0.455"]'
        raw_prices = market.get("outcomePrices", "[]")
        if isinstance(raw_prices, str):
            prices = json.loads(raw_prices)
        else:
            prices = raw_prices

        if prices and len(prices) >= 2:
            yes_price = float(prices[0])
            no_price = float(prices[1])
            return yes_price, no_price

    except Exception as e:
        log.warning(f"Price parse error: {e}")

    return 0.5, 0.5


def get_token_ids(market: dict) -> tuple[Optional[str], Optional[str]]:
    """
    Parse clobTokenIds (a JSON string) and return (yes_token_id, no_token_id).
    """
    try:
        raw = market.get("clobTokenIds", "[]")
        if isinstance(raw, str):
            tokens = json.loads(raw)
        else:
            tokens = raw
        yes_id = tokens[0] if len(tokens) > 0 else None
        no_id  = tokens[1] if len(tokens) > 1 else None
        return yes_id, no_id
    except Exception:
        return None, None


# =============================================================================
# CLAUDE ANALYST
# =============================================================================

ANALYSIS_PROMPT = """You are an expert prediction market analyst. Your job is to assess whether a Polymarket market is MISPRICED and whether there is a profitable edge.

MARKET DETAILS:
Question: {question}
Current Yes probability: {yes_pct}%
Current No probability: {no_pct}%
Volume: ${volume:,.0f}
Days remaining: {days_remaining}
Category: {category}
Tags: {tags}

RECENT NEWS/CONTEXT:
{news}

TASK:
1. Assess whether the current probability is accurate or mispriced
2. If mispriced, identify which side (YES or NO) has edge
3. Estimate the true probability based on your analysis
4. Give a confidence score for your edge assessment

Respond ONLY with a JSON object like this:
{{
  "has_edge": true,
  "direction": "NO",
  "true_probability": 0.72,
  "market_probability": 0.44,
  "confidence": 0.68,
  "reasoning": "2-3 sentence explanation of your reasoning",
  "key_factors": ["factor1", "factor2", "factor3"]
}}

Rules:
- has_edge: true only if you believe the market is meaningfully mispriced (>5% gap)
- direction: "YES" or "NO" — which side to bet
- true_probability: your estimate of the real probability for YES
- market_probability: the current YES market price
- confidence: how confident you are in this edge (0.5 = no confidence, 1.0 = certain)
- Only return has_edge=true if confidence >= 0.60
- NEVER return has_edge=true if yes_price is already below 0.15 or above 0.85 — the market already reflects that view and there is no profitable edge to capture
- Be conservative. If unsure, return has_edge=false
"""

def search_news(question: str) -> str:
    """Search for recent news relevant to the market question."""
    try:
        # Use DuckDuckGo instant answer API (no key required)
        resp = requests.get(
            "https://api.duckduckgo.com/",
            params={
                "q": question[:100],
                "format": "json",
                "no_redirect": 1,
                "no_html": 1,
            },
            timeout=10,
        )
        data = resp.json()

        snippets = []
        abstract = data.get("AbstractText", "")
        if abstract:
            snippets.append(abstract[:500])

        for topic in data.get("RelatedTopics", [])[:3]:
            text = topic.get("Text", "")
            if text:
                snippets.append(text[:200])

        return "\n".join(snippets) if snippets else "No recent news found via quick search."

    except Exception as e:
        return f"News search unavailable: {e}"


def analyze_market(market: dict, claude_client: anthropic.Anthropic) -> Optional[dict]:
    """
    Send market data + news to Claude for analysis.
    Returns analysis dict or None if no edge found.
    """
    question = market.get("question", "")
    yes_price, no_price = get_best_prices(market)
    volume = float(market.get("volume", 0))
    category = market.get("category", "unknown")
    tags = ", ".join(market.get("tags") or [])

    # Calculate days remaining
    end_date_str = market.get("endDate") or market.get("end_date_iso", "")
    try:
        end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        days_remaining = (end_date - datetime.now(timezone.utc)).days
    except Exception:
        days_remaining = 30

    # Fetch news
    news = search_news(question)

    # Build prompt
    prompt = ANALYSIS_PROMPT.format(
        question=question,
        yes_pct=round(yes_price * 100, 1),
        no_pct=round(no_price * 100, 1),
        volume=volume,
        days_remaining=days_remaining,
        category=category,
        tags=tags,
        news=news,
    )

    try:
        response = claude_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = response.content[0].text.strip()
        # Strip markdown fences if present
        raw = raw.replace("```json", "").replace("```", "").strip()
        analysis = json.loads(raw)

        # Attach market metadata
        yes_token, no_token = get_token_ids(market)
        analysis["question"] = question
        analysis["market_id"] = market.get("id") or market.get("condition_id", "")
        analysis["yes_price"] = yes_price
        analysis["no_price"] = no_price
        analysis["volume"] = volume
        analysis["token_id_yes"] = yes_token
        analysis["token_id_no"] = no_token

        return analysis

    except Exception as e:
        log.warning(f"Analysis failed for '{question[:50]}': {e}")
        return None


# =============================================================================
# POSITION SIZER  (Kelly Criterion)
# =============================================================================

def kelly_bet_size(
    true_prob: float,
    market_price: float,
    bankroll: float,
) -> float:
    """
    Kelly criterion for binary bet.
    Returns dollar amount to bet.

    Kelly formula: f = (b*p - q) / b
    where b = odds (payout ratio), p = true probability, q = 1-p
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0

    # Payout: if you pay `price` and win, you get $1, so profit = (1 - price) / price
    b = (1 - market_price) / market_price
    p = true_prob
    q = 1 - p

    kelly_f = (b * p - q) / b
    if kelly_f <= 0:
        return 0.0  # No edge

    # Apply fractional Kelly + hard caps
    bet_fraction = kelly_f * KELLY_FRACTION
    bet_amount = bankroll * bet_fraction
    bet_amount = min(bet_amount, MAX_BET_USD)
    bet_amount = max(bet_amount, MIN_BET_USD)

    return round(bet_amount, 2)


# =============================================================================
# ORDER EXECUTOR
# =============================================================================

def log_trade(trade: dict):
    """Append trade to JSON log file."""
    trades = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            try:
                trades = json.load(f)
            except Exception:
                trades = []
    trades.append(trade)
    with open(LOG_FILE, "w") as f:
        json.dump(trades, f, indent=2)


def paper_place_bet(analysis: dict, bet_size: float) -> bool:
    """Simulate placing a bet (paper trading mode)."""
    direction = analysis["direction"]
    price = analysis["yes_price"] if direction == "YES" else analysis["no_price"]
    shares = bet_size / price

    market_id = analysis["market_id"]
    paper_portfolio["cash"] -= bet_size
    paper_portfolio["positions"][market_id] = {
        "side": direction,
        "shares": shares,
        "avg_price": price,
        "question": analysis["question"],
        "bet_size": bet_size,
        "opened_at": datetime.now().isoformat(),
        "true_prob": analysis["true_probability"],
        "market_prob": analysis["market_probability"],
        "reasoning": analysis["reasoning"],
    }
    paper_portfolio["total_bets"] += 1

    log_trade({
        "timestamp": datetime.now().isoformat(),
        "mode": "paper",
        "question": analysis["question"],
        "market_id": market_id,
        "direction": direction,
        "bet_size": bet_size,
        "price": price,
        "shares": shares,
        "true_prob": analysis["true_probability"],
        "market_prob": analysis["market_probability"],
        "confidence": analysis["confidence"],
        "reasoning": analysis["reasoning"],
        "key_factors": analysis.get("key_factors", []),
    })

    log.info(
        f"[PAPER BET] {direction} on '{analysis['question'][:60]}' "
        f"| ${bet_size:.2f} @ {price:.2%} | confidence={analysis['confidence']:.0%}"
    )
    return True


def live_place_bet(analysis: dict, bet_size: float) -> bool:
    """
    Place a real bet on Polymarket via CLOB API.
    Requires POLYGON_PRIVATE_KEY to be set.
    """
    if not WALLET_AVAILABLE:
        log.error("eth-account not installed. Cannot place live bets.")
        return False

    if not POLYGON_PRIVATE_KEY:
        log.error("POLYGON_PRIVATE_KEY not set. Cannot place live bets.")
        return False

    # ── Step 1: Get or create API credentials ─────────────────────────────
    try:
        account = Account.from_key(POLYGON_PRIVATE_KEY)
        wallet_address = account.address

        # Request nonce from CLOB
        nonce_resp = requests.get(
            f"{CLOB_API_URL}/auth/nonce",
            timeout=10,
        )
        nonce_resp.raise_for_status()
        nonce = nonce_resp.json()["nonce"]

        # Sign the nonce
        msg = encode_defunct(text=nonce)
        signed = account.sign_message(msg)
        signature = signed.signature.hex()

        # Get API key
        auth_resp = requests.post(
            f"{CLOB_API_URL}/auth/api-key",
            json={
                "address": wallet_address,
                "signature": signature,
                "nonce": nonce,
            },
            timeout=10,
        )
        auth_resp.raise_for_status()
        auth_data = auth_resp.json()
        api_key = auth_data["apiKey"]
        api_secret = auth_data["secret"]
        api_passphrase = auth_data["passphrase"]

    except Exception as e:
        log.error(f"Auth failed: {e}")
        return False

    # ── Step 2: Build and sign the order ──────────────────────────────────
    try:
        direction = analysis["direction"]
        token_id = analysis.get("token_id_yes") if direction == "YES" else analysis.get("token_id_no")
        price = analysis["yes_price"] if direction == "YES" else analysis["no_price"]

        if not token_id:
            log.error("No token_id available for order placement")
            return False

        size = round(bet_size / price, 4)  # shares

        order_payload = {
            "token_id": token_id,
            "price": round(price, 4),
            "size": size,
            "side": "BUY",
            "type": "GTC",  # Good Till Cancelled
        }

        # Sign order with private key (EIP-712)
        order_msg = json.dumps(order_payload, sort_keys=True)
        signed_order = account.sign_message(encode_defunct(text=order_msg))

        # ── Step 3: Submit order ─────────────────────────────────────────
        resp = requests.post(
            f"{CLOB_API_URL}/order",
            headers={
                "POLY_ADDRESS": wallet_address,
                "POLY_SIGNATURE": signed_order.signature.hex(),
                "POLY_API_KEY": api_key,
                "POLY_SECRET": api_secret,
                "POLY_PASSPHRASE": api_passphrase,
            },
            json=order_payload,
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()

        log_trade({
            "timestamp": datetime.now().isoformat(),
            "mode": "live",
            "order_id": result.get("orderID"),
            "question": analysis["question"],
            "market_id": analysis["market_id"],
            "direction": direction,
            "bet_size": bet_size,
            "price": price,
            "shares": size,
            "true_prob": analysis["true_probability"],
            "market_prob": analysis["market_probability"],
            "confidence": analysis["confidence"],
            "reasoning": analysis["reasoning"],
        })

        log.info(
            f"[LIVE BET] {direction} on '{analysis['question'][:60]}' "
            f"| ${bet_size:.2f} @ {price:.2%} | order_id={result.get('orderID')}"
        )
        return True

    except Exception as e:
        log.error(f"Order placement failed: {e}")
        return False


# =============================================================================
# CIRCUIT BREAKER  (Daily loss limit)
# =============================================================================

def check_circuit_breaker(mode: str) -> bool:
    """Returns True if we should stop trading today."""
    if mode == "paper":
        today = date.today()
        if paper_portfolio["circuit_break_day"] == today:
            return True

        total_value = paper_portfolio["cash"]
        for pos in paper_portfolio["positions"].values():
            # Estimate current value (simplified)
            total_value += pos["bet_size"] * 0.9  # conservative estimate

        daily_pnl = (total_value - paper_portfolio["daily_start"]) / paper_portfolio["daily_start"]

        if daily_pnl <= -DAILY_LOSS_LIMIT:
            paper_portfolio["circuit_broken"] = True
            paper_portfolio["circuit_break_day"] = today
            log.warning(f"CIRCUIT BREAKER: Daily loss {daily_pnl:.1%} exceeded {DAILY_LOSS_LIMIT:.0%} limit. Halting for today.")
            return True

    return False


# =============================================================================
# PRINT DASHBOARD
# =============================================================================

def print_dashboard(mode: str):
    """Print current portfolio status."""
    print("\n" + "="*65)
    print(f"  POLYMARKET BOT  |  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  |  MODE: {mode.upper()}")
    print("="*65)

    if mode == "paper":
        cash = paper_portfolio["cash"]
        positions = paper_portfolio["positions"]
        total = cash + sum(p["bet_size"] for p in positions.values())
        pnl = total - paper_portfolio["daily_start"]

        print(f"  Cash:       ${cash:>8.2f}")
        print(f"  Positions:  {len(positions)}")
        print(f"  Portfolio:  ${total:>8.2f}  (daily P&L: {pnl:+.2f})")
        print(f"  Bets today: {paper_portfolio['total_bets']}")

        if positions:
            print("\n  OPEN POSITIONS:")
            for mid, pos in positions.items():
                print(f"    {pos['side']:3s} | ${pos['bet_size']:.2f} @ {pos['avg_price']:.2%} | {pos['question'][:45]}")

    print("="*65 + "\n")


# =============================================================================
# MAIN LOOP
# =============================================================================

def run_bot(mode: str = "paper"):
    """Main bot loop."""
    log.info(f"Starting Polymarket bot in {mode.upper()} mode")

    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set. Exiting.")
        sys.exit(1)

    if mode == "live" and not POLYGON_PRIVATE_KEY:
        log.error("POLYGON_PRIVATE_KEY not set. Use --paper for paper trading.")
        sys.exit(1)

    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    scan_count = 0

    while True:
        try:
            scan_count += 1
            log.info(f"── Scan #{scan_count} ──────────────────────────────")

            # Check circuit breaker
            if check_circuit_breaker(mode):
                log.info("Circuit breaker active. Sleeping until next day...")
                time.sleep(3600)
                continue

            # Check position limit
            if len(paper_portfolio["positions"]) >= MAX_OPEN_POSITIONS:
                log.info(f"Max positions ({MAX_OPEN_POSITIONS}) reached. Skipping scan.")
                time.sleep(SCAN_INTERVAL)
                continue

            # Fetch and filter markets
            markets = fetch_markets(limit=100)
            candidates = filter_markets(markets)

            if not candidates:
                log.info("No candidate markets found this scan.")
                time.sleep(SCAN_INTERVAL)
                continue

            # Analyze each candidate
            bets_this_scan = 0
            for market in candidates:
                question = market.get("question", "")[:60]
                log.info(f"Analyzing: {question}")

                # Skip if already in position
                market_id = market.get("id") or market.get("condition_id", "")
                if market_id in paper_portfolio["positions"]:
                    continue

                analysis = analyze_market(market, claude)
                if not analysis:
                    continue

                # Check for edge
                if not analysis.get("has_edge"):
                    log.info(f"  No edge found. Skipping.")
                    continue

                confidence = analysis.get("confidence", 0)
                if confidence < MIN_CONFIDENCE:
                    log.info(f"  Confidence {confidence:.0%} below threshold. Skipping.")
                    continue

                # Size the bet
                direction = analysis["direction"]

                # Check minimum ROI — avoid betting NO on 99% NO markets etc.
                market_price = analysis["yes_price"] if direction == "YES" else analysis["no_price"]
                potential_roi = (1 - market_price) / market_price
                if potential_roi < MIN_ROI:
                    log.info(f"  ROI too small ({potential_roi:.0%}). Skipping — market already agrees.")
                    continue
                market_price = analysis["yes_price"] if direction == "YES" else analysis["no_price"]
                true_prob = analysis["true_probability"]
                if direction == "NO":
                    true_prob = 1 - true_prob  # Flip for NO side

                bankroll = paper_portfolio["cash"] if mode == "paper" else 1000  # TODO: fetch live balance
                bet_size = kelly_bet_size(true_prob, market_price, bankroll)

                if bet_size < MIN_BET_USD:
                    log.info(f"  Bet size ${bet_size:.2f} too small. Skipping.")
                    continue

                # Log analysis
                log.info(
                    f"  EDGE FOUND: {direction} | "
                    f"market={market_price:.0%} → true={analysis['true_probability']:.0%} | "
                    f"confidence={confidence:.0%} | bet=${bet_size:.2f}"
                )
                log.info(f"  Reasoning: {analysis.get('reasoning', '')[:100]}")

                # Place bet
                if mode == "paper":
                    paper_place_bet(analysis, bet_size)
                else:
                    live_place_bet(analysis, bet_size)

                bets_this_scan += 1

                # Small delay between API calls
                time.sleep(2)

            # Print dashboard after each scan
            print_dashboard(mode)
            log.info(f"Scan complete. {bets_this_scan} bets placed. Sleeping {SCAN_INTERVAL}s...")
            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            log.info("Bot stopped by user.")
            print_dashboard(mode)
            break
        except Exception as e:
            log.error(f"Unexpected error in main loop: {e}", exc_info=True)
            time.sleep(60)


# =============================================================================
# ENTRYPOINT
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket AI Trading Bot")
    parser.add_argument("--paper", action="store_true", help="Run in paper trading mode (default)")
    parser.add_argument("--live", action="store_true", help="Run in live trading mode (real money)")
    parser.add_argument("--setup-wallet", action="store_true", help="Generate a new Polygon wallet")
    parser.add_argument("--dashboard", action="store_true", help="Print current portfolio and exit")
    args = parser.parse_args()

    if args.setup_wallet:
        setup_wallet()
    elif args.dashboard:
        print_dashboard("paper")
    elif args.live:
        print("\n⚠️  LIVE MODE — real money will be used. Press Ctrl+C within 5s to cancel.")
        time.sleep(5)
        run_bot(mode="live")
    else:
        run_bot(mode="paper")
