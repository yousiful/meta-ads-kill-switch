"""Undo, re-activate items that kill_switch.py paused.

Reads from paused.jsonl (the log that kill_switch.py writes) and calls the
Meta API to set each matching item back to ACTIVE.

Usage:
  python undo.py --last 1          # undo the most recent pause
  python undo.py --last 5          # undo the last 5 pauses
  python undo.py --id 120250...    # undo one specific item by Meta object id
  python undo.py --dry-run --last 3  # print what would be re-activated

Environment variables (same as kill_switch.py):
  META_ACCESS_TOKEN
  META_AD_ACCOUNT_ID  (not needed for undo, but checked for consistency)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_API_VERSION = "v21.0"
_LOG_FILE = Path("paused.jsonl")


def _api_call(path: str, token: str, method: str = "GET", **params) -> dict:
    params["access_token"] = token
    url = f"https://graph.facebook.com/{_API_VERSION}/{path}"
    for attempt in range(4):
        try:
            if method == "GET":
                resp = urllib.request.urlopen(url + "?" + urllib.parse.urlencode(params), timeout=60)
            else:
                data = urllib.parse.urlencode(params).encode()
                req = urllib.request.Request(url, data=data, method=method)
                resp = urllib.request.urlopen(req, timeout=60)
            return json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                body = json.load(exc)
            except Exception:
                body = {}
            error_code = (body.get("error") or {}).get("code")
            if error_code in (4, 17, 80004):
                time.sleep(60 * (attempt + 1))
                continue
            return body
        except Exception as exc:
            print(f"[api error] {exc}", file=sys.stderr)
    return {}


def load_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Re-activate items that kill_switch.py paused.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--last", type=int, metavar="N",
                       help="Re-activate the last N paused items.")
    group.add_argument("--id", metavar="OBJECT_ID",
                       help="Re-activate one specific item by its Meta object id.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without calling the API.")
    parser.add_argument("--log", default=str(_LOG_FILE),
                        help=f"Path to paused.jsonl (default: {_LOG_FILE}).")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    token = os.getenv("META_ACCESS_TOKEN", "")
    if not token:
        print("ERROR: META_ACCESS_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)

    records = load_log(Path(args.log))
    if not records:
        print(f"No records found in {args.log}")
        return

    # Only look at records where the pause was actually applied
    applied = [r for r in records if r.get("applied")]

    if args.id:
        targets = [r for r in applied if r["id"] == args.id]
        if not targets:
            print(f"No applied pause found for id {args.id}")
            return
    else:
        targets = applied[-args.last:]

    if not targets:
        print("Nothing to undo.")
        return

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Re-activating {len(targets)} item(s):")
    for r in targets:
        print(f"  [{r['level']}] {r['name']}  (paused at {r['ts']})")
        if not args.dry_run:
            result = _api_call(r["id"], token, method="POST", status="ACTIVE")
            if result.get("success"):
                print(f"    -> re-activated")
            else:
                print(f"    -> failed: {result}", file=sys.stderr)


if __name__ == "__main__":
    main()
