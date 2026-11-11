"""Meta Ads Kill Switch

Auto-pause any ACTIVE ad, ad set, or campaign that hits your spend thresholds
with zero results. Two rules run on every execution:

  Lifetime rule , anything with $50+ lifetime spend and 0 results.
  Daily rule    , campaigns with $40+ spend TODAY and 0 results today.

Both thresholds and all other settings come from config.json (falls back to
defaults if the file is missing).

Usage:
  python kill_switch.py               # dry run, prints what would be paused
  python kill_switch.py --apply       # pauses matching items and logs them
  python kill_switch.py --notify-email
  python kill_switch.py --notify-webhook https://hooks.example.com/xyz
  python kill_switch.py --apply --notify-email --notify-webhook <url>

Environment variables (set in .env or export):
  META_ACCESS_TOKEN  , your Meta System User token
  META_AD_ACCOUNT_ID , your ad account id, e.g. act_XXXXXXXXXXXXXXXX

The script reads .env automatically via python-dotenv if it is installed.
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Optional dotenv loading, silent if python-dotenv is not installed
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Default config, overridden by config.json when present
# ---------------------------------------------------------------------------
_DEFAULTS: dict[str, Any] = {
    "lifetime_spend_limit": 50.0,
    "daily_spend_limit": 40.0,
    "levels": ["campaign", "adset", "ad"],
    # Action types that count as a result.
    # NOTE on lead double-counting: Meta's "lead" action type adds instant-form
    # leads AND pixel Lead events together. Use "onsite_conversion.lead_grouped"
    # for instant-form leads only. Add "offsite_conversion.fb_pixel_lead" only
    # if your pixel fires a Lead event AND you want that to count as a result.
    # See README for the full explanation.
    "result_action_types": [
        "purchase",
        "offsite_conversion.fb_pixel_purchase",
        "onsite_conversion.lead_grouped"
        # "offsite_conversion.fb_pixel_custom:OrderFormPurchase",   # custom event example
        # "offsite_conversion.fb_pixel_lead",   # opt-in: see lead double-count note
        # "schedule",
        # "offsite_conversion.fb_pixel_schedule"
    ],
    # Items with an id or a name substring in this list are never paused.
    "protect": [],
    # Minimum age in hours before an item can be killed. Brand-new ads spend
    # briefly in learning, give them a few hours before the rule can fire.
    "min_age_hours": 0
}

_API_VERSION = "v21.0"
_LOG_FILE = Path("paused.jsonl")


def load_config(path: str = "config.json") -> dict[str, Any]:
    cfg = dict(_DEFAULTS)
    p = Path(path)
    if p.exists():
        try:
            with open(p, encoding="utf-8") as f:
                override = json.load(f)
            cfg.update(override)
        except Exception as exc:
            print(f"[warn] could not read {path}: {exc}, using defaults", file=sys.stderr)
    return cfg


# ---------------------------------------------------------------------------
# Meta Graph API helpers
# ---------------------------------------------------------------------------

def _api_call(path: str, token: str, method: str = "GET", **params: Any) -> dict:
    params["access_token"] = token
    url = f"https://graph.facebook.com/{_API_VERSION}/{path}"
    for attempt in range(6):
        try:
            if method == "GET":
                full_url = url + "?" + urllib.parse.urlencode(params)
                resp = urllib.request.urlopen(full_url, timeout=60)
            else:
                data = urllib.parse.urlencode(params).encode()
                req = urllib.request.Request(url, data=data, method=method)
                resp = urllib.request.urlopen(req, timeout=60)
            return json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                body: dict = json.load(exc)
            except Exception:
                body = {}
            error_code = (body.get("error") or {}).get("code")
            if error_code in (4, 17, 80004):
                # Rate limited, back off and retry
                wait = 60 * (attempt + 1)
                print(f"[rate limit] waiting {wait}s (code {error_code})…", file=sys.stderr)
                time.sleep(wait)
                continue
            return body
        except Exception as exc:
            print(f"[api error] {exc}", file=sys.stderr)
            time.sleep(10)
    return {}


def _level_endpoint(level: str) -> str:
    return {"campaign": "campaigns", "adset": "adsets", "ad": "ads"}[level]


def fetch_active_items(account_id: str, token: str, level: str) -> list[dict]:
    """Return all ACTIVE items at this level with spend and actions fields."""
    endpoint = _level_endpoint(level)
    fields = (
        "id,name,created_time,"
        f"{'objective' if level == 'campaign' else 'campaign{objective}'},"
        "insights.date_preset(maximum){spend,actions},"
        "insights.date_preset(today).as(today){spend,actions}"
    )
    out: list[dict] = []
    after: str | None = None
    while True:
        params: dict[str, Any] = {
            "fields": fields,
            "limit": 100,
            "effective_status": '["ACTIVE"]',
        }
        if after:
            params["after"] = after
        result = _api_call(f"{account_id}/{endpoint}", token, **params)
        out.extend(result.get("data", []))
        paging = result.get("paging", {})
        after = paging.get("cursors", {}).get("after") if paging.get("next") else None
        if not after:
            break
    return out


# ---------------------------------------------------------------------------
# Decision logic, pure functions, easy to unit-test
# ---------------------------------------------------------------------------

def extract_insights(item: dict, preset_key: str) -> tuple[float, list[dict]]:
    """Return (spend, actions) from an insights block."""
    block = item.get(preset_key) or {}
    data = block.get("data", [{}])
    row = data[0] if data else {}
    spend = float(row.get("spend") or 0)
    actions = row.get("actions") or []
    return spend, actions


def sum_results(actions: list[dict], result_types: list[str]) -> float:
    """Sum action values whose type is in result_types.

    Supports both plain types ("purchase") and scoped custom events
    ("offsite_conversion.fb_pixel_custom:OrderFormPurchase").
    """
    total = 0.0
    for a in actions:
        atype = a.get("action_type", "")
        avalue = float(a.get("value") or 0)
        for rt in result_types:
            if ":" in rt:
                # Custom event match: type must equal left side, name must match right side
                lhs, rhs = rt.split(":", 1)
                if atype == lhs and a.get("action_target_id", "") == rhs:
                    total += avalue
            else:
                if atype == rt:
                    total += avalue
    return total


def item_age_hours(item: dict) -> float:
    """Return how many hours old this item is based on created_time."""
    created_str = item.get("created_time")
    if not created_str:
        return float("inf")
    try:
        # Meta returns ISO 8601 with timezone offset
        from datetime import timezone as tz
        # Python 3.11+ supports fromisoformat with offsets; older versions need manual parse
        try:
            created = datetime.fromisoformat(created_str)
        except ValueError:
            # Strip timezone suffix and treat as UTC
            created = datetime.fromisoformat(created_str[:19]).replace(tzinfo=timezone.utc)
        now = datetime.now(tz=created.tzinfo or timezone.utc)
        return (now - created).total_seconds() / 3600
    except Exception:
        return float("inf")


def is_protected(item: dict, protect: list[str]) -> bool:
    """Return True if the item id or name matches a protect list entry."""
    item_id = item.get("id", "")
    name = item.get("name", "").lower()
    for entry in protect:
        entry_s = str(entry).lower()
        if entry_s == item_id or entry_s in name:
            return True
    return False


def evaluate_item(
    item: dict,
    level: str,
    result_action_types: list[str],
    lifetime_limit: float,
    daily_limit: float,
    min_age_hours: float,
    protect: list[str],
) -> dict:
    """Evaluate one item and return a verdict dict.

    Returns:
        {
            "id": str,
            "level": str,
            "name": str,
            "lifetime_spend": float,
            "lifetime_results": float,
            "daily_spend": float,
            "daily_results": float,
            "action": "pause" | "keep" | "skip",
            "reason": str,
        }
    """
    item_id = item.get("id", "")
    name = item.get("name", "")

    # Protected items are never touched
    if is_protected(item, protect):
        return _verdict(item_id, level, name, 0, 0, 0, 0, "skip", "protected")

    # Min-age guard
    age = item_age_hours(item)
    if age < min_age_hours:
        return _verdict(item_id, level, name, 0, 0, 0, 0, "skip",
                        f"too new ({age:.1f}h < {min_age_hours}h minimum)")

    lifetime_spend, lifetime_actions = extract_insights(item, "insights")
    daily_spend, daily_actions = extract_insights(item, "today")

    lifetime_results = sum_results(lifetime_actions, result_action_types)
    daily_results = sum_results(daily_actions, result_action_types)

    # Lifetime rule
    if lifetime_spend >= lifetime_limit and lifetime_results == 0:
        return _verdict(item_id, level, name, lifetime_spend, lifetime_results,
                        daily_spend, daily_results,
                        "pause",
                        f"${lifetime_spend:.2f} lifetime spend, 0 results (limit ${lifetime_limit:.0f})")

    # Daily rule, campaigns only
    if level == "campaign" and daily_spend >= daily_limit and daily_results == 0:
        return _verdict(item_id, level, name, lifetime_spend, lifetime_results,
                        daily_spend, daily_results,
                        "pause",
                        f"${daily_spend:.2f} spent today, 0 results (daily limit ${daily_limit:.0f})")

    return _verdict(item_id, level, name, lifetime_spend, lifetime_results,
                    daily_spend, daily_results, "keep", "")


def _verdict(
    item_id: str, level: str, name: str,
    ls: float, lr: float, ds: float, dr: float,
    action: str, reason: str,
) -> dict:
    return {
        "id": item_id, "level": level, "name": name,
        "lifetime_spend": ls, "lifetime_results": lr,
        "daily_spend": ds, "daily_results": dr,
        "action": action, "reason": reason,
    }


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_COL_WIDTHS = (10, 36, 10, 10, 8)


def _truncate(s: str, width: int) -> str:
    return s if len(s) <= width else s[: width - 1] + "…"


def print_table(verdicts: list[dict]) -> None:
    header = ("Level", "Name", "Spend $", "Results", "Verdict")
    divider = "  ".join("-" * w for w in _COL_WIDTHS)
    row_fmt = "  ".join(f"{{:<{w}}}" for w in _COL_WIDTHS)
    print()
    print(row_fmt.format(*header))
    print(divider)
    for v in verdicts:
        level = v["level"]
        name = _truncate(v["name"], _COL_WIDTHS[1])
        spend = f"{v['lifetime_spend']:.2f}" if v["action"] != "skip" else "-"
        results = str(int(v["lifetime_results"])) if v["action"] != "skip" else "-"
        verdict_label = v["action"].upper()
        if v["action"] == "pause":
            verdict_label = "PAUSE"
        print(row_fmt.format(level, name, spend, results, verdict_label))
    print()


def log_pause(verdict: dict, applied: bool) -> None:
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "id": verdict["id"],
        "level": verdict["level"],
        "name": verdict["name"],
        "lifetime_spend": verdict["lifetime_spend"],
        "lifetime_results": verdict["lifetime_results"],
        "daily_spend": verdict["daily_spend"],
        "daily_results": verdict["daily_results"],
        "reason": verdict["reason"],
        "applied": applied,
    }
    with open(_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def notify_email(paused_verdicts: list[dict], dry_run: bool) -> None:
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASSWORD", "")
    to_addr = os.getenv("NOTIFY_EMAIL", user)
    if not user or not password:
        print("[notify-email] SMTP_USER or SMTP_PASSWORD not set, skipping email", file=sys.stderr)
        return
    action_word = "Would pause" if dry_run else "Paused"
    subject = f"[Kill Switch] {action_word} {len(paused_verdicts)} item(s)"
    lines = [f"{action_word} {len(paused_verdicts)} item(s):\n"]
    for v in paused_verdicts:
        lines.append(f"  {v['level']:10}  {v['name'][:40]:<40}  {v['reason']}")
    body = "\n".join(lines)
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, port) as s:
            s.starttls()
            s.login(user, password)
            s.send_message(msg)
        print(f"[notify-email] sent to {to_addr}")
    except Exception as exc:
        print(f"[notify-email] failed: {exc}", file=sys.stderr)


def notify_webhook(url: str, paused_verdicts: list[dict], dry_run: bool) -> None:
    action_word = "Would pause" if dry_run else "Paused"
    text_lines = [f":warning: *Kill Switch*, {action_word} {len(paused_verdicts)} item(s)"]
    for v in paused_verdicts:
        text_lines.append(f"• `{v['level']}` {v['name'][:40]}, {v['reason']}")
    payload = json.dumps({"text": "\n".join(text_lines)}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        print(f"[notify-webhook] sent to {url}")
    except Exception as exc:
        print(f"[notify-webhook] failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Meta Ads Kill Switch, pause ads that spend without results.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--apply", action="store_true",
                        help="Actually pause matching items. Without this flag the script is a dry run.")
    parser.add_argument("--config", default="config.json",
                        help="Path to config.json (default: config.json).")
    parser.add_argument("--notify-email", action="store_true",
                        help="Send a summary email via SMTP env vars when items are found.")
    parser.add_argument("--notify-webhook", metavar="URL",
                        help="POST a summary to this webhook URL (Slack/Discord/Kenji compatible).")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)

    token = os.getenv("META_ACCESS_TOKEN", "")
    account_id = os.getenv("META_AD_ACCOUNT_ID", "")
    if not token or not account_id:
        print("ERROR: META_ACCESS_TOKEN and META_AD_ACCOUNT_ID must be set.", file=sys.stderr)
        print("Copy .env.example to .env and fill in your values.", file=sys.stderr)
        sys.exit(1)

    lifetime_limit: float = float(cfg["lifetime_spend_limit"])
    daily_limit: float = float(cfg["daily_spend_limit"])
    levels: list[str] = cfg["levels"]
    result_types: list[str] = cfg["result_action_types"]
    protect: list[str] = cfg.get("protect", [])
    min_age: float = float(cfg.get("min_age_hours", 0))

    dry_run = not args.apply
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    mode_label = "DRY RUN" if dry_run else "APPLY"
    print(f"\n{stamp}  Meta Ads Kill Switch  [{mode_label}]")
    print(f"Limits: lifetime ${lifetime_limit:.0f}  |  daily ${daily_limit:.0f}  |  levels: {', '.join(levels)}")
    if protect:
        print(f"Protected: {protect}")

    all_verdicts: list[dict] = []
    for level in levels:
        try:
            items = fetch_active_items(account_id, token, level)
        except Exception as exc:
            print(f"[warn] could not fetch {level}s: {exc}", file=sys.stderr)
            items = []
        for item in items:
            v = evaluate_item(item, level, result_types, lifetime_limit,
                              daily_limit, min_age, protect)
            all_verdicts.append(v)

    pause_verdicts = [v for v in all_verdicts if v["action"] == "pause"]
    print_table(all_verdicts)

    if not pause_verdicts:
        print(f"{stamp}  nothing over the thresholds, no action needed.")
        return

    print(f"{len(pause_verdicts)} item(s) matched the kill rule:")
    for v in pause_verdicts:
        label = "would pause" if dry_run else "pausing"
        print(f"  {label}: [{v['level']}] {v['name']} ,  {v['reason']}")

    for v in pause_verdicts:
        applied_this = False
        if not dry_run:
            result = _api_call(v["id"], token, method="POST", status="PAUSED")
            applied_this = bool(result.get("success"))
            if not applied_this:
                print(f"  [warn] pause failed for {v['id']}: {result}", file=sys.stderr)
        log_pause(v, applied_this)

    if not dry_run:
        print(f"\nLogged to {_LOG_FILE}. Use undo.py to re-activate if needed.")

    if pause_verdicts:
        if args.notify_email:
            notify_email(pause_verdicts, dry_run)
        if args.notify_webhook:
            notify_webhook(args.notify_webhook, pause_verdicts, dry_run)


if __name__ == "__main__":
    main()
