"""
Autonomous Supervisor Agent
============================
A Claude agent that monitors the trading bot and thinks freely about
what's wrong — just like a human engineer reviewing logs.

Unlike the old rule-based supervisor, this agent:
  - Has NO fixed rules about what problems to look for
  - Reads logs, signals, code config, and trade history
  - Reasons freely about root cause — just like a conversation
  - Uses adaptive log depth: summary for normal cases, raw detail for anomalies
  - Generates and validates patches, asks for Telegram approval

ARCHITECTURE:
  AutonomousSupervisor (runs every 30 min)
    ├── LogSummarizer     — aggregates logs into structured summary
    ├── TimelineBuilder   — chunks into 5-min windows to detect when things changed
    ├── AnomalyDetector   — finds windows that look different from the rest
    ├── ContextBuilder    — assembles summary + timeline + optional raw detail
    ├── FreeThinkAgent    — Claude reasons freely, no fixed rules
    ├── PatchAgent        — Claude generates exact code changes
    ├── ValidatorAgent    — syntax checks the patch
    ├── TelegramNotifier  — sends you the diagnosis + proposed fix
    └── PatchApplier      — applies + restarts on approval

SETUP:
  export ANTHROPIC_API_KEY="sk-ant-..."
  export TELEGRAM_TOKEN="your-bot-token"
  export TELEGRAM_CHAT_ID="your-chat-id"

  python supervisor_agent.py --bot crypto_prediction_bot.py

  Reply YES or NO to Telegram messages to approve/reject patches.
"""

import os
import sys
import json
import time
import logging
import argparse
import requests
import subprocess
import shutil
import re
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional
import anthropic
CLAUDE_MODEL = "claude-haiku-5-5"   # was claude-sonnet-4-20250514 (retired June 15, 2026)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("supervisor.log"),
    ],
)
log = logging.getLogger(__name__)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

CHECK_INTERVAL     = 1800    # seconds between checks (30 min)
NO_TRADE_HOURS     = 1.0     # trigger if no trades for this long
WIN_RATE_THRESHOLD = 0.42    # overall win rate floor
LOSS_STREAK_LIMIT  = 5       # trigger after N consecutive losses
MIN_TRADES_TO_EVAL = 10      # need at least this many resolved trades
MIN_TRADES_PERIOD  = 5       # min trades in 30-min period to evaluate trend
APPROVAL_TIMEOUT   = 3600    # wait up to 1 hour for Telegram reply
PATCH_HISTORY_FILE = "patch_history.json"
ANOMALY_THRESHOLD  = 2.0     # window is anomalous if skip rate is this x avg
TREND_WINDOW       = 3       # consecutive declining periods to trigger revert
REGIME_WINDOW_MIN  = 30      # minutes to measure market regime


# =============================================================================
# TELEGRAM
# =============================================================================

def send_telegram(message: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"\n{'='*60}\n SUPERVISOR:\n{message}\n{'='*60}\n")
        return False
    try:
        # Use plain text — HTML mode causes 400 errors with code snippets
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message[:4000],  # Telegram max message length
            },
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


# Track last seen Telegram update_id to avoid re-reading old messages
_last_update_id = 0


def clear_telegram_backlog():
    """Mark all existing Telegram messages as read on startup."""
    global _last_update_id
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"limit": 100, "timeout": 1},
            timeout=10,
        )
        updates = resp.json().get("result", [])
        if updates:
            _last_update_id = updates[-1]["update_id"]
            # Confirm read by passing offset = last_id + 1
            requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": _last_update_id + 1, "limit": 1, "timeout": 1},
                timeout=10,
            )
            log.info(f"Cleared {len(updates)} old Telegram messages (last_id={_last_update_id})")
    except Exception as e:
        log.warning(f"Could not clear Telegram backlog: {e}")


def check_telegram_reply() -> Optional[str]:
    global _last_update_id
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return None
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": _last_update_id + 1, "limit": 5, "timeout": 5},
            timeout=15,
        )
        updates = resp.json().get("result", [])
        log.debug(f"Telegram poll: {len(updates)} new updates (offset={_last_update_id + 1})")

        for update in updates:
            update_id = update.get("update_id", 0)
            msg       = update.get("message", {})
            chat_id   = str(msg.get("chat", {}).get("id", ""))
            text      = msg.get("text", "").strip().upper()
            msg_time  = msg.get("date", 0)
            age       = int(time.time() - msg_time)

            # Always advance the offset so we don't re-read
            if update_id > _last_update_id:
                _last_update_id = update_id

            log.debug(f"  Update {update_id}: chat_id={chat_id} text='{text}' age={age}s")

            if chat_id == str(TELEGRAM_CHAT_ID) and text in ["YES", "NO"]:
                log.info(f"Telegram reply detected: {text} (age={age}s)")
                return text

    except Exception as e:
        log.warning(f"Telegram poll failed: {e}")
    return None


def wait_for_approval() -> bool:
    deadline = time.time() + APPROVAL_TIMEOUT
    log.info(f"Waiting for Telegram approval (timeout {APPROVAL_TIMEOUT}s)...")
    while time.time() < deadline:
        reply = check_telegram_reply()
        if reply == "YES":
            log.info("Patch APPROVED via Telegram — applying now...")
            send_telegram("Approved — applying patch and restarting bot...")
            return True
        elif reply == "NO":
            log.info("Patch REJECTED via Telegram — skipping")
            send_telegram("Skipped. Bot continues with current settings.")
            return False
        time.sleep(30)
    log.info("Approval timed out — skipping patch")
    send_telegram("No reply — patch skipped.")
    return False


# =============================================================================
# LOG SUMMARIZER  (adaptive depth)
# =============================================================================

SKIP_PATTERNS = {
    "composite_too_weak":    r"Skip: composite .* too weak",
    "rsi_momentum_conflict": r"Skip: RSI .* but momentum",
    "corroborating_too_low": r"Skip: only \d+/5 corroborating",
    "trend_filter":          r"Skip: hourly trend",
    "timing_gate":           r"Timing gate:",
    "15m_crowd_disagrees":   r"Skip: 15-min crowd",
    "signal_disagrees":      r"Skip: signal disagrees",
    "vetoed_by_claude":      r"VETOED by Claude:",
}

SCORE_PATTERNS = {
    "composite": r"composite: ([+-]?\d+\.\d+)",
    "rsi_score": r"RSI=[\d.]+\(([+-][\d.]+)\)",
    "mom_score": r"Mom=[+-]?[\d.]+%\(([+-][\d.]+)\)",
    "vol_score": r"Vol=[\d.]+x\(([+-][\d.]+)\)",
}


def parse_log_line(line: str) -> dict:
    result = {"raw": line.strip(), "ts": None, "type": "other"}
    try:
        result["ts"] = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")
    except Exception:
        pass

    if "[PAPER]" in line:    result["type"] = "bet"
    elif "Scan #" in line:   result["type"] = "scan"
    elif "WIN" in line or "LOSS" in line: result["type"] = "resolved"
    elif "ERROR" in line:    result["type"] = "error"

    for skip_name, pattern in SKIP_PATTERNS.items():
        if re.search(pattern, line):
            result["type"]   = "skip"
            result["reason"] = skip_name
            break

    for score_name, pattern in SCORE_PATTERNS.items():
        m = re.search(pattern, line)
        if m:
            try:
                result[score_name] = float(m.group(1))
            except Exception:
                pass

    return result


def summarize_logs(bot_log_file: str, hours: float = 0.5) -> dict:
    """Aggregate the last N hours into structured summary."""
    if not os.path.exists(bot_log_file):
        return {"error": "log file not found", "lines": []}

    cutoff = datetime.now() - timedelta(hours=hours)
    lines  = []
    try:
        with open(bot_log_file) as f:
            for line in f:
                try:
                    ts = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")
                    if ts >= cutoff:
                        lines.append(parse_log_line(line))
                except Exception:
                    continue
    except Exception as e:
        return {"error": str(e), "lines": []}

    if not lines:
        return {"error": "no recent log lines", "lines": []}

    skip_counts  = defaultdict(int)
    composites   = []
    rsi_scores   = []
    mom_scores   = []
    vol_scores   = []
    scans = bets = errors = 0
    veto_reasons = []

    for l in lines:
        if l["type"] == "scan":    scans  += 1
        if l["type"] == "bet":     bets   += 1
        if l["type"] == "error":   errors += 1
        if l["type"] == "skip":    skip_counts[l.get("reason","unknown")] += 1
        if "composite" in l:  composites.append(abs(l["composite"]))
        if "rsi_score"  in l: rsi_scores.append(l["rsi_score"])
        if "mom_score"  in l: mom_scores.append(l["mom_score"])
        if "vol_score"  in l: vol_scores.append(l["vol_score"])
        if "VETOED by Claude:" in l.get("raw",""):
            veto_reasons.append(
                l["raw"].split("VETOED by Claude:")[-1].strip()[:120]
            )

    avg = lambda lst: round(sum(lst)/len(lst), 4) if lst else 0.0
    dominant = max(skip_counts, key=skip_counts.get) if skip_counts else "none"

    return {
        "period_hours":  hours,
        "scans":         scans,
        "bets_placed":   bets,
        "errors":        errors,
        "skip_counts":   dict(skip_counts),
        "dominant_skip": dominant,
        "avg_composite": avg(composites),
        "avg_rsi_score": avg(rsi_scores),
        "avg_mom_score": avg(mom_scores),
        "avg_vol_score": avg(vol_scores),
        "veto_sample":   veto_reasons[-5:],
        "sample_lines":  [l["raw"] for l in lines[-15:]],
    }


