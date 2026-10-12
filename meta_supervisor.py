"""
Meta-Supervisor
================
A simple agent that monitors supervisor_agent.py for runtime errors
and automatically patches and restarts it — just like the supervisor
does for the bot.

Kept intentionally simple to minimize its own failure surface.
No regime detection, no trend tracking, no market data.
Just: read log → find errors → fix → restart.

ARCHITECTURE:
  MetaSupervisor (runs every 5 min)
    ├── ErrorScanner   — reads supervisor.log for tracebacks
    ├── CodeExtractor  — pulls exact lines around each error
    ├── FixAgent       — Claude generates the patch
    ├── Validator      — syntax checks the patch
    └── Applier        — applies patch + restarts supervisor

SETUP:
  Run in Terminal 4 (or alongside other processes):
    python meta_supervisor.py

  No config needed — reads env vars from environment.
  Monitors supervisor_agent.py and supervisor.log by default.
"""

import os
import sys
import re
import json
import time
import shutil
import logging
import subprocess
from datetime import datetime, timedelta
from typing import Optional
import anthropic
CLAUDE_MODEL = "claude-haiku-5-5"   # was claude-sonnet-4-20250514 (retired June 15, 2026)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  [META] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("meta_supervisor.log"),
    ],
)
log = logging.getLogger(__name__)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
CHECK_INTERVAL      = 300     # seconds between checks (5 min)
SUPERVISOR_FILE     = "supervisor_agent.py"
SUPERVISOR_LOG      = "supervisor.log"
ERROR_LOOKBACK_MIN  = 7       # scan this many minutes back for errors
MAX_FIXES_PER_HOUR  = 5       # safety cap — if we're fixing more than this, something is very wrong
FIX_HISTORY_FILE    = "meta_fix_history.json"


# =============================================================================
# ERROR SCANNER
# =============================================================================

def scan_for_errors(log_file: str, minutes: float = ERROR_LOOKBACK_MIN) -> list:
    """
    Scan supervisor.log for recent runtime errors.
    Returns list of unique errors with their tracebacks.
    """
    if not os.path.exists(log_file):
        return []

    cutoff  = datetime.now() - timedelta(minutes=minutes)
    errors  = []
    buffer  = []
    in_tb   = False

    try:
        with open(log_file) as f:
            for line in f:
                ts = None
                try:
                    ts = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")
                except Exception:
                    pass

                if "Traceback (most recent call last)" in line:
                    in_tb  = True
                    buffer = [line.rstrip()]
                    continue

                if in_tb:
                    buffer.append(line.rstrip())
                    # Error type line ends the traceback
                    stripped = line.strip()
                    if stripped and not line.startswith(" ") and (
                        "Error" in stripped or "Exception" in stripped
                    ):
                        in_tb = False
                        if ts and ts >= cutoff:
                            errors.append({
                                "type":      stripped.split(":")[0],
                                "message":   stripped,
                                "traceback": "\n".join(buffer),
                                "ts":        ts.isoformat() if ts else "",
                            })
                        buffer = []
                    continue

    except Exception as e:
        log.warning(f"Error scanning log: {e}")

    # Deduplicate by error type + first 80 chars of message
    seen   = set()
    unique = []
    for e in errors:
        key = e["type"] + e["message"][:80]
        if key not in seen:
            seen.add(key)
            unique.append(e)

    return unique


# =============================================================================
# CODE EXTRACTOR
# =============================================================================

def extract_code_for_errors(supervisor_file: str, errors: list) -> str:
    """
    Extract exact code sections around each error's line number.
    Falls back to variable name search if no line numbers in traceback.
    """
    if not os.path.exists(supervisor_file):
        return "Supervisor file not found"

    with open(supervisor_file) as f:
        all_lines = f.readlines()

    sections = []

    for error in errors[:3]:
        tb = error.get("traceback", "")

        # Try line number extraction from traceback
        line_refs = re.findall(
            r'File ".*?supervisor.*?", line (\d+), in (\w+)', tb
        )
        for line_no_str, func_name in line_refs:
            line_no = int(line_no_str)
            start   = max(0, line_no - 15)
            end     = min(len(all_lines), line_no + 15)
            snippet = f"# --- {func_name}() around line {line_no} ---\n"
            snippet += "".join(
                f"{i+1:4d}  {all_lines[i]}" for i in range(start, end)
            )
            sections.append(snippet)

        if sections:
            continue

        # Fallback: search for undefined variable name
        m = re.search(r"name '(\w+)' is not defined", error.get("message", ""))
        if m:
            var_name = m.group(1)
            for i, line in enumerate(all_lines):
                if var_name in line:
                    start = max(0, i - 10)
                    end   = min(len(all_lines), i + 10)
                    snippet = f"# --- found '{var_name}' at line {i+1} ---\n"
                    snippet += "".join(
                        f"{j+1:4d}  {all_lines[j]}" for j in range(start, end)
                    )
                    sections.append(snippet)
                    break

    if not sections:
        # Last resort: return first 60 lines
        return "".join(f"{i+1:4d}  {l}" for i, l in enumerate(all_lines[:60]))

    return "\n\n".join(sections[:4])


