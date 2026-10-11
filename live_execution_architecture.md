# Live Execution Layer — Architectural Sketch

Status: DESIGN ONLY (no implementation yet).
Built once paper-trading confirms go/no-go.

This document specifies WHAT the live execution layer will do, HOW it fits
into the existing bot, and the safety rails baked in. Read it, push back on
anything you disagree with, then we lock the design.


## Decisions already locked

| # | Decision                          | Value                                   |
|---|-----------------------------------|-----------------------------------------|
| 1 | Max daily loss                    | 30% of bankroll                         |
| 2 | Slippage cap                      | 2 cents (skip order if price moved >2c) |
| 3 | Failure retry                     | retry network/timeout 2x, else give up  |
| 4 | Credential handling               | user-managed only, never in chat/logs   |
| 5 | Reconciliation                    | Polymarket API is source of truth       |
| 6 | Build approach                    | architecture now, code after paper OK   |


## What changes in the existing bot

Tiny surface. Only one function changes behavior, and one new module is added.

```
crypto_bot_market.py
├── paper_bet(decision)        ← unchanged when --paper flag is set
└── execute_bet(decision)      ← NEW: dispatches to paper OR live
                                  based on --paper / --live flag

NEW: polymarket_executor.py    ← all real-money execution lives here
NEW: kill_switch.py            ← standalone emergency stop script
```

The decision-making logic (signals, 3-way agreement, veto, etc.) is
completely untouched. Only the *placement* of the trade differs.


## File: polymarket_executor.py

The whole real-money layer is one module with a small public surface.

```python
class PolymarketExecutor:
    """Real-money order placement on Polymarket's CLOB."""

    def __init__(self, credential_path: str, bankroll_cap: float,
                 daily_loss_cap_pct: float = 0.30,
                 slippage_cap_cents: float = 0.02):
        # Load credential from file (never from code, never logged).
        # credential_path is something like ~/.polymarket_credentials,
        # chmod 600, read once on startup.
        ...

    # ── Pre-flight checks (run before EVERY order) ───────────────────────
    def can_trade(self) -> tuple[bool, str]:
        """
        Returns (allowed, reason). Blocks order if:
          - kill switch file exists
          - daily loss already exceeds cap
          - too many trades in last hour (rate limit)
          - reconciliation hasn't run successfully
        """

    def check_slippage(self, intended_price: float,
                       current_ask: float) -> tuple[bool, str]:
        """
        Returns (within_cap, reason). Rejects if current_ask is more
        than slippage_cap_cents above intended_price.
        """

    # ── Order placement ──────────────────────────────────────────────────
    def place_order(self, decision: dict) -> dict:
        """
        Place a real market order on Polymarket.

        Returns:
          {
            "status":      "filled" | "partial" | "rejected" | "failed",
            "order_id":    str | None,
            "fill_price":  float (avg fill price, what you really paid),
            "fill_shares": float (shares actually acquired),
            "fee_paid":    float (in USDC),
            "tx_hash":     str | None (Polygon tx if applicable),
            "error":       str | None,
          }

        Internally:
          1. Calls can_trade() — abort if blocked.
          2. Re-fetches current ask price for the market.
          3. Calls check_slippage() — abort if moved.
          4. Submits the order. Up to 2 retries on network/timeout.
          5. Polls order status until terminal state or 10s timeout.
          6. Computes actual fill price and fee from server response.
          7. Returns the structured result.
          8. Logs everything (without secrets).
          9. Sends Telegram alert with result.
        """

    # ── Reconciliation (source of truth = Polymarket) ────────────────────
    def reconcile_positions(self, local_positions: dict) -> dict:
        """
        On startup and periodically: fetch real positions from
        Polymarket API. Return the corrected positions dict.
        Local state is overwritten to match server. Any drift is
        logged and alerted.
        """

    # ── Daily loss tracking ──────────────────────────────────────────────
    def record_outcome(self, profit_usd: float) -> None:
        """Track realized P&L for the daily loss cap."""

    def daily_loss_pct(self) -> float:
        """Current % loss vs daily_start. Trips the cap when >= 30%."""

    # ── Kill switch hooks ────────────────────────────────────────────────
    def is_killed(self) -> bool:
        """Returns True if ~/.killswitch file exists."""
```


## File: kill_switch.py

A one-line emergency stop. Standalone — no dependencies on the bot.

```
Usage:
  python kill_switch.py on    # stop all new orders immediately
  python kill_switch.py off   # resume trading
  python kill_switch.py status

What it does:
  - "on"  : creates ~/.killswitch file. Every executor checks this
            before EVERY order. If file exists, no new orders are placed.
            Existing positions resolve normally (we don't try to close
            them — selling 5-min markets is messy).
  - "off" : removes the file.
  - status: prints current state and time since toggled.

This file is the ONE thing standing between you and accidental real-money
trades. It's a hard local file check, not an API setting. Even if the bot
is bugged or the executor is broken, the file check happens first.
```


## What the bot loop looks like in live mode

The existing loop in run_bot() barely changes. The dispatch is one line:

```python
# At the bottom of the scan loop where it currently calls paper_bet:
if LIVE_MODE:
    result = executor.place_order(decision)
    if result["status"] == "filled":
        record_real_position(decision, result)
    else:
        log.warning(f"Order not filled: {result['status']} — {result.get('error')}")
else:
    paper_bet(decision)
```