def build_timeline(bot_log_file: str, hours: float = 0.5,
                   chunk_minutes: int = 5) -> list:
    """Split logs into N-minute chunks to detect when things changed."""
    if not os.path.exists(bot_log_file):
        return []

    cutoff = datetime.now() - timedelta(hours=hours)
    chunks = {}

    try:
        with open(bot_log_file) as f:
            for line in f:
                try:
                    ts = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")
                    if ts < cutoff:
                        continue
                except Exception:
                    continue

                bucket = (ts.hour * 60 + ts.minute) // chunk_minutes
                key    = f"{ts.strftime('%H')}:{(bucket * chunk_minutes) % 60:02d}"

                if key not in chunks:
                    chunks[key] = {
                        "window": key, "scans": 0, "bets": 0, "skips": 0,
                        "skip_reasons": defaultdict(int), "composites": [],
                    }

                p = parse_log_line(line)
                if p["type"] == "scan":  chunks[key]["scans"]  += 1
                if p["type"] == "bet":   chunks[key]["bets"]   += 1
                if p["type"] == "skip":
                    chunks[key]["skips"] += 1
                    chunks[key]["skip_reasons"][p.get("reason","?")] += 1
                if "composite" in p:
                    chunks[key]["composites"].append(abs(p["composite"]))

    except Exception as e:
        log.warning(f"Timeline build failed: {e}")
        return []

    timeline = []
    for key in sorted(chunks.keys()):
        c = chunks[key]
        avg_c = round(sum(c["composites"])/len(c["composites"]),4) if c["composites"] else 0
        top   = max(c["skip_reasons"], key=c["skip_reasons"].get) if c["skip_reasons"] else "none"
        timeline.append({
            "window": c["window"], "scans": c["scans"],
            "bets": c["bets"],     "skips": c["skips"],
            "top_skip": top,       "avg_composite": avg_c,
        })
    return timeline


def find_anomaly_window(timeline: list) -> Optional[str]:
    """Find a time window that looks different from the rest."""
    if len(timeline) < 3:
        return None

    # Sudden stop: bets in early windows, none in later
    had_bets = [w for w in timeline if w["bets"] > 0]
    if had_bets:
        last_bet_idx = max(timeline.index(w) for w in had_bets)
        if last_bet_idx < len(timeline) - 2:
            return timeline[last_bet_idx + 1]["window"]

    # Skip rate spike
    total_scans = sum(w["scans"] for w in timeline)
    total_skips = sum(w["skips"] for w in timeline)
    if total_scans == 0:
        return None

    avg_rate = total_skips / total_scans
    for w in timeline:
        if w["scans"] > 0 and avg_rate > 0:
            if (w["skips"] / w["scans"]) > avg_rate * ANOMALY_THRESHOLD:
                return w["window"]

    return None


def get_raw_lines_for_window(bot_log_file: str, window: str,
                              chunk_minutes: int = 5) -> list:
    """Pull raw log lines for a specific time window."""
    try:
        target_h = int(window.split(":")[0])
        target_m = int(window.split(":")[1])
        end_m    = target_m + chunk_minutes
        raw = []
        with open(bot_log_file) as f:
            for line in f:
                try:
                    ts = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")
                    if ts.hour == target_h and target_m <= ts.minute < end_m:
                        raw.append(line.strip())
                except Exception:
                    continue
        return raw
    except Exception:
        return []


def parse_market_snapshots(bot_log_file: str, hours: float = 0.5) -> str:
    """
    Parse MARKET_SNAPSHOT lines from bot log and build a
    30-minute timeline of what Polymarket was actually showing.

    Each snapshot line looks like:
      MARKET_SNAPSHOT | BTC | 5m_yes=0.55 5m=UP | 15m_yes=0.61 15m=UP | signal=DOWN composite=-0.210

    Returns a formatted string showing the full picture:
    - What the crowd was pricing (5m and 15m)
    - What the bot's signal was saying
    - Whether they agreed or disagreed
    - When they agreed (missed bet opportunities)
    """
    if not os.path.exists(bot_log_file):
        return "=== MARKET HISTORY === (log file not found)"

    cutoff   = datetime.now() - timedelta(hours=hours)
    entries  = []

    try:
        with open(bot_log_file) as f:
            for line in f:
                if "MARKET_SNAPSHOT" not in line:
                    continue
                try:
                    ts = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")
                    if ts < cutoff:
                        continue
                except Exception:
                    continue

                # Parse the structured line
                # MARKET_SNAPSHOT | BTC | 5m_yes=0.55 5m=UP | 15m_yes=0.61 15m=UP | signal=DOWN composite=-0.210
                try:
                    parts    = line.split("MARKET_SNAPSHOT |")[1].strip()
                    segments = [s.strip() for s in parts.split("|")]
                    coin     = segments[0].strip()

                    # Parse 5m segment
                    seg_5m   = segments[1] if len(segments) > 1 else ""
                    yes_5m   = float(re.search(r"5m_yes=([\d.]+)", seg_5m).group(1)) if re.search(r"5m_yes=([\d.]+)", seg_5m) else 0.5
                    leans_5m = re.search(r"5m=(\w+)", seg_5m).group(1) if re.search(r"5m=(\w+)", seg_5m) else "?"

                    # Parse 15m segment
                    seg_15m   = segments[2] if len(segments) > 2 else ""
                    yes_15m   = float(re.search(r"15m_yes=([\d.]+)", seg_15m).group(1)) if re.search(r"15m_yes=([\d.]+)", seg_15m) else 0.5
                    leans_15m = re.search(r"15m=(\w+)", seg_15m).group(1) if re.search(r"15m=(\w+)", seg_15m) else "?"

                    # Parse signal segment
                    seg_sig   = segments[3] if len(segments) > 3 else ""
                    sig_dir   = re.search(r"signal=(\w+)", seg_sig).group(1) if re.search(r"signal=(\w+)", seg_sig) else "?"
                    composite = float(re.search(r"composite=([+-]?[\d.]+)", seg_sig).group(1)) if re.search(r"composite=([+-]?[\d.]+)", seg_sig) else 0.0

                    # Determine agreement
                    agree_5m  = (leans_5m  == sig_dir or leans_5m  == "NEUTRAL")
                    agree_15m = (leans_15m == sig_dir or leans_15m == "NEUTRAL")
                    all_agree = agree_5m and agree_15m
                    missed    = all_agree and abs(composite) >= 0.15  # signal was strong enough but still didn't bet

                    entries.append({
                        "ts":        ts,
                        "coin":      coin,
                        "yes_5m":    yes_5m,
                        "leans_5m":  leans_5m,
                        "yes_15m":   yes_15m,
                        "leans_15m": leans_15m,
                        "sig_dir":   sig_dir,
                        "composite": composite,
                        "agree_5m":  agree_5m,
                        "agree_15m": agree_15m,
                        "all_agree": all_agree,
                        "missed":    missed,
                    })
                except Exception:
                    continue

    except Exception as e:
        return f"=== MARKET HISTORY === (parse error: {e})"

    if not entries:
        return "=== MARKET HISTORY === (no MARKET_SNAPSHOT lines found — update bot first)"

    # Aggregate stats
    by_coin = defaultdict(list)
    for e in entries: by_coin[e["coin"]].append(e)

    lines = [f"=== MARKET HISTORY (last {hours*60:.0f} min, {len(entries)} snapshots) ==="]

    for coin, coin_entries in sorted(by_coin.items()):
        total      = len(coin_entries)
        all_agreed = sum(1 for e in coin_entries if e["all_agree"])
        missed_ops = sum(1 for e in coin_entries if e["missed"])
        crowd_up   = sum(1 for e in coin_entries if e["leans_5m"] == "UP")
        crowd_down = sum(1 for e in coin_entries if e["leans_5m"] == "DOWN")
        sig_up     = sum(1 for e in coin_entries if e["sig_dir"] == "UP")
        sig_down   = sum(1 for e in coin_entries if e["sig_dir"] == "DOWN")

        lines.append(f"\n  {coin} ({total} scans):")
        lines.append(f"    5m crowd: UP={crowd_up} DOWN={crowd_down} | "
                     f"Signal: UP={sig_up} DOWN={sig_down}")
        lines.append(f"    3-way agreement: {all_agreed}/{total} scans ({all_agreed/total*100:.0f}%)")
        if missed_ops:
            lines.append(f"    ⚠ MISSED OPPORTUNITIES: {missed_ops} scans where all agreed "
                         f"but no bet was placed (composite was strong enough)")

        # Show a sample of aligned moments
        aligned = [e for e in coin_entries if e["all_agree"]][:5]
        if aligned:
            lines.append(f"    Sample aligned scans:")
            for e in aligned:
                lines.append(
                    f"      {e['ts'].strftime('%H:%M:%S')} "
                    f"5m={e['leans_5m']}({e['yes_5m']:.0%}) "
                    f"15m={e['leans_15m']}({e['yes_15m']:.0%}) "
                    f"signal={e['sig_dir']} composite={e['composite']:+.3f}"
                    + (" ← MISSED" if e["missed"] else "")
                )

    # Overall diagnosis hint
    total_missed = sum(1 for e in entries if e["missed"])
    total_all_agree = sum(1 for e in entries if e["all_agree"])
    lines.append(f"\n  SUMMARY: {total_all_agree}/{len(entries)} scans had full agreement "
                 f"| {total_missed} had strong signals but no bet placed")

    if total_missed > 0:
        lines.append("  → Something is blocking trades even when market + signal agree")
    elif total_all_agree == 0:
        lines.append("  → Market and signal rarely agreed — this explains the lack of trades")
    else:
        lines.append("  → Most agreement scans resulted in bets — bot is working correctly")

    return "\n".join(lines)

COIN_SLUGS_LIVE = {
    "BTC": {"5m": "btc-updown-5m", "15m": "btc-updown-15m"},
    "ETH": {"5m": "eth-updown-5m", "15m": "eth-updown-15m"},
    "SOL": {"5m": "sol-updown-5m", "15m": "sol-updown-15m"},
    "XRP": {"5m": "xrp-updown-5m", "15m": "xrp-updown-15m"},
}