# =============================================================================
# FIX AGENT
# =============================================================================

FIX_PROMPT = """You are a Python code repair agent fixing a supervisor script.

The supervisor script (supervisor_agent.py) has a runtime error. Fix it.

ERRORS DETECTED:
{errors}

RELEVANT CODE (exact source with line numbers):
{code}

Rules:
- Fix the specific error shown. Nothing else.
- old_str must be copied EXACTLY from the code above, character for character.
- Include enough context lines (3-5) to make old_str unique in the file.
- For NameError (undefined variable): either define the variable before use,
  or replace the call with equivalent inline code.
- For TypeError/AttributeError: fix the type handling.
- Keep changes minimal — one targeted fix per error.

Respond with valid JSON only:
{{
  "summary": "one sentence describing the fix",
  "changes": [
    {{
      "description": "what this fixes",
      "old_str": "exact source text to find (must be unique in file)",
      "new_str": "replacement text"
    }}
  ]
}}
"""


def run_fix_agent(errors: list, code: str,
                  claude: anthropic.Anthropic) -> Optional[dict]:
    """Ask Claude to generate a fix for the detected errors."""
    error_text = "\n".join(
        f"[{e['ts'][11:19]}] {e['message']}\n{e['traceback']}"
        for e in errors[:2]
    )

    prompt = FIX_PROMPT.format(errors=error_text, code=code)

    try:
        resp = claude.messages.create(
            model      = CLAUDE_MODEL,
            max_tokens = 1500,
            system     = (
                "You are a Python code repair agent. "
                "Respond with valid JSON only. "
                "No prose, no markdown. "
                "Your entire response must start with { and end with }."
            ),
            messages   = [{"role": "user", "content": prompt}],
        )
        raw = next(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        if not raw:
            log.error("Fix agent returned empty response")
            return None
        fix = json.loads(raw)
        log.info(f"Fix generated: {fix.get('summary','')}")
        return fix
    except json.JSONDecodeError as e:
        log.error(f"Fix agent JSON parse failed: {e}")
        return None
    except Exception as e:
        log.error(f"Fix agent failed: {e}")
        return None


# =============================================================================
# VALIDATOR + APPLIER
# =============================================================================

def apply_changes(content: str, changes: list) -> tuple:
    """
    Apply changes to content. Returns (new_content, error_or_None).
    Tries exact match, then regex fallback for config variables.
    """
    for ch in changes:
        old = ch.get("old_str", "")
        new = ch.get("new_str", "")

        if not old:
            return content, "Empty old_str"

        if old in content:
            content = content.replace(old, new)
            continue

        # Regex fallback for simple variable assignments
        var_match = re.match(r'^(\w+)\s*=', old.strip())
        if var_match:
            var_name = var_match.group(1)
            new_val_m = re.match(r'^\w+\s*=\s*(.+?)(\s*#.*)?$', new.strip())
            if new_val_m:
                new_val  = new_val_m.group(1).strip()
                pattern  = rf'^({re.escape(var_name)}\s*=\s*).*$'
                replace  = f'{var_name} = {new_val}   # fixed by meta-supervisor'
                new_content, count = re.subn(
                    pattern, replace, content, flags=re.MULTILINE
                )
                if count > 0:
                    content = new_content
                    continue

        return content, f"Could not apply: '{old[:60]}'"

    return content, None


def validate_and_apply(fix: dict, supervisor_file: str) -> tuple:
    """
    Validate the fix compiles, then apply it.
    Returns (success: bool, message: str)
    """
    if not os.path.exists(supervisor_file):
        return False, "Supervisor file not found"

    with open(supervisor_file) as f:
        original = f.read()

    patched, err = apply_changes(original, fix.get("changes", []))
    if err:
        return False, f"Could not apply patch: {err}"

    # Syntax check
    temp = supervisor_file + ".meta_tmp"
    try:
        with open(temp, "w") as f:
            f.write(patched)
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", temp],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False, f"Syntax error: {result.stderr[:200]}"
    except Exception as e:
        return False, f"Validation error: {e}"
    finally:
        if os.path.exists(temp):
            os.remove(temp)

    # Apply
    backup = supervisor_file + f".meta_backup_{int(time.time())}"
    shutil.copy2(supervisor_file, backup)
    log.info(f"Backup: {backup}")

    try:
        with open(supervisor_file, "w") as f:
            f.write(patched)
        log.info("Patch applied to supervisor_agent.py")

        # Save to fix history
        history = []
        if os.path.exists(FIX_HISTORY_FILE):
            try:
                with open(FIX_HISTORY_FILE) as f:
                    history = json.load(f)
            except Exception:
                pass
        history.append({
            "applied_at": datetime.now().isoformat(),
            "summary":    fix.get("summary", ""),
            "changes":    fix.get("changes", []),
        })
        with open(FIX_HISTORY_FILE, "w") as f:
            json.dump(history[-50:], f, indent=2)

        return True, "Patch applied successfully"

    except Exception as e:
        shutil.copy2(backup, supervisor_file)
        return False, f"Apply failed, restored backup: {e}"


# =============================================================================
# RESTART SUPERVISOR
# =============================================================================

def restart_supervisor(supervisor_file: str):
    """Restart supervisor_agent.py as a background process."""
    try:
        subprocess.run(["pkill", "-f", supervisor_file], capture_output=True)
        time.sleep(2)

        env      = os.environ.copy()
        bot_dir  = os.path.dirname(os.path.abspath(supervisor_file))
        log_path = os.path.join(bot_dir, "supervisor.log")

        proc = subprocess.Popen(
            [sys.executable, supervisor_file, "--bot", "crypto_prediction_bot.py"],
            cwd    = bot_dir,
            env    = env,
            stdout = open(log_path, "a"),
            stderr = subprocess.STDOUT,
        )
        log.info(f"Supervisor restarted (PID {proc.pid})")

    except Exception as e:
        log.error(f"Could not restart supervisor: {e}")


# =============================================================================
# RATE LIMITER
# =============================================================================

def fixes_this_hour() -> int:
    """Count how many fixes were applied in the last hour."""
    if not os.path.exists(FIX_HISTORY_FILE):
        return 0
    try:
        with open(FIX_HISTORY_FILE) as f:
            history = json.load(f)
        cutoff = datetime.now() - timedelta(hours=1)
        return sum(
            1 for h in history
            if datetime.fromisoformat(h["applied_at"]) >= cutoff
        )
    except Exception:
        return 0


# =============================================================================
# MAIN LOOP
# =============================================================================

def run_meta_supervisor(supervisor_file: str, supervisor_log: str):
    if not ANTHROPIC_API_KEY:
        log.error("ANTHROPIC_API_KEY not set. Exiting.")
        sys.exit(1)

    claude      = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    check_count = 0

    log.info("Meta-supervisor started")
    log.info(f"Monitoring: {supervisor_file}")
    log.info(f"Log file:   {supervisor_log}")
    log.info(f"Check interval: {CHECK_INTERVAL}s")

    while True:
        try:
            check_count += 1
            log.info(f"── Meta-check #{check_count} ──────────────────────────────")

            # Scan supervisor log for errors
            errors = scan_for_errors(supervisor_log, minutes=ERROR_LOOKBACK_MIN)

            if not errors:
                log.info("Supervisor healthy — no errors detected")
                time.sleep(CHECK_INTERVAL)
                continue

            log.warning(f"Detected {len(errors)} error(s) in supervisor:")
            for e in errors:
                log.warning(f"  [{e['ts'][11:19]}] {e['message'][:100]}")

            # Safety rate limit
            recent_fixes = fixes_this_hour()
            if recent_fixes >= MAX_FIXES_PER_HOUR:
                log.error(
                    f"Rate limit hit: {recent_fixes} fixes in last hour. "
                    f"Something is very wrong — stopping auto-fix. "
                    f"Manual intervention required."
                )
                time.sleep(CHECK_INTERVAL * 6)  # wait 30 min before trying again
                continue

            # Extract relevant code
            code = extract_code_for_errors(supervisor_file, errors)
            log.info(f"Extracted {len(code)} chars of relevant code")

            # Generate fix
            fix = run_fix_agent(errors, code, claude)
            if not fix:
                log.warning("Fix agent failed to generate patch")
                time.sleep(CHECK_INTERVAL)
                continue

            # Validate and apply
            success, msg = validate_and_apply(fix, supervisor_file)
            log.info(f"Fix result: {msg}")

            if success:
                log.info("Restarting supervisor...")
                restart_supervisor(supervisor_file)
                log.info("Done. Waiting for supervisor to stabilize...")
                time.sleep(15)  # give supervisor time to start before next check
            else:
                log.error(f"Fix failed: {msg}")

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            log.info("Meta-supervisor stopped.")
            break
        except Exception as e:
            log.error(f"Meta-supervisor error: {e}", exc_info=True)
            time.sleep(60)


# =============================================================================
# ENTRY
# =============================================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Meta-Supervisor — monitors supervisor_agent.py")
    parser.add_argument("--supervisor", default=SUPERVISOR_FILE,
                        help="Path to supervisor_agent.py")
    parser.add_argument("--log",        default=SUPERVISOR_LOG,
                        help="Path to supervisor.log")
    parser.add_argument("--history",    action="store_true",
                        help="Print fix history and exit")
    args = parser.parse_args()

    if args.history:
        if os.path.exists(FIX_HISTORY_FILE):
            with open(FIX_HISTORY_FILE) as f:
                for h in json.load(f):
                    print(f"[{h['applied_at']}] {h.get('summary','')}")
        else:
            print("No fix history.")
    else:
        run_meta_supervisor(args.supervisor, args.log)