That's it. The strategy code never knows whether it's paper or live.


## Safety rails — the things that ABSOLUTELY MUST happen before any order

These are non-negotiable preconditions. Every single real order is gated by:

1. **--live flag explicitly set** (default is --paper, you must opt in each run)
2. **Credentials file exists** and is readable
3. **~/.killswitch file does NOT exist**
4. **Reconciliation has succeeded at least once this session**
5. **Daily loss < 30% of starting bankroll today**
6. **Hourly trade count < some sane limit** (say 30)
7. **Bot is in the betting window** (existing check)
8. **Current ask is within 2c of intended price**
9. **Position size respects bankroll cap** (won't bet $5 with $50 bankroll)
10. **Polymarket API is reachable** (recent successful health check)

If any of these fails, the order is skipped, logged, and Telegram-alerted.
ZERO bypass paths. ZERO "force" flags. The only way to lift a block is to
fix the underlying condition (e.g. delete the killswitch file manually).


## Credential handling — what stays on YOUR machine forever

The bot needs an API credential to place orders. To be crystal clear about
what's where:

```
NEVER in this chat:        any private key, any seed phrase
NEVER in any code I write: hardcoded credentials
NEVER in logs:             the bot logs operations, never secrets
NEVER in patches:          supervisor patches must not touch executor files
NEVER in uploaded files:   if you ever upload portfolio/logs again,
                           the credential file is NOT included

ONLY in one place:         ~/.polymarket_credentials
                           chmod 600 (only you can read)
                           created by you, contents you choose
                           read once at bot startup
```

You generate the credential through Polymarket's official process,
yourself, on the Seoul server. The bot reads from the file path. That's
the entire integration. I never see the contents.


## What I write vs what you do — clear boundaries

| Step                                       | Who    |
|--------------------------------------------|--------|
| Write polymarket_executor.py               | me     |
| Write kill_switch.py                       | me     |
| Add --live flag dispatch in bot            | me     |
| Wire up Telegram alerts for live events    | me     |
| Set up Polymarket account                  | you    |
| Fund the wallet                            | you    |
| Generate API credential                    | you    |
| Save credential to ~/.polymarket_credentials | you   |
| Decide when to flip --paper to --live      | you    |
| Decide bet size                            | you    |
| Hit the kill switch if something's wrong   | you    |


## Open questions to resolve before I implement

These are things I don't want to decide for you silently:

1. **Telegram alert volume.** Real fills you definitely want notified.
   But do you want every skip, every veto, every reconciliation diff?
   Or just: fills, errors, kill-switch trips, daily summary?
   (My suggestion: just those four. Otherwise noise overwhelms signal.)

2. **What to do with paper bot when live is running.** Keep paper running
   in parallel as a baseline (so you can compare live vs paper drift), or
   shut paper off to avoid confusion? (My suggestion: keep paper running
   forever — best diagnostic if live ever diverges.)

3. **Initial bankroll size.** Plan was $50. Still the right number?
   Anything larger is meaningful real money; anything smaller may not
   produce enough fills to learn from.

4. **Bet size.** $2.40 like paper, or smaller? $50 bankroll × 5% = $2.50,
   so $2.40 is roughly 5% of bankroll which is HIGH per-trade exposure.
   Suggestion: $1.00 bets. Smaller per-trade risk, more fills for learning.

5. **Live → paper fallback.** If credentials fail to load or kill switch
   trips, should the bot quietly fall back to paper mode and keep running,
   or stop entirely? (My suggestion: STOP entirely. If something's wrong,
   stop. Don't paper-trade-as-fallback because that's a behavior change
   you might not notice.)


## What this design does NOT include

Being explicit about scope:

- No automatic position closing (we hold to resolution)
- No multi-account support (one credential, one wallet)
- No supervisor patching of executor code (off-limits, like signal functions)
- No supervisor changes to bet sizing in live mode (you control that)
- No alerts for paper-mode events (paper stays quiet)
- No automatic rebalancing or withdrawal
- No tax/reporting features
- No GUI — everything is SSH + tmux + Telegram alerts


## When this gets built

Trigger: market bot's 5-day paper sample shows net-of-fee profitability
with a stable win rate (~57%+) across at least one mixed-conditions stretch.

Until then: paper-only on the market bot. This document waits.

If the trigger fires, we revisit this document, confirm nothing has
changed in the design, then I implement it module by module:

1. kill_switch.py (simplest, builds confidence in the safety model)
2. polymarket_executor.py (the core)
3. --live dispatch in the bot
4. Telegram alerts
5. End-to-end test in a sandbox / with a $1 test order before any
   real strategy money

Each module gets reviewed by you before it goes live.


## Final note on what could still go wrong

I want to be straight: even with all of this scaffolding, real money on
a live market has irreducible risk:

- Polymarket could change their API
- Polygon could have a network event
- A bug I didn't anticipate could exist somewhere
- Your strategy's edge could vanish for reasons unrelated to the bot
- Adverse selection effects we discussed could be larger live than paper
- The 73% win rate from day one was almost certainly a lucky day —
  expect it to regress

The scaffolding makes the bot SAFE-ish. It doesn't make it PROFITABLE.
Profitability is determined by your strategy, not the execution layer.
