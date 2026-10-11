"""
test_one_order.py — Instrumented single $1 test order on Polymarket CLOB V2.

Runs the ENTIRE V2 order path one step at a time, printing exactly what
happens (and the full error + API response) at each stage. Standalone
diagnostic, separate from the bot. Places at most ONE small real order.

V2 changes vs the old test:
  - uses py_clob_client_v2
  - STEP 2 now PROBES signature types (1 and 2): for each, it calls
    update_balance_allowance (the V2 balance-cache sync) and reads the
    balance. Whichever sigtype shows your pUSD is the one your account uses.
  - STEP 5 prints the market's V2 fee parameters via get_clob_market_info.
  - Market order uses the V2 MarketOrderArgs (side as string, FOK,
    user_usdc_balance for fee-aware fill math).

USAGE:
    read -s PK
    export PK
    python3 test_one_order.py
    unset PK

Steps:
  1. Load credentials + init                       [STEP 1]
  2. Signature-type probe + balance sync           [STEP 2]
  3. Find a live crypto market (gamma API)         [STEP 3]
  4. Current price + market fee info               [STEP 4]
  5. Build + sign a $1 V2 market order             [STEP 5]
  6. POST the order (the real money step)          [STEP 6]
  7. Print the full raw response                   [STEP 7]
"""

import os
import json
import traceback

BET_USD   = 1.0     # the test order size
PRICE_CAP = 0.60    # only bet if price <= this (avoid bad test fills)

def line(msg):
    print("\n" + "─"*60)
    print(msg)
    print("─"*60)

