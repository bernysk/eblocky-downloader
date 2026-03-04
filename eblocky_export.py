#!/usr/bin/env python3
# Developed with AI assistance from Claude (https://claude.ai) by Anthropic
"""
eBlocky Receipt Exporter
Exports receipts from app.eblocky.sk to JSON using the Firebase/Firestore REST API.

Usage:
  python eblocky_export.py --email YOUR_EMAIL --password YOUR_PASSWORD
  python eblocky_export.py --email e@mail.com --password pw --limit 10
  python eblocky_export.py --email e@mail.com --password pw --output my_receipts.json
  python eblocky_export.py --email e@mail.com --password pw --update eblocky_receipts_20260304_153203.json
  python eblocky_export.py --har path/to/session.har
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: 'requests' library not found. Install it with:  pip install requests")
    sys.exit(1)

# ── Firebase / Firestore constants ────────────────────────────────────────────
FIREBASE_API_KEY   = "AIzaSyCwJo-wzypxg_L5pnwhQPqsyppOUzAYHlk"
FIREBASE_PROJECT   = "sk-venari-ereceipt"
FIRESTORE_DB       = f"projects/{FIREBASE_PROJECT}/databases/(default)"
FIRESTORE_PARENT   = f"{FIRESTORE_DB}/documents/version/prod"
FIRESTORE_BASE_URL = "https://firestore.googleapis.com/v1"
AUTH_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
    f"?key={FIREBASE_API_KEY}"
)

PAGE_SIZE = 50


# ── Auth ──────────────────────────────────────────────────────────────────────

def login(email: str, password: str) -> tuple[str, str]:
    """Sign in with email/password → (id_token, user_uid)"""
    print(f"[*] Logging in as {email} …")
    resp = requests.post(
        AUTH_URL,
        json={"email": email, "password": password, "returnSecureToken": True},
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"ERROR: Login failed ({resp.status_code}): {resp.text}")
        sys.exit(1)
    data = resp.json()
    print(f"[+] Logged in  |  UID: {data['localId']}")
    return data["idToken"], data["localId"]


def token_from_har(har_path: str) -> tuple[str, str]:
    """Extract the most recent id_token and user_uid from a HAR file."""
    print(f"[*] Extracting token from HAR: {har_path}")
    with open(har_path, encoding="utf-8") as f:
        har = json.load(f)

    id_token = user_uid = None
    for entry in har["log"]["entries"]:
        req = entry["request"]
        if "signInWithPassword" in req["url"] and req["method"] == "POST":
            resp_text = entry["response"]["content"].get("text", "")
            if resp_text:
                data = json.loads(resp_text)
                id_token = data.get("idToken")
                user_uid = data.get("localId")

    if not id_token or not user_uid:
        print("ERROR: No login response found in HAR file.")
        sys.exit(1)

    print(f"[+] Token extracted  |  UID: {user_uid}")
    print("[!] Note: HAR tokens expire after 1 hour. If you get 401 errors, use --email/--password instead.")
    return id_token, user_uid


# ── Existing file helpers ─────────────────────────────────────────────────────

def load_existing(path: str) -> tuple[list[dict], set[str]]:
    """
    Load an existing export file.
    Returns (receipts_list, set_of_firestore_ids).
    Exits if the file is missing or malformed.
    """
    p = Path(path)
    if not p.exists():
        print(f"ERROR: File not found: {path}")
        sys.exit(1)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        receipts = data["receipts"]
        known_ids = {r["_firestore_id"] for r in receipts if r.get("_firestore_id")}
        print(f"[*] Loaded existing file: {path}")
        print(f"    → {len(receipts)} receipts already stored  |  latest: {receipts[0].get('app_issueDateTimestamp', '?')}")
        return receipts, known_ids
    except (KeyError, json.JSONDecodeError) as e:
        print(f"ERROR: Could not parse {path}: {e}")
        sys.exit(1)


# ── Firestore helpers ─────────────────────────────────────────────────────────

def _fs_value(v: dict):
    """Recursively convert a Firestore value dict → plain Python value."""
    if "stringValue"    in v: return v["stringValue"]
    if "integerValue"   in v: return int(v["integerValue"])
    if "doubleValue"    in v: return float(v["doubleValue"])
    if "booleanValue"   in v: return bool(v["booleanValue"])
    if "nullValue"      in v: return None
    if "timestampValue" in v: return v["timestampValue"]
    if "bytesValue"     in v: return v["bytesValue"]
    if "referenceValue" in v: return v["referenceValue"]
    if "geoPointValue"  in v:
        gp = v["geoPointValue"]
        return {"latitude": gp.get("latitude"), "longitude": gp.get("longitude")}
    if "arrayValue" in v:
        return [_fs_value(i) for i in v["arrayValue"].get("values", [])]
    if "mapValue" in v:
        return {k: _fs_value(val) for k, val in v["mapValue"].get("fields", {}).items()}
    return v


def doc_to_dict(doc: dict) -> dict:
    """Convert a Firestore REST document → plain dict with metadata."""
    fields = {k: _fs_value(v) for k, v in doc.get("fields", {}).items()}
    name = doc.get("name", "")
    fields["_firestore_id"]         = name.split("/")[-1] if name else None
    fields["_firestore_createTime"] = doc.get("createTime")
    fields["_firestore_updateTime"] = doc.get("updateTime")
    return fields


# ── Receipt fetching ──────────────────────────────────────────────────────────

def _run_query(id_token: str, user_uid: str, limit: int, start_after: dict | None) -> list:
    """Execute a Firestore runQuery REST call and return the raw result list."""
    url = f"{FIRESTORE_BASE_URL}/{FIRESTORE_PARENT}:runQuery"
    headers = {"Authorization": f"Bearer {id_token}"}

    structured_query: dict = {
        "from": [{"collectionId": "receipt"}],
        "where": {
            "compositeFilter": {
                "op": "AND",
                "filters": [
                    {
                        "fieldFilter": {
                            "field": {"fieldPath": "app_userUid"},
                            "op": "EQUAL",
                            "value": {"stringValue": user_uid},
                        }
                    },
                    {
                        "fieldFilter": {
                            "field": {"fieldPath": "app_issueDateTimestamp"},
                            "op": "GREATER_THAN_OR_EQUAL",
                            "value": {"timestampValue": "1999-12-31T23:00:00.000000000Z"},
                        }
                    },
                    {
                        "fieldFilter": {
                            "field": {"fieldPath": "app_issueDateTimestamp"},
                            "op": "LESS_THAN_OR_EQUAL",
                            "value": {"timestampValue": "2099-12-31T23:00:00.000000000Z"},
                        }
                    },
                ],
            }
        },
        "orderBy": [
            {"field": {"fieldPath": "app_issueDateTimestamp"}, "direction": "DESCENDING"},
            {"field": {"fieldPath": "__name__"},               "direction": "DESCENDING"},
        ],
        "limit": limit,
    }

    if start_after:
        # before=False means exclusive cursor → equivalent to "startAfter"
        structured_query["startAt"] = {**start_after, "before": False}

    resp = requests.post(
        url,
        headers=headers,
        json={"structuredQuery": structured_query},
        timeout=30,
    )

    if resp.status_code == 401:
        print("ERROR: Token expired or invalid. Re-run with --email/--password to get a fresh token.")
        sys.exit(1)
    if resp.status_code != 200:
        print(f"ERROR: Firestore query failed ({resp.status_code}): {resp.text[:400]}")
        sys.exit(1)

    return resp.json()


def fetch_receipts(
    id_token: str,
    user_uid: str,
    max_receipts: int | None = None,
    known_ids: set[str] | None = None,
) -> tuple[list[dict], bool]:
    """
    Page through receipts newest-first.

    - max_receipts: hard cap on how many to fetch (None = unlimited)
    - known_ids:    set of _firestore_ids already in an existing file;
                    stops as soon as a match is encountered

    Returns (new_receipts, hit_existing) where hit_existing=True means
    the fetch stopped because it reached a receipt already in the existing file.
    """
    receipts: list[dict] = []
    start_after = None
    hit_existing = False
    page = 1

    while True:
        limit = PAGE_SIZE
        if max_receipts is not None:
            remaining = max_receipts - len(receipts)
            if remaining <= 0:
                break
            limit = min(PAGE_SIZE, remaining)

        print(f"[*] Fetching page {page} (up to {limit} receipts) …")
        raw_results = _run_query(id_token, user_uid, limit=limit, start_after=start_after)

        page_docs = []
        last_doc  = None

        for result in raw_results:
            doc = result.get("document")
            if not doc:
                continue

            receipt = doc_to_dict(doc)
            fid = receipt.get("_firestore_id")

            # In update mode: stop the moment we hit a receipt we already have
            if known_ids and fid in known_ids:
                print(f"    → Reached known receipt ({fid}), stopping.")
                hit_existing = True
                break

            page_docs.append(receipt)
            last_doc = doc

        receipts.extend(page_docs)
        print(f"    → {len(page_docs)} new receipt(s) on this page")

        if hit_existing:
            break
        if len(page_docs) < limit:
            break  # natural end of data
        if max_receipts is not None and len(receipts) >= max_receipts:
            break

        # Cursor for next page
        if last_doc:
            last_fields = last_doc.get("fields", {})
            start_after = {
                "values": [
                    last_fields.get("app_issueDateTimestamp", {"nullValue": None}),
                    {"referenceValue": last_doc["name"]},
                ]
            }
        else:
            break

        page += 1
        time.sleep(0.2)

    return receipts, hit_existing


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Export eBlocky receipts to JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    auth_group = parser.add_mutually_exclusive_group(required=True)
    auth_group.add_argument("--email", help="eBlocky account email")
    auth_group.add_argument("--har",   metavar="HAR_FILE",
                            help="Path to a HAR file with a recorded login session (token may be expired)")

    parser.add_argument("--password", help="eBlocky account password (required with --email)")
    parser.add_argument("--limit",    type=int, default=None,
                        help="Max number of receipts to fetch (omit for all)")
    parser.add_argument("--output",   default=None,
                        help="Output JSON file path (default: auto-generated timestamp name)")
    parser.add_argument("--update",   metavar="EXISTING_JSON",
                        help="Path to a previous export file; fetch only receipts newer than its contents "
                             "and save a fresh file with a new timestamp")

    args = parser.parse_args()

    # ── Authenticate
    if args.email:
        if not args.password:
            parser.error("--password is required when using --email")
        id_token, user_uid = login(args.email, args.password)
    else:
        id_token, user_uid = token_from_har(args.har)

    # ── Load existing file if updating
    existing_receipts: list[dict] = []
    known_ids: set[str] = set()

    if args.update:
        existing_receipts, known_ids = load_existing(args.update)
        print(f"[*] Update mode — fetching only receipts newer than the existing file\n")

    # ── Fetch
    t_start = time.time()
    new_receipts, hit_existing = fetch_receipts(
        id_token, user_uid,
        max_receipts=args.limit,
        known_ids=known_ids if args.update else None,
    )
    elapsed = time.time() - t_start

    # ── Merge: new receipts on top, existing ones below (preserves newest-first order)
    if args.update:
        if not new_receipts:
            print(f"\n[+] Already up to date — no new receipts found  ({elapsed:.1f}s)")
            return
        merged = new_receipts + existing_receipts
        print(f"\n[+] {len(new_receipts)} new  +  {len(existing_receipts)} existing  =  {len(merged)} total  ({elapsed:.1f}s)")
    else:
        merged = new_receipts
        print(f"\n[+] Total receipts fetched: {len(merged)}  ({elapsed:.1f}s)")

    # ── Save to a new file with a fresh timestamp
    output_path = args.output or f"eblocky_receipts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    export_data = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user_uid":    user_uid,
        "total":       len(merged),
        "receipts":    merged,
    }

    Path(output_path).write_text(json.dumps(export_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[+] Saved → {output_path}")


if __name__ == "__main__":
    main()
