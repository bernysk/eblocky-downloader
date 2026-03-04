#!/usr/bin/env python3
"""
eBlocky Receipt Exporter
Exports all receipts from app.eblocky.sk to JSON using the Firebase/Firestore REST API.

Usage:
  python eblocky_export.py --email YOUR_EMAIL --password YOUR_PASSWORD
  python eblocky_export.py --har path/to/session.har          # use existing HAR token
  python eblocky_export.py --email e@mail.com --password pw --limit 10
  python eblocky_export.py --email e@mail.com --password pw --output my_receipts.json
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

# Firestore REST pagination page size
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
    return v  # fallback – return raw


def doc_to_dict(doc: dict) -> dict:
    """Convert a Firestore REST document → plain dict with metadata."""
    fields = {k: _fs_value(v) for k, v in doc.get("fields", {}).items()}
    # Add Firestore metadata
    name = doc.get("name", "")
    fields["_firestore_id"]          = name.split("/")[-1] if name else None
    fields["_firestore_createTime"]  = doc.get("createTime")
    fields["_firestore_updateTime"]  = doc.get("updateTime")
    return fields


# ── Receipt fetching ──────────────────────────────────────────────────────────

def _run_query(id_token: str, user_uid: str, limit: int | None, start_after: dict | None) -> dict:
    """Execute a Firestore runQuery REST call and return the raw JSON response."""
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
    }

    if limit is not None:
        structured_query["limit"] = limit
    else:
        structured_query["limit"] = PAGE_SIZE

    if start_after:
        structured_query["startAt"] = start_after

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


def fetch_all_receipts(id_token: str, user_uid: str, max_receipts: int | None) -> list[dict]:
    """
    Page through all receipts in Firestore and return a list of plain dicts.
    If max_receipts is set, stop after that many.
    """
    receipts: list[dict] = []
    start_after = None
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
            page_docs.append(doc_to_dict(doc))
            last_doc = doc

        print(f"    → {len(page_docs)} receipts received")
        receipts.extend(page_docs)

        # Stop conditions
        if len(page_docs) < limit:
            break  # last page
        if max_receipts is not None and len(receipts) >= max_receipts:
            break

        # Build cursor for next page from last document.
        # Firestore REST uses startAt with before=False to mean "startAfter".
        if last_doc:
            last_fields = last_doc.get("fields", {})
            start_after = {
                "before": False,   # False = exclusive / "startAfter" semantics
                "values": [
                    last_fields.get("app_issueDateTimestamp", {"nullValue": None}),
                    {"referenceValue": last_doc["name"]},
                ],
            }
        else:
            break

        page += 1
        time.sleep(0.2)  # gentle rate-limiting

    return receipts


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Export eBlocky receipts to JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    auth_group = parser.add_mutually_exclusive_group(required=True)
    auth_group.add_argument("--email",    help="eBlocky account email")
    auth_group.add_argument("--har",      metavar="HAR_FILE",
                             help="Path to a HAR file with a recorded login session (token may be expired)")

    parser.add_argument("--password", help="eBlocky account password (required with --email)")
    parser.add_argument("--limit",    type=int, default=None,
                        help="Max number of receipts to export (omit for all)")
    parser.add_argument("--output",   default=None,
                        help="Output JSON file path (default: eblocky_receipts_YYYYMMDD_HHMMSS.json)")

    args = parser.parse_args()

    # ── Authenticate
    if args.email:
        if not args.password:
            parser.error("--password is required when using --email")
        id_token, user_uid = login(args.email, args.password)
    else:
        id_token, user_uid = token_from_har(args.har)

    # ── Fetch
    t_start = time.time()
    receipts = fetch_all_receipts(id_token, user_uid, max_receipts=args.limit)
    elapsed  = time.time() - t_start

    print(f"\n[+] Total receipts fetched: {len(receipts)}  ({elapsed:.1f}s)")

    # ── Output
    output_path = args.output or f"eblocky_receipts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    export_data = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "user_uid":    user_uid,
        "total":       len(receipts),
        "receipts":    receipts,
    }

    Path(output_path).write_text(json.dumps(export_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[+] Saved → {output_path}")


if __name__ == "__main__":
    main()