def fetch_polymarket_live_data() -> str:
    """
    Fetch current live Polymarket market data for all tracked coins.
    Returns a formatted string showing:
    - Current 5-min and 15-min market odds for each coin
    - Whether each market is leaning UP or DOWN
    - Volume and liquidity
    This gives the supervisor ground truth about what Polymarket
    was actually showing, not just what the bot's signals said.
    """
    now_ts       = int(time.time())
    window_5m    = now_ts - (now_ts % 300)
    window_15m   = now_ts - (now_ts % 900)

    lines = ["=== LIVE POLYMARKET DATA (current odds right now) ==="]

    for coin, slugs in COIN_SLUGS_LIVE.items():
        coin_lines = []
        for timeframe, slug_base in slugs.items():
            window_ts = window_5m if timeframe == "5m" else window_15m
            slug      = f"{slug_base}-{window_ts}"
            try:
                resp = requests.get(
                    f"{GAMMA_API_URL}/markets",
                    params={"slug": slug},
                    timeout=8,
                )
                resp.raise_for_status()
                data = resp.json()
                m    = data[0] if isinstance(data, list) and data else data

                if not m or not m.get("active"):
                    coin_lines.append(f"  {coin} {timeframe}: market not found (slug={slug})")
                    continue

                # Parse prices
                yes_price = 0.5
                best_ask  = m.get("bestAsk")
                last      = m.get("lastTradePrice")
                if best_ask and float(best_ask) > 0:
                    yes_price = float(best_ask)
                elif last and float(last) > 0:
                    yes_price = float(last)
                else:
                    raw = m.get("outcomePrices","[]")
                    if isinstance(raw, str):
                        prices = json.loads(raw)
                        if prices: yes_price = float(prices[0])

                no_price = round(1 - yes_price, 4)
                crowd_leans = ("UP" if yes_price > 0.52
                               else "DOWN" if yes_price < 0.48
                               else "NEUTRAL")

                volume   = float(m.get("volume24hr", 0))
                secs_left = (window_ts + (300 if timeframe=="5m" else 900)) - now_ts

                coin_lines.append(
                    f"  {coin} {timeframe}: YES={yes_price:.0%} NO={no_price:.0%} "
                    f"leans={crowd_leans} vol24h=${volume:,.0f} "
                    f"window_closes_in={secs_left}s"
                )

            except Exception as e:
                coin_lines.append(f"  {coin} {timeframe}: fetch failed ({e})")

        lines.extend(coin_lines)

    lines.append("")
    lines.append("  Interpretation guide:")
    lines.append("  YES > 52% = crowd betting UP  |  YES < 48% = crowd betting DOWN")
    lines.append("  If crowd leans UP but bot signals DOWN → disagreement causing skips")
    lines.append("  If crowd is NEUTRAL (48-52%) → market uncertain, good time for bot to bet")

    return "\n".join(lines)


def build_context(bot_log_file: str, bot_file: str,
                  trades: list, hours: float = 0.5) -> str:
    """
    Assemble full context for the free-think agent.
    Adaptively adds raw scan detail if an anomaly is detected.
    """
    summary  = summarize_logs(bot_log_file, hours)
    timeline = build_timeline(bot_log_file, hours)
    anomaly  = find_anomaly_window(timeline)

    # Extract bot config lines
    config_lines = []
    if os.path.exists(bot_file):
        capture = False
        with open(bot_file) as f:
            for line in f:
                if "Config" in line or "Signal thresholds" in line:
                    capture = True
                if capture:
                    config_lines.append(line.rstrip())
                if capture and line.strip() == "" and len(config_lines) > 20:
                    break

    resolved = [t for t in trades if t.get("outcome") in ["WIN","LOSS"]]
    wins     = sum(1 for t in resolved if t["outcome"] == "WIN")
    losses   = len(resolved) - wins

    ctx = ["=== PERIOD SUMMARY ==="]
    ctx.append(json.dumps(
        {k: v for k, v in summary.items() if k != "sample_lines"},
        indent=2
    ))

    ctx.append("\n=== 5-MINUTE TIMELINE (when did things change?) ===")
    for w in timeline:
        ctx.append(
            f"  {w['window']}  scans={w['scans']}  bets={w['bets']}  "
            f"skips={w['skips']}  top_skip={w['top_skip']}  "
            f"avg_composite={w['avg_composite']}"
        )

    if anomaly:
        ctx.append(f"\n=== ANOMALY DETECTED at {anomaly} — raw scan detail ===")
        for line in get_raw_lines_for_window(bot_log_file, anomaly)[:60]:
            ctx.append(f"  {line}")

    # 30-minute market history — what Polymarket was actually showing
    ctx.append("")
    try:
        market_history = parse_market_snapshots(bot_log_file, hours)
        # Cap at 3000 chars to avoid blowing context window
        if len(market_history) > 3000:
            market_history = market_history[:3000] + "\n  ... (truncated)"
        ctx.append(market_history)
    except Exception as e:
        ctx.append(f"=== MARKET HISTORY === (error: {e})")

    ctx.append("\n=== LAST 15 LOG LINES ===")
    for line in summary.get("sample_lines", []):
        ctx.append(f"  {line}")

    ctx.append("\n=== BOT CONFIG (tunable parameters) ===")
    for line in config_lines[:40]:
        ctx.append(line)

    ctx.append("\n=== TRADE HISTORY ===")
    ctx.append(f"  Total: {len(trades)}  Resolved: {len(resolved)}  "
               f"Wins: {wins}  Losses: {losses}")
    if resolved:
        ctx.append(f"  Win rate: {wins/len(resolved):.0%}")
        for t in resolved[-3:]:
            ctx.append(f"  {t.get('outcome','?')} | "
                       f"{t.get('question','')[:50]} | "
                       f"${t.get('profit',0):+.2f}")

    # Live Polymarket data — ground truth of what the market was showing
    ctx.append("")
    try:
        polymarket_data = fetch_polymarket_live_data()
        if len(polymarket_data) > 1500:
            polymarket_data = polymarket_data[:1500] + "\n... (truncated)"
        ctx.append(polymarket_data)
    except Exception as e:
        ctx.append(f"=== LIVE POLYMARKET DATA === (fetch failed: {e})")

    return "\n".join(ctx)


# =============================================================================
# FREE-THINK AGENT
# =============================================================================

FREE_THINK_PROMPT = """You are an autonomous trading bot engineer doing a routine 30-minute performance review.

You have full visibility into the bot's recent behavior, market conditions, and performance trend.
Think freely — reason like an engineer who checks in every 30 minutes and adapts the system.

{context}

CURRENT LIVE SIGNALS:
{signals}

TRIGGER FOR THIS CHECK: {trigger_type}
TIME SINCE LAST TRADE: {hours:.1f} hours

IMPORTANT CONTEXT:
- The Claude veto system was recently updated to be more permissive (default approve).
  Do NOT recommend disabling or bypassing the veto — it is already lenient.
  If the veto is still blocking trades, the issue is upstream (signals, thresholds, or logic).
- The volume signal has been returning 0.0x — this is a known issue with the threshold.
  Do NOT recommend disabling volume — recommend lowering the volume threshold instead.
- If the trigger is a runtime_error, focus ENTIRELY on fixing that error.
  Read the full traceback, identify the undefined variable or broken logic,
  and generate a precise fix. Do not suggest config changes for runtime errors —
  only code fixes. Always set fix_needed=true for runtime errors.
- MAX_CROWD_PRICE=0.57 filters out bets where crowd conviction > 57%. This is intentional —
  data shows crowd 65%+ produces only 17% win rate while crowd 50-55% produces 66% win rate.
  Do NOT recommend removing this filter. You may recommend tuning it between 0.54-0.62.
- STRICTLY OFF-LIMITS — never modify these functions under any circumstances:
  * compute_composite() — any change risks introducing undefined variables
  * signal_rsi(), signal_momentum(), signal_vwap(), signal_bollinger(), signal_volume()
  * fetch_ohlcv()
  * resolve_positions()
  If you think one of these needs changing, set fix_needed=false and explain why in root_cause.
- You have access to MARKET REGIME data — use it to recommend regime-appropriate settings.
- You have access to WIN RATE TREND data — use it to detect if recent patches helped or hurt.

Think through this step by step:

Step 1 — What does the MARKET REGIME show?
  Is the market trending, ranging, or volatile?
  Are current bot settings appropriate for this regime?
  (Trending markets need momentum-weighted settings, ranging markets need RSI-weighted)

Step 2 — What does the PERIOD PERFORMANCE and TREND show?
  Is win rate this period better or worse than previous periods?
  Did any recent patch correlate with a performance change?
  Are there missed opportunities (strong signals but no bets)?

Step 3 — What does the MARKET HISTORY show?
  How often did 5m crowd, 15m crowd, and signal agree?
  Were there missed opportunities where all agreed but no bet was placed?

Step 4 — What is the root cause and what should change?
  Be specific. Reference actual numbers.
  If win rate is high and stable — say so and recommend no change.
  If regime changed — recommend regime-appropriate tuning.
  If a recent patch hurt performance — recommend reverting or adjusting it.
  If no trades — find the specific blocker.

Step 5 — Confidence check.
  How confident are you? What evidence supports your recommendation?

IMPORTANT: If win rate is above 60% and stable with regular trades — say fix_needed=false.
Do not over-optimize a working system. Stability beats marginal gains.

Respond with JSON only — no prose, no markdown headers, no text outside the JSON:
{{
  "thinking": "your step-by-step reasoning, 2-3 sentences per step, referencing actual numbers",
  "root_cause": "one clear sentence",
  "market_is_quiet": true,
  "confidence": 0.0,
  "fix_needed": true,
  "recommended_changes": [
    {{
      "what": "exact parameter or logic to change",
      "from": "current value",
      "to":   "new value",
      "why":  "specific evidence from the data"
    }}
  ]
}}

IMPORTANT: Your ENTIRE response must be valid JSON. Start with {{ and end with }}. No text before or after.
"""


