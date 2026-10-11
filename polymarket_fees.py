"""
polymarket_fees.py — Shared cost model for both bots.

Implements Polymarket's actual taker-fee formula so paper trading
reflects real-world costs. Used by BOTH the market-order bot and the
limit-order bot so their results are directly comparable.

Source: Polymarket official docs (trading/fees), verified May 2026.

FORMULA:
    fee_usdc = C * rate * p * (1 - p)
    where:
        C    = number of shares traded
        rate = category fee rate (Crypto = 0.072)
        p    = share price (0..1)

    The p*(1-p) term creates a bell curve peaking at p=0.50.
    For crypto at p=0.50: 0.072 * 0.25 = 0.018 = 1.8% effective (matches headline).

KEY RULES:
    - Only TAKERS pay fees. Market orders are always takers.
    - MAKERS pay zero. A limit order that rests on the book and gets
      filled by someone else is a maker → no fee.
    - A limit order priced to CROSS the spread fills as a taker → still pays.
    - Fee is symmetric around 50%: a trade at 30c costs the same $ as 70c.
    - Minimum fee 0.00001 USDC, rounded to 5 decimals.
    - Sells are not charged a separate taker fee here (we hold to resolution).
"""

# Category fee rates (the `rate` in the formula). Crypto is highest.
CATEGORY_RATES = {
    "crypto":     0.072,
    "sports":     0.030,
    "finance":    0.040,
    "politics":   0.040,
    "tech":       0.040,
    "economics":  0.060,
    "culture":    0.050,
    "weather":    0.050,
    "geopolitics": 0.0,    # fee-free
}

MIN_FEE_USDC = 0.00001

# Crypto maker-rebate share of collected fees (~20%), paid daily.
# We model this CONSERVATIVELY and OFF by default — it's discretionary,
# pooled, and not guaranteed per-trade. Enable only for optimistic scenarios.
CRYPTO_MAKER_REBATE_SHARE = 0.20


def taker_fee(shares: float, price: float, category: str = "crypto") -> float:
    """
    Compute the taker fee in USDC for a trade.

    shares   : number of shares (C)
    price    : share price p in 0..1
    category : market category (default crypto)

    Returns fee in USDC (>= MIN_FEE_USDC if any fee applies, else 0.0).
    """
    rate = CATEGORY_RATES.get(category, 0.072)
    if rate <= 0:
        return 0.0
    fee = shares * rate * price * (1.0 - price)
    fee = round(fee, 5)
    if fee <= 0:
        return 0.0
    return max(fee, MIN_FEE_USDC)


def effective_fee_pct(price: float, category: str = "crypto") -> float:
    """Effective fee as a fraction of notional (for reporting/intuition)."""
    rate = CATEGORY_RATES.get(category, 0.072)
    # fee / notional = (C*rate*p*(1-p)) / (C*p) = rate*(1-p)
    return rate * (1.0 - price)


# ── Market-order (TAKER) cost model ─────────────────────────────────────────
def market_order_cost(bet_size_usd: float, ask_price: float,
                      category: str = "crypto") -> dict:
    """
    Model a TAKER market order.

    The order fills at the ASK (what you really pay), and pays the taker fee.
    Fee on BUYS is collected in shares; we convert to an effective USDC cost
    and reduce the shares acquired accordingly so downstream payout math is
    consistent.

    Returns dict with:
        fill_price   : price actually paid (the ask)
        gross_shares : shares before fee
        fee_usd      : taker fee in USDC
        net_shares   : shares after fee deduction
        cost_usd     : total cash outlay (bet_size_usd)
    """
    fill_price   = ask_price
    gross_shares = bet_size_usd / fill_price
    fee_usd      = taker_fee(gross_shares, fill_price, category)
    # Fee collected in shares on buys: convert USDC fee to share-equivalent
    fee_shares   = fee_usd / fill_price if fill_price > 0 else 0.0
    net_shares   = gross_shares - fee_shares
    return {
        "fill_price":   fill_price,
        "gross_shares": round(gross_shares, 6),
        "fee_usd":      round(fee_usd, 5),
        "net_shares":   round(net_shares, 6),
        "cost_usd":     round(bet_size_usd, 2),
    }


# ── Limit-order (MAKER) cost model ──────────────────────────────────────────
def limit_order_cost(bet_size_usd: float, limit_price: float,
                     category: str = "crypto",
                     count_rebate: bool = False) -> dict:
    """
    Model a MAKER limit order that rests on the book and gets filled.

    Maker orders pay ZERO taker fee. Optionally model a small maker rebate
    (off by default — it's discretionary and pooled, so don't count on it).

    NOTE: This assumes the limit order did NOT cross the spread (i.e. it was
    a genuine maker). The caller (bot) is responsible for only treating an
    order as filled-as-maker when the market traded down/up to the resting
    price, not when it crossed.

    Returns dict with fill_price, gross_shares, fee_usd (0), net_shares,
    rebate_usd, cost_usd.
    """
    fill_price   = limit_price
    gross_shares = bet_size_usd / fill_price
    fee_usd      = 0.0
    rebate_usd   = 0.0
    if count_rebate and category == "crypto":
        # Conservative: rebate is a small share of the equivalent taker fee.
        equiv_fee  = taker_fee(gross_shares, fill_price, category)
        rebate_usd = round(equiv_fee * CRYPTO_MAKER_REBATE_SHARE, 5)
    return {
        "fill_price":   fill_price,
        "gross_shares": round(gross_shares, 6),
        "fee_usd":      0.0,
        "net_shares":   round(gross_shares, 6),
        "rebate_usd":   rebate_usd,
        "cost_usd":     round(bet_size_usd, 2),
    }


# ── Payout helper ───────────────────────────────────────────────────────────
def resolve_payout(net_shares: float, won: bool, cost_usd: float,
                   rebate_usd: float = 0.0) -> float:
    """
    Compute realized profit for a resolved position.

    net_shares : shares held after any fee
    won        : True if the bet won (shares pay $1 each)
    cost_usd   : original cash outlay
    rebate_usd : any maker rebate credited (default 0)

    Winning shares pay $1.00 USDC each. No fee on winnings.
    """
    if won:
        winnings = net_shares * 1.0
        return round(winnings - cost_usd + rebate_usd, 4)
    else:
        return round(-cost_usd + rebate_usd, 4)


if __name__ == "__main__":
    # Quick self-test / illustration at the 50c peak
    print("Crypto fee at various prices for a $2.40 bet:")
    for p in [0.50, 0.53, 0.55, 0.60, 0.70, 0.90]:
        shares = 2.40 / p
        f = taker_fee(shares, p, "crypto")
        print(f"  price={p:.2f}  shares={shares:5.2f}  "
              f"taker_fee=${f:.4f}  ({effective_fee_pct(p)*100:.2f}% of notional)")

    print("\nMarket order ($2.40 @ 0.53 ask):")
    print(" ", market_order_cost(2.40, 0.53))
    print("\nLimit order ($2.40 @ 0.53 maker):")
    print(" ", limit_order_cost(2.40, 0.53))