def main():
    # ── STEP 1: credentials + imports ───────────────────────────────────
    line("STEP 1: Load credentials and init (V2 client)")
    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import (
            MarketOrderArgs, OrderType, ApiCreds,
            BalanceAllowanceParams, AssetType,
        )
        import requests

        if "PK" not in os.environ:
            print("✗ PK env var not set. Run: read -s PK && export PK")
            return
        d = json.load(open(os.path.expanduser("~/.polymarket_credentials")))
        api_creds = ApiCreds(api_key=d["api_key"], api_secret=d["api_secret"],
                             api_passphrase=d["api_passphrase"])

        def make_client(sigtype):
            c = ClobClient(host="https://clob.polymarket.com", chain_id=137,
                           key=os.environ["PK"], creds=api_creds,
                           signature_type=sigtype, funder=d["funder_address"])
            return c

        probe = make_client(1)
        print(f"✓ V2 client ready. Signer={probe.get_address()}")
        print(f"  Funder in file={d['funder_address']}")
    except Exception as e:
        print(f"✗ STEP 1 FAILED: {type(e).__name__}: {e}")
        traceback.print_exc()
        return

    # ── STEP 2: signature-type probe + balance sync ─────────────────────
    line("STEP 2: Signature-type probe (sync + read balance per type)")
    file_sigtype = d.get("signature_type")
    candidates = []
    if file_sigtype is not None:
        candidates.append(int(file_sigtype))
    for st in (1, 2):
        if st not in candidates:
            candidates.append(st)

    chosen_sigtype = None
    balance_usd = 0.0
    for st in candidates:
        try:
            c = make_client(st)
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL,
                                            signature_type=st)
            sync_err = ""
            try:
                c.update_balance_allowance(params)   # V2 balance-cache sync
            except Exception as e:
                sync_err = f" (sync err: {type(e).__name__} {str(e)[:60]})"
            bal = c.get_balance_allowance(params)
            raw = bal.get("balance") if isinstance(bal, dict) else None
            usd = float(raw) / 1e6 if raw not in (None, "") else 0.0
            allow = bal.get("allowances") if isinstance(bal, dict) else None
            n_set = sum(1 for v in (allow or {}).values() if v not in ("0", 0, None))
            print(f"  sigtype {st}: balance=${usd:.2f} | allowances set: {n_set}{sync_err}")
            if usd > 0 and chosen_sigtype is None:
                chosen_sigtype = st
                balance_usd = usd
        except Exception as e:
            print(f"  sigtype {st}: ✗ {type(e).__name__}: {str(e)[:100]}")

    if chosen_sigtype is None:
        chosen_sigtype = int(file_sigtype) if file_sigtype is not None else 1
        print(f"  ⚠️  No sigtype showed a balance. Proceeding with sigtype "
              f"{chosen_sigtype} anyway — the order attempt itself is the "
              f"authoritative test (balance query may lag).")
    else:
        print(f"  ✓ Using signature_type={chosen_sigtype} (balance visible: "
              f"${balance_usd:.2f})")
        print(f"    → Put \"signature_type\": {chosen_sigtype} in "
              f"~/.polymarket_credentials for the executor.")

    client = make_client(chosen_sigtype)

    # ── STEP 3: find a live market ──────────────────────────────────────
    line("STEP 3: Find a live crypto 5-min market (gamma API)")
    token_id = condition_id = market_q = None
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets",
                         params={"active": "true", "closed": "false",
                                 "limit": "200"},
                         timeout=15).json()
        markets = r if isinstance(r, list) else r.get("data", [])

        def extract(m):
            raw = m.get("clobTokenIds")
            if not raw:
                return None
            ids = json.loads(raw) if isinstance(raw, str) else raw
            return ids[0] if ids else None

        for m in markets:                       # prefer 5-min crypto markets
            if "up or down" in (m.get("question") or "").lower():
                t = extract(m)
                if t:
                    token_id, condition_id = t, m.get("conditionId")
                    market_q = m.get("question")
                    break
        if not token_id:                        # fallback: any active market
            for m in markets:
                t = extract(m)
                if t:
                    token_id, condition_id = t, m.get("conditionId")
                    market_q = m.get("question")
                    break

        if token_id:
            print(f"✓ Market: {market_q[:60]}")
            print(f"  token_id:     {str(token_id)[:40]}...")
            print(f"  condition_id: {condition_id}")
        else:
            print("✗ STEP 3 FAILED: no market with token ids found")
            return
    except Exception as e:
        print(f"✗ STEP 3 FAILED: {type(e).__name__}: {e}")
        traceback.print_exc()
        return

    # ── STEP 4: price + V2 fee info ─────────────────────────────────────
    line("STEP 4: Current price + V2 market fee parameters")
    try:
        resp = client.get_price(token_id=token_id, side="BUY")
        print(f"  Raw price response: {resp}")
        price = float(resp.get("price")) if isinstance(resp, dict) else float(resp)
        print(f"✓ Current ask price: {price}")
        if price > PRICE_CAP:
            print(f"✗ Price {price} above cap {PRICE_CAP} — bad test fill. "
                  f"Stopping before order. (Re-run; markets change.)")
            return
    except Exception as e:
        print(f"✗ STEP 4 FAILED (price): {type(e).__name__}: {e}")
        traceback.print_exc()
        return

    if condition_id:
        try:
            info = client.get_clob_market_info(condition_id)
            print(f"  V2 market info (fees/ticks): {info}")
        except Exception as e:
            print(f"  (market info unavailable: {type(e).__name__} {str(e)[:80]})")

    # ── STEP 5: build + sign the V2 market order ────────────────────────
    line("STEP 5: Build and sign a $1 V2 market order (FOK)")
    try:
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=BET_USD,                       # USD (pUSD) to spend
            side="BUY",                           # V2: plain string
            order_type=OrderType.FOK,
            user_usdc_balance=balance_usd or BET_USD,  # fee-aware fill calc
        )
        signed = client.create_market_order(order_args)
        print(f"✓ Order built and signed. Type: {type(signed).__name__}")
    except Exception as e:
        print(f"✗ STEP 5 FAILED (build/sign): {type(e).__name__}: {e}")
        traceback.print_exc()
        return

    # ── STEP 6: POST the order (REAL MONEY) ─────────────────────────────
    line("STEP 6: POST the order  ⚠️  THIS IS THE REAL ORDER")
    print("  Submitting $1 FOK market order now...")
    try:
        resp = client.post_order(signed, order_type=OrderType.FOK)
        print("✓ post_order returned without exception.")
    except Exception as e:
        print(f"✗ STEP 6 FAILED (post_order): {type(e).__name__}: {e}")
        print("\n  → THIS is the exact error from V2 order submission.")
        print("    balance/allowance error → deposit-wallet approvals issue")
        print("    signature error        → wrong signature_type for account")
        print("    anything else          → now precisely identified")
        traceback.print_exc()
        return

    # ── STEP 7: full raw response ───────────────────────────────────────
    line("STEP 7: Full raw V2 order response")
    try:
        print(json.dumps(resp, indent=2, default=str))
    except TypeError:
        print(repr(resp))
    print("\n  ↑ REAL V2 field names — these finalize the executor's response")
    print("    parsing (the remaining # VERIFY items).")
    print("\n✓✓✓ ORDER PLACED. Check the Polymarket UI to confirm the position.")

if __name__ == "__main__":
    main()
