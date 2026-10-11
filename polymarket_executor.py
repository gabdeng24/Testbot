"""
polymarket_executor.py — Real-money order placement on Polymarket CLOB V2.

⚠️  THIS PLACES REAL ORDERS WITH REAL MONEY when used with --live.
⚠️  Every public method is gated by safety checks. There are NO bypass paths.

═══════════════════════════════════════════════════════════════════════════
V2 MIGRATION (April 28, 2026)
═══════════════════════════════════════════════════════════════════════════
Polymarket replaced its entire trading stack on 2026-04-28: new CTF Exchange
V2 contracts, rewritten CLOB backend, and pUSD collateral (replaces USDC.e).
This module targets the official `py-clob-client-v2` package and was written
against its REAL inspected signatures (not docs assumptions):

  ClobClient(host, chain_id, key, creds, signature_type, funder, ...)
  MarketOrderArgs(token_id, amount, side: str, price=0, order_type=FOK,
                  user_usdc_balance=0, builder_code=..., metadata=...)
  create_and_post_market_order(order_args, options=None, order_type=FOK, ...)
  get_balance_allowance(params: BalanceAllowanceParams)
  update_balance_allowance(params: BalanceAllowanceParams)   <- balance sync
  get_clob_market_info(condition_id) -> dict                 <- fee params

VERIFIED 2026-06-11 by a real $1 order (orderID 0xa559..., status "matched"):
  - signature_type for post-V2 deposit-wallet accounts = 3 (ERC-1271)
  - update_balance_allowance(sigtype 3) is the required balance sync — it
    surfaced the pUSD balance AND set all three exchange allowances
  - market-order response shape (see place_order for the field mapping)
Remaining # VERIFY: Data API positions field names (account now holds a
position, so the next reconcile run can confirm them).
═══════════════════════════════════════════════════════════════════════════

CREDENTIALS:
    Loaded from a JSON file you create (default ~/.polymarket_credentials),
    chmod 600. Secrets never appear in logs. Claude never sees them.

    {
      "private_key":    "0x...",   # wallet key (NEVER share)
      "api_key":        "...",     # carry over from V1 — auth unchanged in V2
      "api_secret":     "...",
      "api_passphrase": "...",
      "funder_address": "0x...",   # deposit wallet holding pUSD (0x3dd1...)
      "signature_type": 3          # post-V2 deposit-wallet accounts = 3 (verified)
    }

SAFETY RAILS (all enforced before every order):
    - --live must be explicitly set by the bot (default paper)
    - credentials file must load
    - kill switch (~/.killswitch) must NOT exist
    - reconciliation must have succeeded at least once this session
    - daily loss < cap (default 30%)
    - hourly trade count < cap (default 30)
    - current ask within slippage cap of intended price (default 2c)
    - position size respects bankroll cap
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta
from typing import Optional

import kill_switch  # for is_killed()

log = logging.getLogger("executor")

# ── py-clob-client-v2 import (official Polymarket V2 client) ─────────────────
try:
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import (
        MarketOrderArgs, OrderType, ApiCreds,
        BalanceAllowanceParams, AssetType,
    )
    CLOB_AVAILABLE = True
except ImportError:
    CLOB_AVAILABLE = False
    log.warning("py-clob-client-v2 not installed — executor cannot place live orders. "
                "Run: pip install py-clob-client-v2")


class PolymarketExecutor:
    """Real-money order placement on CLOB V2 with hard safety rails."""

    def __init__(self,
                 credential_path: str = "~/.polymarket_credentials",
                 bankroll_cap: float = 50.0,
                 daily_loss_cap_pct: float = 0.30,
                 slippage_cap_cents: float = 0.02,
                 min_fill_price: float = 0.30,
                 max_trades_per_hour: int = 30,
                 max_position_pct: float = 0.05,
                 telegram_fn=None):
        """
        credential_path     : path to JSON credentials file (you create it)
        bankroll_cap        : total real bankroll in USD (pUSD)
        daily_loss_cap_pct  : halt new orders if daily loss exceeds this (0.30 = 30%)
        slippage_cap_cents  : reject order if price moved more than this (0.02 = 2c)
        max_trades_per_hour : rate limit
        max_position_pct    : max single bet as fraction of bankroll (0.05 = 5%)
        telegram_fn         : optional fn(str) to send alerts
        """
        self.bankroll_cap        = bankroll_cap
        self.daily_loss_cap_pct  = daily_loss_cap_pct
        self.slippage_cap_cents  = slippage_cap_cents
        self.min_fill_price      = min_fill_price
        self.max_trades_per_hour = max_trades_per_hour
        self.max_position_pct    = max_position_pct
        self.telegram_fn         = telegram_fn

        self._reconciled     = False
        self._daily_start    = bankroll_cap
        self._daily_date     = datetime.now().date()
        self._daily_realized = 0.0
        self._recent_trades  = []   # timestamps for rate limiting

        self.client = None
        self.signature_type = 1     # overridden by credentials file if present
        self._load_credentials(os.path.expanduser(credential_path))

    # ── Credentials ─────────────────────────────────────────────────────────
    def _load_credentials(self, path: str):
        """Load credentials and init the V2 CLOB client. Secrets never logged."""
        if not CLOB_AVAILABLE:
            log.error("py-clob-client-v2 not available — cannot init executor")
            return
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"Credentials file not found: {path}\n"
                f"Create it yourself (chmod 600) with your Polymarket API keys."
            )
        mode = oct(os.stat(path).st_mode)[-3:]
        if mode != "600":
            log.warning(f"⚠️  Credentials file {path} has mode {mode}, "
                        f"should be 600. Run: chmod 600 {path}")

        with open(path) as f:
            creds = json.load(f)

        api_creds = ApiCreds(
            api_key=creds["api_key"],
            api_secret=creds["api_secret"],
            api_passphrase=creds["api_passphrase"],
        )

        # signature_type: V2 docs example uses 1 (proxy). Some new accounts
        # may differ — test_one_order.py probes empirically. Set the value it
        # confirms into the credentials file as "signature_type".
        self.signature_type = int(creds.get("signature_type", 1))

        # chain_id 137 = Polygon mainnet. V2 took over the production URL on
        # 2026-04-28, so the host is unchanged. funder = the deposit wallet
        # that HOLDS YOUR pUSD (the address pUSD was minted to), NOT the
        # signer EOA derived from the private key.
        self.client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=creds["private_key"],
            creds=api_creds,
            signature_type=self.signature_type,
            funder=creds["funder_address"],
        )
        log.info(f"Credentials loaded; CLOB V2 client initialized "
                 f"(signature_type={self.signature_type}).")

    # ── V2 balance sync ──────────────────────────────────────────────────────
    def sync_balance(self) -> Optional[float]:
        """
        V2 requires the CLOB's balance cache to be synced before it 'sees'
        your pUSD. Calls update_balance_allowance then reads the balance.
        Returns balance in USD (float), or None on failure.
        """
        if not CLOB_AVAILABLE or self.client is None:
            return None
        try:
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=self.signature_type,
            )
            try:
                self.client.update_balance_allowance(params)  # sync the cache
            except Exception as e:
                log.warning(f"update_balance_allowance: {e} (continuing to read)")
            bal = self.client.get_balance_allowance(params)
            raw = bal.get("balance") if isinstance(bal, dict) else None
            usd = float(raw) / 1e6 if raw is not None else None  # 6-dp integer
            log.info(f"Collateral balance: ${usd:.2f}" if usd is not None
                     else f"Balance read returned: {bal}")
            return usd
        except Exception as e:
            log.error(f"sync_balance failed: {e}")
            return None

    # ── Pre-flight checks ─────────────────────────────────────────────────────
    def can_trade(self) -> tuple:
        """
        Returns (allowed: bool, reason: str). Checked before EVERY order.
        Every blocking condition is listed explicitly; no bypass exists.
        """
        if not CLOB_AVAILABLE or self.client is None:
            return False, "CLOB client not initialized"

        if kill_switch.is_killed():
            return False, "kill switch engaged (~/.killswitch exists)"

        if not self._reconciled:
            return False, "reconciliation has not succeeded this session"

        today = datetime.now().date()
        if today != self._daily_date:
            self._daily_date     = today
            self._daily_realized = 0.0
            self._daily_start    = self.bankroll_cap

        loss_pct = self.daily_loss_pct()
        if loss_pct >= self.daily_loss_cap_pct:
            return False, (f"daily loss {loss_pct:.0%} >= cap "
                           f"{self.daily_loss_cap_pct:.0%} — halted for the day")

        cutoff = datetime.now() - timedelta(hours=1)
        self._recent_trades = [t for t in self._recent_trades if t >= cutoff]
        if len(self._recent_trades) >= self.max_trades_per_hour:
            return False, (f"hourly trade limit reached "
                           f"({self.max_trades_per_hour}/hr)")

        return True, "ok"

    def check_slippage(self, intended_price: float, current_ask: float) -> tuple:
        """Returns (within_cap, reason). Rejects if ask moved beyond cap."""
        move = current_ask - intended_price
        if move > self.slippage_cap_cents:
            return False, (f"slippage {move*100:.1f}c > cap "
                           f"{self.slippage_cap_cents*100:.0f}c "
                           f"(intended {intended_price:.0%}, ask {current_ask:.0%})")
        return True, "ok"

    def check_position_size(self, bet_size: float) -> tuple:
        """Returns (ok, reason). Enforces max single-bet fraction of bankroll."""
        max_bet = self.bankroll_cap * self.max_position_pct
        if bet_size > max_bet + 1e-9:
            return False, (f"bet ${bet_size:.2f} exceeds max "
                           f"${max_bet:.2f} ({self.max_position_pct:.0%} of bankroll)")
        return True, "ok"

    # ── Order placement ───────────────────────────────────────────────────────
    def place_order(self, decision: dict) -> dict:
        """
        Place a real market order. Returns a structured result dict.
        Every safety check runs first; any failure aborts with status 'rejected'.

        CONTRACT: decision["token_id"] must be the CLOB token id of the
        OUTCOME BEING BOUGHT (the YES token if betting YES, the NO token if
        betting NO). The executor always BUYs that token with bet_size USD —
        in a binary market, buying the NO token IS the "bet NO" action.
        """
        result = {
            "status": "rejected", "order_id": None, "fill_price": None,
            "fill_shares": 0.0, "fee_paid": 0.0, "tx_hash": None, "error": None,
        }

        # 1. Global pre-flight
        ok, reason = self.can_trade()
        if not ok:
            result["error"] = f"can_trade blocked: {reason}"
            log.warning(result["error"])
            self._alert(f"⛔ Order blocked: {reason}")
            return result

        # 2. Position size
        ok, reason = self.check_position_size(decision["bet_size"])
        if not ok:
            result["error"] = f"position size blocked: {reason}"
            log.warning(result["error"])
            return result

        # 3. Re-fetch current price & check slippage
        token_id = decision.get("token_id")
        if not token_id:
            result["error"] = "decision missing token_id for CLOB order"
            log.error(result["error"])
            return result

        try:
            current_ask = self._get_current_ask(token_id)
        except Exception as e:
            result["error"] = f"could not fetch current price: {e}"
            log.error(result["error"])
            return result

        ok, reason = self.check_slippage(decision["bet_price"], current_ask)
        if not ok:
            result["error"] = f"slippage blocked: {reason}"
            log.info(result["error"])  # normal/expected sometimes
            return result

        # Fill-price floor: refuse fills below min_fill_price. Empirically
        # (48h live data) every dollar of loss came from fills < 0.30 — a
        # price that has already collapsed that far signals the market has
        # information against the bet, not "our signal at a discount."
        if current_ask < self.min_fill_price:
            result["error"] = (f"fill-price floor: ask {current_ask:.2f} "
                               f"< floor {self.min_fill_price:.2f} "
                               f"(deep-dump fill, skipped)")
            log.info(result["error"])
            return result

        # 4. Submit with limited retry on network/timeout only.
        expected_shares = decision["bet_size"] / max(current_ask, 1e-9)
        order_result = self._submit_with_retry(token_id, decision["bet_size"])
        if order_result is None:
            result["status"] = "failed"
            result["error"]  = "order submission failed after retries"
            log.error(result["error"])
            self._alert(f"❌ Order FAILED after retries: {decision['question'][:50]}")
            return result

        # 5. Interpret the fill
        # VERIFIED 2026-06-11 via real $1 order. V2 response shape:
        #   { "errorMsg": "", "orderID": "0x...", "takingAmount": "1.923075",
        #     "makingAmount": "0.999999", "status": "matched",
        #     "transactionsHashes": ["0x..."], "success": true }
        # On a BUY: makingAmount = USD (pUSD) spent, takingAmount = shares
        # received. Effective fill price = makingAmount / takingAmount.
        if not isinstance(order_result, dict):
            order_result = getattr(order_result, "__dict__", {}) or {}
        success       = bool(order_result.get("success"))
        api_status    = order_result.get("status")          # e.g. "matched"
        err_msg       = order_result.get("errorMsg") or None
        usd_spent     = float(order_result.get("makingAmount") or 0)
        filled_shares = float(order_result.get("takingAmount") or 0)
        avg_price     = (usd_spent / filled_shares) if filled_shares > 0 else current_ask
        order_id      = order_result.get("orderID")
        tx_hashes     = order_result.get("transactionsHashes") or []

        if not success or filled_shares <= 0 or api_status != "matched":
            result["status"]   = "rejected"
            result["order_id"] = order_id
            result["error"]    = (err_msg or
                                  f"order not matched (success={success}, "
                                  f"status={api_status}, shares={filled_shares})")
            log.warning(result["error"])
            return result

        # FOK semantics: matched means fully executed — no partial fills.
        status = "filled"
        # Fee: V2 charges takers at match time with the same formula our
        # paper model uses; we estimate locally for P&L tracking. The exact
        # per-market rate is queryable via get_clob_market_info(condition_id).
        import polymarket_fees as pf
        fee = pf.taker_fee(filled_shares, avg_price, "crypto")

        result.update({
            "status":      status,
            "order_id":    order_id,
            "fill_price":  round(avg_price, 4),
            "fill_shares": round(filled_shares, 4),
            "fee_paid":    round(fee, 5),
            "tx_hash":     tx_hashes[0] if tx_hashes else None,
            "error":       None,
        })

        self._recent_trades.append(datetime.now())
        log.info(f"[LIVE-FILL] {decision['bet_side']} '{decision['question'][:45]}' "
                 f"| {filled_shares:.2f} sh @ {avg_price:.0%} "
                 f"| fee ${fee:.4f} | status={status}")
        self._alert(
            f"✅ LIVE FILL: {decision['bet_side']} {decision['question'][:40]}\n"
            f"   {filled_shares:.2f} shares @ {avg_price:.0%}\n"
            f"   intended {decision['bet_price']:.0%}, drift {(avg_price-decision['bet_price'])*100:+.1f}c\n"
            f"   fee ${fee:.4f} | status {status}"
        )
        return result

    def _submit_with_retry(self, token_id: str, bet_size_usd: float):
        """
        Submit a V2 MARKET BUY (FOK). Retry ONLY on network/timeout errors,
        max 2 retries. Any other error → give up immediately (no duplicate-
        trade risk).

        V2 notes (verified against installed signatures):
          - MarketOrderArgs takes side as a plain string "BUY"/"SELL".
          - amount on a BUY is the USD (pUSD) to spend.
          - user_usdc_balance lets the SDK compute fee-adjusted fills.
          - create_and_post_market_order builds, signs and posts in one call.
        """
        attempts = 0
        max_attempts = 3  # initial + 2 retries
        while attempts < max_attempts:
            attempts += 1
            try:
                order_args = MarketOrderArgs(
                    token_id=token_id,
                    amount=round(bet_size_usd, 2),       # USD to spend
                    side="BUY",
                    order_type=OrderType.FOK,
                    user_usdc_balance=self.bankroll_cap,  # fee-aware fill calc
                )
                resp = self.client.create_and_post_market_order(
                    order_args, order_type=OrderType.FOK)
                return resp
            except (TimeoutError, ConnectionError) as e:
                log.warning(f"Network error on attempt {attempts}/{max_attempts}: {e}")
                if attempts < max_attempts:
                    time.sleep(1.5)
                    continue
                return None
            except Exception as e:
                log.error(f"Non-recoverable order error: {e}")
                return None
        return None

    def _get_current_ask(self, token_id: str) -> float:
        """Current best ask (what we'd pay to BUY) for a token."""
        resp = self.client.get_price(token_id=token_id, side="BUY")
        if isinstance(resp, dict):
            return float(resp.get("price"))
        return float(resp)

    # ── Reconciliation (Polymarket is source of truth) ────────────────────────
    def reconcile_positions(self, local_positions: dict) -> dict:
        """
        Fetch real positions from Polymarket's Data API and return corrected
        dict. On success, sets self._reconciled = True (required to trade).
        Also runs the V2 balance sync so the CLOB cache is fresh.
        """
        if not CLOB_AVAILABLE or self.client is None:
            log.error("Cannot reconcile — client not initialized")
            return local_positions
        try:
            import requests
            addr = self.client.get_address()
            resp = requests.get(
                "https://data-api.polymarket.com/positions",
                params={"user": addr},
                timeout=10,
            )
            resp.raise_for_status()
            real_list = resp.json()  # VERIFY: confirmed list on empty account; field names once a position exists

            corrected = {}
            for p in real_list:
                mid = p.get("conditionId") or p.get("market") or p.get("asset")  # VERIFY
                if not mid:
                    continue
                corrected[mid] = {
                    "shares":    float(p.get("size", 0)),
                    "avg_price": float(p.get("avgPrice", 0) or 0),
                    "outcome":   p.get("outcome"),
                }

            local_ids = set(local_positions.keys())
            real_ids  = set(corrected.keys())
            only_local = local_ids - real_ids
            only_real  = real_ids - local_ids
            if only_local:
                log.warning(f"Reconcile: in local but NOT on Polymarket: {only_local}")
            if only_real:
                log.warning(f"Reconcile: on Polymarket but NOT local: {only_real}")
                self._alert(f"⚠️ Reconcile found {len(only_real)} untracked real position(s)")

            self._reconciled = True
            log.info(f"Reconciliation OK — {len(corrected)} real position(s).")
            self.sync_balance()  # refresh CLOB balance cache while we're at it
            return corrected
        except Exception as e:
            log.error(f"Reconciliation FAILED: {e}")
            self._reconciled = False
            self._alert(f"⚠️ Reconciliation failed: {e} — trading blocked")
            return local_positions

    # ── Daily loss tracking ───────────────────────────────────────────────────
    def record_outcome(self, profit_usd: float):
        """Record realized P&L for the daily loss cap."""
        self._daily_realized += profit_usd

    def daily_loss_pct(self) -> float:
        """Current loss as a positive fraction (0.0 if in profit today)."""
        if self._daily_realized >= 0:
            return 0.0
        return abs(self._daily_realized) / self._daily_start

    # ── Misc ──────────────────────────────────────────────────────────────────
    def is_killed(self) -> bool:
        return kill_switch.is_killed()

    def _alert(self, msg: str):
        if self.telegram_fn:
            try:
                self.telegram_fn(msg)
            except Exception as e:
                log.warning(f"Telegram alert failed: {e}")


if __name__ == "__main__":
    # Cannot meaningfully self-test without live credentials.
    # Confirms the module imports and the safety logic is sound (no client).
    logging.basicConfig(level=logging.INFO)
    print("Module import OK. Safety-logic dry checks (no live client):")

    class _Dummy(PolymarketExecutor):
        def __init__(self):
            self.bankroll_cap = 50.0
            self.daily_loss_cap_pct = 0.30
            self.slippage_cap_cents = 0.02
            self.max_trades_per_hour = 30
            self.max_position_pct = 0.05
            self.telegram_fn = None
            self._reconciled = True
            self._daily_start = 50.0
            self._daily_date = datetime.now().date()
            self._daily_realized = 0.0
            self._recent_trades = []
            self.client = "dummy"
            self.signature_type = 1

    d = _Dummy()
    print("  slippage 1c within 2c cap:", d.check_slippage(0.53, 0.54))
    print("  slippage 3c over 2c cap:  ", d.check_slippage(0.53, 0.56))
    print("  position $1 of $50 (2%):  ", d.check_position_size(1.0))
    print("  position $5 of $50 (10%): ", d.check_position_size(5.0))
    d._daily_realized = -20.0
    print("  daily loss 40% vs 30% cap:", d.can_trade())