def run_free_think_agent(context: str, signals: str,
                         hours: float, trigger_type: str,
                         claude: anthropic.Anthropic) -> Optional[dict]:
    log.info("Running free-think agent...")

    # Truncate context if too large — keep most important sections
    MAX_CONTEXT_CHARS = 12000
    if len(context) > MAX_CONTEXT_CHARS:
        log.warning(f"Context too large ({len(context)} chars) — truncating to {MAX_CONTEXT_CHARS}")
        context = context[:MAX_CONTEXT_CHARS] + "\n... (truncated)"

    prompt = FREE_THINK_PROMPT.format(
        context=context, signals=signals,
        hours=hours, trigger_type=trigger_type,
    )

    log.info(f"Prompt size: {len(prompt)} chars")

    try:
        resp = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2000,
            system="You are a trading bot engineer. You MUST respond with valid JSON only. No prose, no markdown, no explanation outside the JSON object. Your entire response must be parseable by json.loads().",
            messages=[{"role": "user", "content": prompt}],
        )
        raw = next(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        log.info(f"Raw response ({len(raw)} chars): {raw[:200]}")
        raw = raw.replace("```json","").replace("```","").strip()

        if not raw:
            log.error("Free-think agent returned empty response")
            return None

        result = json.loads(raw)
        log.info(f"Root cause: {result.get('root_cause','?')}")
        log.info(f"Confidence: {result.get('confidence',0):.0%} | "
                 f"Fix needed: {result.get('fix_needed',False)}")
        return result
    except json.JSONDecodeError as e:
        log.error(f"Free-think agent JSON parse failed: {e}")
        log.error(f"Raw response was: {raw[:500] if 'raw' in dir() else 'unavailable'}")
        return None
    except Exception as e:
        log.error(f"Free-think agent failed: {e}")
        return None


# =============================================================================
# PATCH AGENT
# =============================================================================

PATCH_PROMPT = """You are a Python code patching agent. Your job is to fix bugs and tune config.

DIAGNOSIS:
{diagnosis}

CURRENT BOT CONFIG (exact Python source lines):
{raw_config}

RELEVANT CODE SECTIONS (exact source — use these for old_str):
{relevant_code}

Generate the minimal patch. Each change needs:
- old_str: EXACT text as it appears in the source above (can be multiple lines)
- new_str: replacement text

The old_str will be found using Python's str.replace() — it must match EXACTLY,
including all spaces, indentation, and newlines.

Respond with JSON:
{{
  "patch_id": "patch_{timestamp}",
  "summary": "one sentence",
  "changes": [
    {{
      "description": "what this does",
      "old_str": "exact source text to find and replace",
      "new_str": "replacement text"
    }}
  ],
  "risk_level": "low|medium|high",
  "expected_improvement": "what metric improves"
}}

Rules:
- For config changes: include the full line with spacing and comments
- For logic changes: include enough context (3-5 lines) to be unique
- Never change function signatures or overall architecture
- low risk = config only, medium = logic tweak, high = structural change
- NEVER modify these functions — patches touching them will be rejected:
  compute_composite, signal_rsi, signal_momentum, signal_vwap,
  signal_bollinger, signal_volume, fetch_ohlcv, resolve_positions
- Only tune config variables or modify match_and_decide / claude_veto
- Never introduce a new variable in a function without defining it first in that scope
"""


def extract_code_around_error(bot_file: str, errors: list) -> str:
    """
    Extract the exact code lines around each error's line number.
    Uses the traceback to find the precise location — much more reliable
    than keyword search for runtime errors.
    """
    if not os.path.exists(bot_file):
        return "Bot file not found"

    with open(bot_file) as f:
        all_lines = f.readlines()

    sections = []
    for error in errors[:3]:
        tb = error.get("traceback", "")

        # Extract line numbers from traceback
        # Format: '  File "bot.py", line 604, in match_and_decide'
        line_refs = re.findall(
            r'File ".*?", line (\d+), in (\w+)', tb
        )

        for line_no_str, func_name in line_refs:
            line_no = int(line_no_str)
            # Extract 20 lines before and after for context
            start = max(0, line_no - 20)
            end   = min(len(all_lines), line_no + 20)
            snippet = f"# --- {func_name}() around line {line_no} ---\n"
            snippet += "".join(
                f"{i+1:4d}  {all_lines[i]}"
                for i in range(start, end)
            )
            sections.append(snippet)

    if not sections:
        # Fallback: return lines around any line mentioning the error variable
        error_var = ""
        for e in errors:
            m = re.search(r"name '(\w+)' is not defined", e.get("message",""))
            if m:
                error_var = m.group(1)
                break

        if error_var and all_lines:
            for i, line in enumerate(all_lines):
                if error_var in line:
                    start = max(0, i - 15)
                    end   = min(len(all_lines), i + 15)
                    snippet = f"# --- found '{error_var}' around line {i+1} ---\n"
                    snippet += "".join(
                        f"{j+1:4d}  {all_lines[j]}"
                        for j in range(start, end)
                    )
                    sections.append(snippet)
                    break

        if not sections:
            # Last resort: return first 60 lines of bot (config + key functions)
            return "".join(f"{i+1:4d}  {l}" for i, l in enumerate(all_lines[:60]))

    return "\n\n".join(sections)
    """
    Extract code sections most likely relevant to the diagnosed problem.
    Searches for functions/blocks mentioned in the diagnosis root_cause.
    Always includes: config section, match_and_decide key blocks.
    """
    if not os.path.exists(bot_file):
        return "Bot file not found"

    with open(bot_file) as f:
        lines = f.readlines()

    root_cause = diagnosis.get("root_cause", "").lower()
    changes    = diagnosis.get("recommended_changes", [])

    # Keywords from diagnosis to search for
    keywords = set()
    for ch in changes:
        what = ch.get("what", "").lower()
        for word in what.split():
            if len(word) > 4:
                keywords.add(word.strip("_()"))
    # Also extract from root cause
    for word in root_cause.split():
        w = word.strip(".,()").lower()
        if len(w) > 5 and w not in {"signal","market","trade","crowd","block"}:
            keywords.add(w)

    # Always include these function names
    always_include = {
        "match_and_decide", "compute_composite", "signal_momentum",
        "signal_rsi", "signal_volume", "run_bot",
        "claude_veto", "VETO_PROMPT", "MIN_COMPOSITE",
        "MIN_CORROBORATING", "REQUIRE_AGREES",
    }
    keywords |= always_include

    # Find relevant line ranges
    sections = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Check if this line is relevant
        line_lower = line.lower()
        if any(kw in line_lower for kw in keywords):
            # Capture surrounding context: 3 lines before, up to 60 lines after
            start = max(0, i - 3)
            end   = min(len(lines), i + 60)
            section_lines = lines[start:end]
            section_text  = f"# --- lines {start+1}-{end} ---\n" + "".join(section_lines)
            sections.append((start, section_text))
            i = end
            continue
        i += 1

    if not sections:
        # Fallback: return first 80 lines (usually contains config + key functions)
        return "".join(lines[:80])

    # Deduplicate overlapping sections and join
    result = []
    last_end = -1
    for start, text in sorted(sections):
        if start > last_end:
            result.append(text)
            last_end = start + text.count("\n")

    return "\n".join(result[:8])  # cap at 8 sections to avoid context overflow


PATCH_CORRECTION_PROMPT = """Your previous patch failed validation with this error:

ERROR: {error}

YOUR FAILED PATCH:
{failed_patch}

THE ACTUAL CODE (exact source — use this for old_str):
{actual_code}

The old_str you provided does not exist in the file. Look at the actual code above
and generate a corrected patch where old_str EXACTLY matches what is in the file.

Copy the old_str character-for-character from the code above. Include full lines with
all indentation, spacing, and comments exactly as they appear.

Respond with the same JSON format:
{{
  "patch_id": "patch_{timestamp}_retry",
  "summary": "one sentence",
  "changes": [
    {{
      "description": "what this does",
      "old_str": "exact source text copied from the code above",
      "new_str": "replacement text"
    }}
  ],
  "risk_level": "low|medium|high",
  "expected_improvement": "what metric improves"
}}
"""


def run_patch_with_self_correction(diagnosis: dict, bot_file: str,
                                    claude: anthropic.Anthropic,
                                    max_retries: int = 3) -> Optional[dict]:
    """
    Generate a patch and self-correct if validation fails.
    On each retry, shows Claude the exact error and actual code so it can fix its mistake.
    """
    patch = run_patch_agent(diagnosis, bot_file, claude)
    if not patch:
        return None

    for attempt in range(max_retries):
        valid, error = validate_patch(patch, bot_file)

        if valid:
            log.info(f"Patch validated on attempt {attempt + 1}")
            return patch

        log.warning(f"Patch validation failed (attempt {attempt + 1}/{max_retries}): {error}")

        if attempt == max_retries - 1:
            log.error(f"Patch failed after {max_retries} attempts — giving up")
            send_telegram(
                f"Patch failed after {max_retries} self-correction attempts.\n"
                f"Last error: {error}\n"
                f"Manual fix may be needed."
            )
            return None

        # Self-correction: show Claude the error + actual code
        log.info(f"Self-correcting patch (attempt {attempt + 2})...")

        # Get exact code using line-number extraction if errors available,
        # otherwise read the full bot file for context
        error_list = diagnosis.get("_errors", [])
        if error_list:
            actual_code = extract_code_around_error(bot_file, [
                {"traceback": tb, "message": tb.split("\n")[-1]} for tb in error_list
            ])
        else:
            # Read whole file — patch agent needs exact text to match
            try:
                with open(bot_file) as f:
                    lines = f.readlines()
                actual_code = "".join(f"{i+1:4d}  {l}" for i, l in enumerate(lines))
                # Cap at reasonable size
                if len(actual_code) > 8000:
                    actual_code = actual_code[:8000] + "\n... (truncated)"
            except Exception:
                actual_code = "Could not read bot file"

        correction_prompt = PATCH_CORRECTION_PROMPT.format(
            error        = error,
            failed_patch = json.dumps(patch, indent=2),
            actual_code  = actual_code,
            timestamp    = int(time.time()),
        )

        try:
            resp = claude.messages.create(
                model      = CLAUDE_MODEL,
                max_tokens = 1500,
                system     = "You are a Python code patching agent. Respond with valid JSON only. No prose, no markdown, no explanation outside the JSON object. Your entire response must start with { and end with } and be parseable by json.loads().",
                messages   = [{"role": "user", "content": correction_prompt}],
            )
            raw = next(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
            raw = raw.replace("```json","").replace("```","").strip()
            corrected = json.loads(raw)
            log.info(f"Self-corrected patch: {corrected.get('summary','')}")
            patch = corrected  # try the corrected patch next iteration

        except Exception as e:
            log.error(f"Self-correction failed: {e}")
            return None

    return None


def run_patch_agent(diagnosis: dict, bot_file: str,
                    claude: anthropic.Anthropic) -> Optional[dict]:
    log.info("Running patch agent...")

    # Extract config values
    raw_config = {}
    config_vars = [
        "MIN_COMPOSITE","MIN_CORROBORATING","REQUIRE_AGREES",
        "MAX_BET_USD","DAILY_LOSS_LIMIT","SCAN_INTERVAL",
        "MIN_MARKET_EDGE","MIN_PAYOUT_ROI","MAX_CROWD_PRICE",
    ]
    if os.path.exists(bot_file):
        with open(bot_file) as f:
            for line in f:
                for var in config_vars:
                    if line.strip().startswith(var):
                        raw_config[var] = line.rstrip()
                        break

    # For runtime errors, use exact line-number-based code extraction
    # For normal patches, extract code sections by keyword matching
    if "_error_code_override" in diagnosis:
        relevant_code = diagnosis["_error_code_override"]
        error_text    = "\n\n".join(diagnosis.get("_errors", []))
        relevant_code = f"=== EXACT TRACEBACKS ===\n{error_text}\n\n=== CODE AT ERROR LOCATION ===\n{relevant_code}"
    else:
        # Keyword-based extraction — find functions mentioned in the diagnosis
        relevant_code = ""
        if os.path.exists(bot_file):
            with open(bot_file) as f:
                all_lines = f.readlines()

            root_cause = diagnosis.get("root_cause", "").lower()
            changes    = diagnosis.get("recommended_changes", [])

            # Build keyword set from diagnosis
            keywords = set()
            for ch in changes:
                for word in ch.get("what","").lower().split():
                    if len(word) > 4:
                        keywords.add(word.strip("_()"))
            for word in root_cause.split():
                w = word.strip(".,()").lower()
                if len(w) > 5:
                    keywords.add(w)

            # Always include these key functions
            keywords |= {
                "match_and_decide", "compute_composite", "claude_veto",
                "veto_prompt", "min_composite", "min_corroborating",
                "require_agrees", "max_crowd_price",
            }

            # Find relevant sections
            sections = []
            i = 0
            while i < len(all_lines):
                if any(kw in all_lines[i].lower() for kw in keywords):
                    start = max(0, i - 3)
                    end   = min(len(all_lines), i + 50)
                    snippet = f"# --- lines {start+1}-{end} ---\n"
                    snippet += "".join(
                        f"{j+1:4d}  {all_lines[j]}"
                        for j in range(start, end)
                    )
                    sections.append((start, snippet))
                    i = end
                    continue
                i += 1

            # Deduplicate and join
            seen_starts = set()
            parts = []
            for start, text in sorted(sections):
                if start not in seen_starts:
                    seen_starts.add(start)
                    parts.append(text)
                if len(parts) >= 6:
                    break

            relevant_code = "\n".join(parts) if parts else "".join(
                f"{i+1:4d}  {l}" for i, l in enumerate(all_lines[:80])
            )

    prompt = PATCH_PROMPT.format(
        diagnosis     = json.dumps(diagnosis, indent=2),
        raw_config    = json.dumps(raw_config, indent=2),
        relevant_code = relevant_code,
        timestamp     = int(time.time()),
    )
    try:
        resp = claude.messages.create(
            model      = CLAUDE_MODEL,
            max_tokens = 2000,
            system     = "You are a Python code patching agent. Respond with valid JSON only. No prose, no markdown, no explanation outside the JSON object. Your entire response must start with { and end with } and be parseable by json.loads().",
            messages   = [{"role": "user", "content": prompt}],
        )
        raw = next(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        raw = raw.replace("```json","").replace("```","").strip()
        if not raw:
            log.error("Patch agent returned empty response")
            return None
        patch = json.loads(raw)
        log.info(f"Patch: {patch.get('summary','')}")
        return patch
    except json.JSONDecodeError as e:
        log.error(f"Patch agent JSON parse failed: {e}")
        log.error(f"Raw was: {raw[:300] if 'raw' in locals() else 'unavailable'}")
        return None
    except Exception as e:
        log.error(f"Patch agent failed: {e}")
        return None


# =============================================================================
# VALIDATOR
# =============================================================================

def _apply_changes_to_content(content: str, changes: list) -> tuple:
    """
    Apply a list of changes to content string.
    Tries exact match first, falls back to regex on variable name.
    Returns (new_content, error_message_or_None).
    """
    for ch in changes:
        old = ch.get("old_str", "")
        new = ch.get("new_str", "")
        if not old:
            return content, "Empty old_str"

        # Try exact match
        if old in content:
            content = content.replace(old, new)
            continue

        # Regex fallback — match variable name, ignore comment differences
        var_match = re.match(r'^(\w+)\s*=', old.strip())
        if var_match:
            var_name = var_match.group(1)
            # Extract new value from new_str
            new_val_match = re.match(r'^\w+\s*=\s*(.+?)(\s*#.*)?$', new.strip())
            if new_val_match:
                new_val = new_val_match.group(2).strip() if new_val_match.group(2) else new_val_match.group(1).strip()
                # Also try group 1 if group 2 is None
                new_val = new_val_match.group(1).strip()
                pattern     = rf'^({re.escape(var_name)}\s*=\s*).*$'
                replacement = f'{var_name}       = {new_val}   # updated by supervisor'
                new_content, count = re.subn(pattern, replacement, content,
                                             flags=re.MULTILINE)
                if count > 0:
                    content = new_content
                    log.info(f"  Regex match applied: {var_name} = {new_val}")
                    continue

        return content, f"Could not apply: '{old[:60]}'"

    return content, None


def validate_patch(patch: dict, bot_file: str) -> tuple:
    # ── Hard check: never allow patching off-limits functions ─────────────────
    OFF_LIMITS = [
        "compute_composite",
        "signal_rsi",
        "signal_momentum",
        "signal_vwap",
        "signal_bollinger",
        "signal_volume",
        "fetch_ohlcv",
        "resolve_positions",
    ]
    for ch in patch.get("changes", []):
        old = ch.get("old_str", "")
        for fn in OFF_LIMITS:
            if fn in old:
                return False, (
                    f"Patch touches off-limits function '{fn}'. "
                    f"This function must not be modified by the supervisor."
                )

    if not os.path.exists(bot_file):
        return False, "Bot file not found"
    with open(bot_file) as f:
        content = f.read()
    patched, err = _apply_changes_to_content(content, patch.get("changes", []))
    if err:
        return False, err
    temp = bot_file + ".tmp"
    try:
        with open(temp, "w") as f: f.write(patched)
        r = subprocess.run([sys.executable, "-m", "py_compile", temp],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return False, f"Syntax error: {r.stderr[:200]}"
        return True, "OK"
    except Exception as e:
        return False, str(e)
    finally:
        if os.path.exists(temp): os.remove(temp)


# =============================================================================
# PATCH APPLIER
# =============================================================================

def apply_patch(patch: dict, bot_file: str) -> bool:
    backup = bot_file + f".backup_{int(time.time())}"
    shutil.copy2(bot_file, backup)
    log.info(f"Backup: {backup}")
    try:
        with open(bot_file) as f:
            content = f.read()
        patched, err = _apply_changes_to_content(content, patch.get("changes", []))
        if err:
            log.error(f"Apply failed: {err}")
            shutil.copy2(backup, bot_file)
            return False
        with open(bot_file, "w") as f:
            f.write(patched)
        log.info("All changes applied successfully")
        save_patch_history(patch, bot_file)
        restart_bot(bot_file)
        return True
    except Exception as e:
        log.error(f"Apply failed: {e}")
        shutil.copy2(backup, bot_file)
        return False


def restart_bot(bot_file: str):
    try:
        subprocess.run(["pkill", "-f", bot_file], capture_output=True)
        time.sleep(2)

        bot_dir  = os.path.dirname(os.path.abspath(bot_file))
        bot_name = os.path.basename(bot_file)

        # Build env with all required vars
        env = os.environ.copy()

        # Restart as background process inheriting current environment
        # This keeps the original terminal clean and preserves env vars
        log_path = os.path.join(bot_dir, "crypto_bot.log")
        proc = subprocess.Popen(
            [sys.executable, bot_name, "--paper"],
            cwd    = bot_dir,
            env    = env,
            stdout = open(log_path, "a"),
            stderr = subprocess.STDOUT,
        )
        log.info(f"Bot restarted as background process (PID {proc.pid})")
        log.info(f"Bot output appending to: {log_path}")

    except Exception as e:
        log.error(f"Restart failed: {e}")
        send_telegram(
            f"Could not restart bot.\n"
            f"Run manually:\n"
            f"python {bot_file} --paper"
        )


def save_patch_history(patch: dict, bot_file: str):
    history = []
    if os.path.exists(PATCH_HISTORY_FILE):
        try:
            with open(PATCH_HISTORY_FILE) as f: history = json.load(f)
        except Exception: pass
    history.append({**patch, "applied_at": datetime.now().isoformat(),
                    "bot_file": bot_file})
    with open(PATCH_HISTORY_FILE,"w") as f: json.dump(history, f, indent=2)


# =============================================================================
# PERFORMANCE ANALYZER
# =============================================================================

def analyze_performance(trades: list) -> dict:
    resolved = [t for t in trades if t.get("outcome") in ["WIN","LOSS"]]
    if len(resolved) < MIN_TRADES_TO_EVAL:
        return {"status": "insufficient_data", "resolved": len(resolved)}
    wins = [t for t in resolved if t["outcome"]=="WIN"]
    losses = [t for t in resolved if t["outcome"]=="LOSS"]
    win_rate = len(wins)/len(resolved)
    loss_streak = 0
    for t in reversed(resolved):
        if t["outcome"]=="LOSS": loss_streak += 1
        else: break
    recent = resolved[-10:]
    recent_wr = sum(1 for t in recent if t["outcome"]=="WIN")/len(recent)
    trigger = None
    if win_rate < WIN_RATE_THRESHOLD:       trigger = f"win_rate_low ({win_rate:.0%})"
    elif loss_streak >= LOSS_STREAK_LIMIT:  trigger = f"loss_streak ({loss_streak})"
    elif recent_wr < 0.30:                  trigger = f"recent_win_rate_low ({recent_wr:.0%})"
    return {
        "status": "evaluated", "trigger": trigger,
        "win_rate": round(win_rate,3), "recent_win_rate": round(recent_wr,3),
        "loss_streak": loss_streak, "total": len(trades),
        "resolved": len(resolved), "wins": len(wins), "losses": len(losses),
        "profit": round(sum(t.get("profit",0) for t in resolved), 2),
    }


def load_trades(trades_file: str) -> list:
    if not os.path.exists(trades_file): return []
    try:
        with open(trades_file) as f: return json.load(f)
    except Exception: return []


# =============================================================================
# GET CURRENT SIGNALS
# =============================================================================

def get_current_signals(bot_file: str) -> str:
    try:
        r = subprocess.run([sys.executable, bot_file, "--signals"],
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip() or "Could not fetch signals"
    except Exception as e:
        return f"Signal fetch failed: {e}"


# =============================================================================
# APPROVAL MESSAGE
# =============================================================================

def send_approval_request(thinking: dict, patch: dict,
                           trigger_type: str) -> bool:
    root_cause = thinking.get("root_cause","Unknown")
    summary    = patch.get("summary","No summary")
    risk       = patch.get("risk_level","unknown")
    expected   = patch.get("expected_improvement","?")
    confidence = thinking.get("confidence",0)
    changes_text = "\n".join(
        f"  - {c['description']}\n    {c.get('from','')} → {c.get('to','')}"
        for c in patch.get("changes",[])
    )
    thinking_text = thinking.get("thinking","")[:500]

    send_telegram(
        f"Autonomous Supervisor Alert\n\n"
        f"Trigger: {trigger_type}\n\n"
        f"Agent's reasoning:\n{thinking_text}\n\n"
        f"Root cause: {root_cause}\n"
        f"Confidence: {confidence:.0%}\n\n"
        f"Proposed fix: {summary}\n\n"
        f"Changes:\n{changes_text}\n\n"
        f"Expected: {expected}\n"
        f"Risk: {risk}\n\n"
        f"Reply YES to apply | NO to skip\n"
        f"(Auto-skips in 1 hour)"
    )
    return wait_for_approval()


# =============================================================================
# TREND TRACKER  — detects declining win rate across periods
# =============================================================================

class TrendTracker:
    """
    Tracks win rate per 30-minute period to detect sustained decline.
    Also correlates performance changes with patches applied.
    """
    def __init__(self):
        self.periods = []   # list of {ts, win_rate, trades, patch_applied}
        self.load()

    def load(self):
        path = "trend_history.json"
        if os.path.exists(path):
            try:
                with open(path) as f:
                    self.periods = json.load(f)
            except Exception:
                self.periods = []

    def save(self):
        with open("trend_history.json", "w") as f:
            json.dump(self.periods[-48:], f, indent=2)  # keep last 24 hours

    def record(self, win_rate: float, trades: int, patch_id: str = None):
        self.periods.append({
            "ts":            datetime.now().isoformat(),
            "win_rate":      round(win_rate, 3),
            "trades":        trades,
            "patch_applied": patch_id,
        })
        self.save()

    def is_declining(self) -> tuple:
        """
        Returns (is_declining: bool, consecutive_drops: int, since_patch: str|None)
        Needs at least TREND_WINDOW periods with enough trades to evaluate.
        """
        valid = [p for p in self.periods[-6:]
                 if p.get("trades", 0) >= MIN_TRADES_PERIOD]
        if len(valid) < TREND_WINDOW:
            return False, 0, None

        recent = valid[-TREND_WINDOW:]
        drops  = sum(1 for i in range(1, len(recent))
                     if recent[i]["win_rate"] < recent[i-1]["win_rate"])

        if drops < TREND_WINDOW - 1:
            return False, drops, None

        # Find which patch correlates with the start of the decline
        decline_start_ts = recent[0]["ts"]
        since_patch = None
        for p in reversed(self.periods):
            if p.get("patch_applied") and p["ts"] <= decline_start_ts:
                since_patch = p["patch_applied"]
                break

        return True, drops, since_patch

    def get_summary(self) -> str:
        if not self.periods:
            return "No period history yet"
        lines = ["Recent 30-min period win rates:"]
        for p in self.periods[-6:]:
            patch_note = f" [patch: {p['patch_applied']}]" if p.get("patch_applied") else ""
            lines.append(f"  {p['ts'][11:16]}  wr={p['win_rate']:.0%}  "
                         f"trades={p['trades']}{patch_note}")
        return "\n".join(lines)


# =============================================================================
# REGIME DETECTOR  — detects market conditions (trending vs ranging)
# =============================================================================

def detect_market_regime(bot_log_file: str, minutes: int = 30) -> dict:
    """
    Detect current market regime from MARKET_SNAPSHOT log lines.
    Returns a structured regime description the agent can use.

    Regimes:
    - trending_up:   crowd consistently UP, composites positive
    - trending_down: crowd consistently DOWN, composites negative
    - ranging:       crowd mixed/neutral, composites weak
    - volatile:      crowd flipping direction frequently
    """
    cutoff  = datetime.now() - timedelta(minutes=minutes)
    snaps   = []

    try:
        with open(bot_log_file) as f:
            for line in f:
                if "MARKET_SNAPSHOT" not in line:
                    continue
                try:
                    ts = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")
                    if ts < cutoff:
                        continue
                except Exception:
                    continue
                try:
                    parts    = line.split("MARKET_SNAPSHOT |")[1].strip()
                    segments = [s.strip() for s in parts.split("|")]
                    coin     = segments[0].strip()
                    seg_5m   = segments[1] if len(segments) > 1 else ""
                    leans_5m = re.search(r"5m=(\w+)", seg_5m)
                    comp_m   = re.search(r"composite=([+-]?[\d.]+)", segments[3] if len(segments) > 3 else "")
                    if leans_5m and comp_m:
                        snaps.append({
                            "coin":      coin,
                            "leans":     leans_5m.group(1),
                            "composite": float(comp_m.group(1)),
                        })
                except Exception:
                    continue
    except Exception:
        pass

    if not snaps:
        return {"regime": "unknown", "description": "No market data available"}

    total     = len(snaps)
    up_pct    = sum(1 for s in snaps if s["leans"] == "UP")   / total
    down_pct  = sum(1 for s in snaps if s["leans"] == "DOWN") / total
    neutral   = sum(1 for s in snaps if s["leans"] == "NEUTRAL") / total
    avg_comp  = sum(abs(s["composite"]) for s in snaps) / total

    # Count direction flips (volatility indicator)
    flips = 0
    prev  = None
    for s in snaps:
        if prev and s["leans"] != prev and s["leans"] != "NEUTRAL":
            flips += 1
        prev = s["leans"]
    flip_rate = flips / total

    if flip_rate > 0.3:
        regime = "volatile"
        desc   = f"Market flipping direction frequently ({flip_rate:.0%} flip rate) — signals unreliable"
    elif up_pct > 0.65:
        regime = "trending_up"
        desc   = f"Strong uptrend ({up_pct:.0%} UP) — momentum signals more reliable than RSI"
    elif down_pct > 0.65:
        regime = "trending_down"
        desc   = f"Strong downtrend ({down_pct:.0%} DOWN) — momentum signals more reliable than RSI"
    elif neutral > 0.5:
        regime = "ranging"
        desc   = f"Ranging market ({neutral:.0%} neutral) — RSI signals more reliable, mean-reversion"
    else:
        regime = "mixed"
        desc   = f"Mixed conditions (UP={up_pct:.0%} DOWN={down_pct:.0%} NEUTRAL={neutral:.0%})"

    return {
        "regime":       regime,
        "description":  desc,
        "up_pct":       round(up_pct, 2),
        "down_pct":     round(down_pct, 2),
        "neutral_pct":  round(neutral, 2),
        "avg_composite": round(avg_comp, 3),
        "flip_rate":    round(flip_rate, 3),
        "sample_size":  total,
    }


# =============================================================================
# PERIOD PERFORMANCE  — win rate for just the last 30 minutes
# =============================================================================

def get_period_performance(trades: list, minutes: int = 30) -> dict:
    """Get win rate and trade stats for just the last N minutes."""
    cutoff  = datetime.now() - timedelta(minutes=minutes)
    recent  = []
    for t in trades:
        try:
            ts = datetime.fromisoformat(t.get("opened_at",""))
            if ts >= cutoff and t.get("outcome") in ["WIN","LOSS"]:
                recent.append(t)
        except Exception:
            continue

    if not recent:
        return {"trades": 0, "win_rate": None, "profit": 0.0}

    wins   = sum(1 for t in recent if t["outcome"] == "WIN")
    profit = sum(t.get("profit", 0) for t in recent)
    return {
        "trades":   len(recent),
        "wins":     wins,
        "losses":   len(recent) - wins,
        "win_rate": round(wins / len(recent), 3),
        "profit":   round(profit, 2),
    }


# =============================================================================
# PATCH REVERSION
# =============================================================================

def revert_last_patch(bot_file: str) -> tuple:
    """
    Revert the most recently applied patch by restoring its backup file.
    Returns (success: bool, message: str)
    """
    history = []
    if os.path.exists(PATCH_HISTORY_FILE):
        try:
            with open(PATCH_HISTORY_FILE) as f:
                history = json.load(f)
        except Exception:
            pass

    if not history:
        return False, "No patch history found"

    last_patch = history[-1]
    patch_ts   = last_patch.get("applied_at", "")
    summary    = last_patch.get("summary", "unknown patch")

    # Find the backup file for this patch
    backup_pattern = bot_file + ".backup_"
    backups = sorted([
        f for f in os.listdir(os.path.dirname(os.path.abspath(bot_file)) or ".")
        if f.startswith(os.path.basename(backup_pattern))
    ])

    if not backups:
        return False, "No backup files found to revert to"

    # Use the most recent backup
    backup_path = os.path.join(
        os.path.dirname(os.path.abspath(bot_file)) or ".",
        backups[-1]
    )

    try:
        shutil.copy2(backup_path, bot_file)
        # Mark as reverted in history
        history[-1]["reverted_at"] = datetime.now().isoformat()
        with open(PATCH_HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
        log.info(f"Reverted to {backup_path}")
        restart_bot(bot_file)
        return True, f"Reverted patch: {summary} (applied {patch_ts})"
    except Exception as e:
        return False, f"Revert failed: {e}"


def detect_runtime_errors(bot_log_file: str, minutes: float = 35) -> list:
    """
    Scan bot log for recent runtime errors (Traceback, ERROR lines).
    Returns list of error dicts with the full traceback text.
    Looks back slightly more than CHECK_INTERVAL to catch errors between checks.
    """
    if not os.path.exists(bot_log_file):
        return []

    cutoff  = datetime.now() - timedelta(minutes=minutes)
    errors  = []
    buffer  = []   # accumulates lines for current traceback
    in_tb   = False

    try:
        with open(bot_log_file) as f:
            for line in f:
                # Parse timestamp if present
                ts = None
                try:
                    ts = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")
                except Exception:
                    pass

                # Detect start of traceback
                if "Traceback (most recent call last)" in line:
                    in_tb  = True
                    buffer = [line.rstrip()]
                    continue

                if in_tb:
                    buffer.append(line.rstrip())
                    # End of traceback — the error type line (e.g. NameError: ...)
                    if line.strip() and not line.startswith(" ") and "Error" in line:
                        in_tb = False
                        # Only include if within time window
                        if ts and ts >= cutoff:
                            errors.append({
                                "type":      line.strip().split(":")[0],
                                "message":   line.strip(),
                                "traceback": "\n".join(buffer),
                                "ts":        ts.isoformat(),
                            })
                        buffer = []
                    continue

                # Also catch standalone ERROR log lines (non-traceback)
                if "  ERROR " in line and ts and ts >= cutoff:
                    if "Traceback" not in line:
                        errors.append({
                            "type":      "LogError",
                            "message":   line.strip(),
                            "traceback": line.strip(),
                            "ts":        ts.isoformat() if ts else "",
                        })

    except Exception as e:
        log.warning(f"Error detector failed: {e}")

    # Deduplicate — same error type within 60s is one error
    seen = set()
    unique = []
    for e in errors:
        key = e["type"] + e["message"][:80]
        if key not in seen:
            seen.add(key)
            unique.append(e)

    return unique


REVIEWER_PROMPT = """You are a senior engineer reviewing a proposed patch to a live trading bot.
Your job is to be SKEPTICAL. Most proposed patches are not worth applying.
Your default is REJECT. Only approve if the patch clearly solves a real problem.

CONTEXT:
  Current bot performance: {current_wr:.0%} win rate over {total_resolved} resolved trades
  Recent period (30 min): {period_wr} win rate over {period_trades} trades
  Patches applied in last 24 hours: {recent_patches}
  Patches reverted in last 24 hours: {recent_reverts}

DIAGNOSIS THE FIRST AGENT MADE:
  Root cause: {root_cause}
  Confidence: {confidence:.0%}
  Reasoning: {thinking}

PROPOSED PATCH:
  Summary: {patch_summary}
  Risk level: {risk_level}
  Expected improvement: {expected}
  Changes:
{changes_text}

RECENT PATCH HISTORY (last 5):
{recent_patch_summaries}

Reject in any of these cases:
1. The patch repeats a similar change made recently (likely thrashing)
2. The current win rate is above 55% — don't break what works
3. The diagnosis says "fix bet direction" or "Rule 3" — known false alarm
4. The change is risky (medium/high) without overwhelming evidence
5. The patch lowers a threshold AGAIN after recent threshold changes
6. The change touches signal computation logic
7. The "evidence" is one bad 30-min window — could be variance, not a real problem

Approve only if ALL of these are true:
1. The diagnosis points to a clear, specific problem
2. The patch directly addresses that problem
3. No similar patch was applied recently
4. The risk is low and the expected improvement is meaningful
5. Win rate is below 55% OR there is a clear bug that needs fixing

Respond with JSON only:
{{
  "approve": true,
  "reasoning": "one clear sentence explaining your decision",
  "confidence": 0.0
}}
"""


def review_patch(thinking: dict, patch: dict,
                 perf: dict, period: dict,
                 patch_history: list,
                 claude: anthropic.Anthropic) -> tuple:
    """
    Use a separate Claude call to review the proposed patch.
    Acts as a skeptical code reviewer — biased toward rejection.
    Returns (approved: bool, reasoning: str)
    """
    log.info("Running reviewer agent...")

    # Recent patch stats
    cutoff = datetime.now() - timedelta(hours=24)
    recent_patches = []
    for p in patch_history:
        try:
            if datetime.fromisoformat(p.get("applied_at","")) >= cutoff:
                recent_patches.append(p)
        except Exception:
            continue

    recent_reverts = sum(1 for p in recent_patches if p.get("reverted_at"))

    recent_summaries = "\n".join(
        f"  - {p.get('summary','')[:120]}{' [REVERTED]' if p.get('reverted_at') else ''}"
        for p in recent_patches[-5:]
    ) or "  (no recent patches)"

    changes_text = "\n".join(
        f"  - {c.get('description','')}\n    {c.get('old_str','')[:80]}\n    -> {c.get('new_str','')[:80]}"
        for c in patch.get("changes", [])
    )

    period_wr   = f"{period['win_rate']:.0%}" if period.get("win_rate") is not None else "no_trades"
    current_wr  = perf.get("win_rate", 0) or 0

    prompt = REVIEWER_PROMPT.format(
        current_wr            = current_wr,
        total_resolved        = perf.get("resolved", 0),
        period_wr             = period_wr,
        period_trades         = period.get("trades", 0),
        recent_patches        = len(recent_patches),
        recent_reverts        = recent_reverts,
        root_cause            = thinking.get("root_cause", "")[:200],
        confidence            = thinking.get("confidence", 0) or 0,
        thinking              = thinking.get("thinking", "")[:600],
        patch_summary         = patch.get("summary", ""),
        risk_level            = patch.get("risk_level", "?"),
        expected              = patch.get("expected_improvement", "?"),
        changes_text          = changes_text,
        recent_patch_summaries = recent_summaries,
    )

    try:
        resp = claude.messages.create(
            model      = CLAUDE_MODEL,
            max_tokens = 600,
            system     = (
                "You are a SKEPTICAL senior engineer reviewing patches. "
                "Default to rejection unless the patch is clearly worth applying. "
                "Respond with valid JSON only — no prose, no markdown."
            ),
            messages   = [{"role": "user", "content": prompt}],
        )
        raw = next(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        raw = raw.replace("```json","").replace("```","").strip()
        result = json.loads(raw)

        approved  = bool(result.get("approve", False))
        reasoning = result.get("reasoning", "no reasoning")

        if approved:
            log.info(f"REVIEWER APPROVED: {reasoning}")
        else:
            log.info(f"REVIEWER REJECTED: {reasoning}")

        return approved, reasoning

    except Exception as e:
        log.error(f"Reviewer agent failed: {e}")
        # On failure, default to REJECT (safer)
        return False, f"Reviewer agent failed: {e}"


# =============================================================================
# MAIN LOOP
# =============================================================================

def run_supervisor(bot_file: str, trades_file: str):
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set. Exiting.")
        sys.exit(1)

    claude   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    bot_log  = "crypto_bot.log"
    tracker  = TrendTracker()

    log.info("Autonomous supervisor started")

    if TELEGRAM_TOKEN:
        clear_telegram_backlog()

    send_telegram(
        f"Autonomous Supervisor Started\n\n"
        f"Monitoring: {bot_file}\n"
        f"Check interval: {CHECK_INTERVAL//60} minutes\n"
        f"Mode: always-on adaptive — analyzes every period\n"
        f"Features: regime detection, trend tracking, auto-revert"
    )

    check_count      = 0
    last_patch_id    = None  # track what patch was most recently applied

    while True:
        try:
            check_count += 1
            log.info(f"── Check #{check_count} ──────────────────────────────")

            trades = load_trades(trades_file)
            perf   = analyze_performance(trades)
            period = get_period_performance(trades, minutes=30)
            regime = detect_market_regime(bot_log, minutes=REGIME_WINDOW_MIN)

            log.info(f"Trades: {len(trades)} | "
                     f"Resolved: {perf.get('resolved',0)} | "
                     f"Win rate: {perf.get('win_rate',0):.0%} | "
                     f"Period({period['trades']} trades): "
                     f"{period['win_rate']:.0%}" if period['win_rate'] is not None
                     else f"Period: no trades")
            log.info(f"Regime: {regime['regime']} — {regime['description']}")

            # ── HIGHEST PRIORITY: runtime error detection ──────────────────────
            # Check for errors before anything else — a crashing bot needs
            # fixing immediately regardless of win rate or trade count.
            runtime_errors = detect_runtime_errors(bot_log, minutes=35)
            if runtime_errors:
                error_summary = "\n".join(
                    f"  [{e['ts'][11:19]}] {e['message'][:120]}"
                    for e in runtime_errors[:3]
                )
                log.error(f"Runtime errors detected:\n{error_summary}")
                send_telegram(
                    f"Bot Runtime Error Detected\n\n"
                    f"{error_summary}\n\n"
                    f"Running auto-fix..."
                )

                # Build focused context with error details
                context = build_context(bot_log, bot_file, trades, hours=0.5)
                context += "\n\n=== RUNTIME ERRORS (fix these first) ===\n"
                for e in runtime_errors[:3]:
                    context += f"\n{e['traceback']}\n"

                signals  = get_current_signals(bot_file)
                trigger_type = f"runtime_error: {runtime_errors[0]['type']}"

                thinking = run_free_think_agent(
                    context, signals, 0, trigger_type, claude
                )
                if thinking:
                    def to_bool(val) -> bool:
                        if isinstance(val, bool): return val
                        if isinstance(val, str):  return val.lower() == "true"
                        return bool(val)

                    if to_bool(thinking.get("fix_needed", True)):
                        # For runtime errors, override the patch agent's code extraction
                        # with exact line-number-based extraction from the traceback
                        error_code = extract_code_around_error(bot_file, runtime_errors)
                        log.info(f"Extracted {len(error_code)} chars of error-relevant code")

                        # Inject exact code into diagnosis so patch agent uses it
                        thinking["_error_code_override"] = error_code
                        thinking["_errors"] = [e["traceback"] for e in runtime_errors[:2]]

                        patch = run_patch_with_self_correction(thinking, bot_file, claude)
                        if patch:
                            success = apply_patch(patch, bot_file)
                            if success:
                                last_patch_id = patch.get("patch_id", f"patch_{int(time.time())}")
                                send_telegram(
                                    f"Runtime error auto-fixed\n\n"
                                    f"Fix: {patch.get('summary','')}\n"
                                    f"Bot restarted."
                                )
                            else:
                                send_telegram("Auto-fix failed. Bot may still be erroring.")
                        else:
                            send_telegram(
                                f"Could not generate fix for error:\n"
                                f"{runtime_errors[0]['message'][:200]}"
                            )

                time.sleep(CHECK_INTERVAL)
                continue

            # ── Record this period in trend tracker ────────────────────────────
            if period["trades"] >= MIN_TRADES_PERIOD and period["win_rate"] is not None:
                tracker.record(
                    win_rate  = period["win_rate"],
                    trades    = period["trades"],
                    patch_id  = last_patch_id,
                )
                last_patch_id = None  # reset after recording
                log.info(tracker.get_summary())

            # ── Check for declining trend → consider reversion ─────────────────
            declining, drops, since_patch = tracker.is_declining()
            if declining and since_patch:
                log.warning(f"Win rate declining {drops} periods in a row since patch {since_patch}")
                log.info("Auto-reverting patch due to sustained decline...")
                success, msg = revert_last_patch(bot_file)
                send_telegram(
                    f"Auto-Revert Applied\n\n"
                    f"Win rate declined {drops} consecutive periods\n"
                    f"Reverted patch: {since_patch}\n"
                    f"Result: {msg}\n\n"
                    f"{tracker.get_summary()}"
                )
                log.info(f"Revert result: {msg}")
                time.sleep(CHECK_INTERVAL)
                continue

            # ── Always build context and analyze ──────────────────────────────
            # No longer conditional — we analyze every period
            hours_since = NO_TRADE_HOURS + 1
            if trades:
                try:
                    last_ts     = datetime.fromisoformat(trades[-1]["opened_at"])
                    hours_since = (datetime.now() - last_ts).total_seconds() / 3600
                except Exception:
                    pass

            # Determine trigger type for context
            no_trade     = hours_since >= NO_TRADE_HOURS
            perf_trigger = perf.get("trigger") if perf.get("status") == "evaluated" else None

            if no_trade:
                trigger_type = f"no_trades ({hours_since:.1f}h)"
            elif perf_trigger:
                trigger_type = perf_trigger
            elif period["win_rate"] is not None and period["win_rate"] < 0.45:
                trigger_type = f"period_win_rate_low ({period['win_rate']:.0%} this period)"
            elif period["win_rate"] is not None and period["win_rate"] > 0.70:
                trigger_type = f"high_performance ({period['win_rate']:.0%}) — check for optimization opportunity"
            else:
                wr_str = f"{period['win_rate']:.0%}" if period["win_rate"] is not None else "no_trades"
                trigger_type = f"routine_check (period_wr={wr_str})"

            log.info(f"Trigger: {trigger_type}")

            # Build context with regime info
            log.info("Building adaptive context...")
            context = build_context(bot_log, bot_file, trades, hours=0.5)

            # Append regime and trend data to context
            context += f"\n\n=== MARKET REGIME (last {REGIME_WINDOW_MIN} min) ===\n"
            context += json.dumps(regime, indent=2)
            context += f"\n\n=== PERIOD PERFORMANCE (last 30 min) ===\n"
            context += json.dumps(period, indent=2)
            context += f"\n\n=== WIN RATE TREND (recent periods) ===\n"
            context += tracker.get_summary()

            signals  = get_current_signals(bot_file)

            # ── Free-think agent ───────────────────────────────────────────────
            thinking = run_free_think_agent(
                context, signals, hours_since, trigger_type, claude
            )
            if not thinking:
                log.warning("Free-think agent failed")
                time.sleep(CHECK_INTERVAL)
                continue

            def to_bool(val) -> bool:
                if isinstance(val, bool): return val
                if isinstance(val, str):  return val.lower() == "true"
                return bool(val)

            fix_needed   = to_bool(thinking.get("fix_needed", False))
            market_quiet = to_bool(thinking.get("market_is_quiet", False))

            log.info(f"fix_needed={fix_needed} | market_is_quiet={market_quiet}")

            if not fix_needed:
                log.info(f"No fix needed: {thinking.get('root_cause','?')}")
                # For routine checks with no fix, just log — don't send Telegram
                # Only send Telegram if something notable happened
                if trigger_type not in ("routine_check",) or \
                   (period["win_rate"] is not None and period["win_rate"] > 0.70):
                    send_telegram(
                        f"Check #{check_count} — {trigger_type}\n\n"
                        f"Regime: {regime['regime']}\n"
                        f"Period: {period['trades']} trades @ "
                        f"{period.get('win_rate',0):.0%} win rate\n\n"
                        f"Conclusion: {thinking.get('root_cause','?')}"
                    )
                time.sleep(CHECK_INTERVAL)
                continue

            # ── Generate patch ─────────────────────────────────────────────────
            patch = run_patch_with_self_correction(thinking, bot_file, claude)
            if not patch:
                log.warning("Patch generation failed after self-correction attempts")
                time.sleep(CHECK_INTERVAL)
                continue

            # ── Reviewer Claude evaluates the patch ────────────────────────────
            # Loads patch history for context
            patch_history = []
            if os.path.exists(PATCH_HISTORY_FILE):
                try:
                    with open(PATCH_HISTORY_FILE) as f:
                        patch_history = json.load(f)
                except Exception:
                    pass

            approved, reasoning = review_patch(
                thinking, patch, perf, period, patch_history, claude
            )

            if not approved:
                log.info(f"Patch rejected by reviewer — skipping")
                send_telegram(
                    f"Patch rejected by reviewer\n\n"
                    f"Trigger: {trigger_type}\n"
                    f"Proposed: {patch.get('summary','')}\n"
                    f"Reviewer said: {reasoning}\n\n"
                    f"No changes applied. Bot continues unchanged."
                )
                time.sleep(CHECK_INTERVAL)
                continue

            # Apply patch — reviewer approved
            log.info("Reviewer approved — applying patch...")
            success = apply_patch(patch, bot_file)
            if success:
                last_patch_id = patch.get("patch_id", f"patch_{int(time.time())}")
                send_telegram(
                    f"Patch applied (reviewer approved)\n\n"
                    f"Trigger: {trigger_type}\n"
                    f"Fix: {patch.get('summary','')}\n"
                    f"Root cause: {thinking.get('root_cause','')}\n"
                    f"Reviewer: {reasoning}\n\n"
                    f"Bot restarted. Monitoring for impact..."
                )
            else:
                send_telegram(f"Auto-patch FAILED. Original restored.\n{patch.get('summary','')}")

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            log.info("Supervisor stopped.")
            send_telegram("Autonomous supervisor stopped manually.")
            break
        except Exception as e:
            log.error(f"Error: {e}", exc_info=True)
            time.sleep(60)


# =============================================================================
# ENTRY
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Autonomous Supervisor Agent")
    parser.add_argument("--bot",     default="crypto_prediction_bot.py")
    parser.add_argument("--trades",  default="crypto_trades.json")
    parser.add_argument("--history", action="store_true",
                        help="Print patch history and exit")
    parser.add_argument("--status",  action="store_true",
                        help="Print current performance metrics")
    parser.add_argument("--context", action="store_true",
                        help="Print what the agent would see (debug)")
    args = parser.parse_args()

    if args.history:
        if os.path.exists(PATCH_HISTORY_FILE):
            with open(PATCH_HISTORY_FILE) as f:
                for p in json.load(f):
                    print(f"[{p['applied_at']}] {p.get('summary','')}")
        else:
            print("No patch history.")

    elif args.status:
        trades = load_trades(args.trades)
        print(json.dumps(analyze_performance(trades), indent=2))

    elif args.context:
        # Debug: print exactly what the agent would see
        trades  = load_trades(args.trades)
        context = build_context("crypto_bot.log", args.bot, trades, hours=0.5)
        signals = get_current_signals(args.bot)
        print("=== CONTEXT ===\n" + context)
        print("\n=== SIGNALS ===\n" + signals)

    else:
        run_supervisor(bot_file=args.bot, trades_file=args.trades)
