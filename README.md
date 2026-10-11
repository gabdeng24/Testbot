# Polybot

Polymarket 5-minute crypto (BTC/ETH/SOL/XRP) up/down prediction bot with a Claude veto layer, cost-aware paper trading, and a live execution layer for Polymarket CLOB V2.

## Files

| File | Purpose |
|---|---|
| `crypto_bot_market.py` | Main bot. Paper mode by default; `--live --bankroll N` routes bets through the executor |
| `crypto_bot_limit.py` | Limit-order (maker) paper variant |
| `polymarket_executor.py` | Live order placement on CLOB V2 (`py-clob-client-v2`, signature type 3) with safety rails |
| `polymarket_fees.py` | Polymarket taker fee model |
| `kill_switch.py` | Emergency stop (`python3 kill_switch.py on/off/status`) |
| `tail_log.py` | Terminal dashboard (`--prefix market|limit|live`) |
| `test_one_order.py` | Instrumented single $1 live test order |
| `sim_report.py` | Compares sim variants and the live run side by side |
| `supervisor_agent.py`, `meta_supervisor.py` | Claude-driven supervisor/reviewer agents |
| `crypto_prediction_bot.py`, `polymarket_bot.py` | Earlier bot versions |
| `live_execution_architecture.md` | Live execution design doc |

## Realistic paper trading (`--sim`)

Plain `--paper` assumes every bet fills at the price the bot decided on. Live trading showed that's wrong: by the time an order is placed, the real price has usually moved. `--sim` places no orders but reads Polymarket's real public order book at the moment the live bot would, and applies the live executor's rules.

```bash
python crypto_bot_market.py --sim --tag sim_live                          # replicates live behaviour
python crypto_bot_market.py --sim --tag sim_sym  --slip-down 0.02         # block price drops too
python crypto_bot_market.py --sim --tag sim_clob --price-source clob      # judge bets at the real price
python crypto_bot_market.py --sim --tag sim_floor --floor 0.30            # skip fills below 30c
python3 sim_report.py                                                     # compare all variants
```

## Secrets

Credentials are read from `~/.polymarket_credentials` and `~/.telegram_credentials` (chmod 600), outside the repo. Never commit them.

## Status

Live results over ~82 trades landed near breakeven; see the analysis notes in the project history before trading real money.
