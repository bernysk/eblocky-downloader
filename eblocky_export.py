#!/usr/bin/env python3
"""
eBlocky Receipt Exporter
========================
Exports receipts from app.eblocky.sk to a local JSON file using the 
Firebase/Firestore REST API.

Features:
- Authenticates via Email/Password or a captured HAR session.
- Handles Firestore's complex typed-JSON REST responses.
- Supports incremental updates (only downloading newly issued receipts).
- Preserves the chronological order of receipts.

Usage Examples:
  python eblocky_export.py --email YOUR_EMAIL --password YOUR_PASSWORD
  python eblocky_export.py --email e@mail.com --password pw --limit 10
  python eblocky_export.py --email e@mail.com --password pw --output my_receipts.json
  python eblocky_export.py --email e@mail.com --password pw --update existing_receipts.json
  python eblocky_export.py --har path/to/session.har

Dependencies:
  - requests (install via: pip install requests)
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

# Public API key used by the eBlocky web/mobile client
FIREBASE_API_KEY   = "AIzaSyCwJo-wzypxg_L5pnwhQPqsyppOUzAYHlk"
FIREBASE_PROJECT   = "sk-venari-ereceipt"

# Firestore database pathing
FIRESTORE_DB       = f"projects/{FIREBASE_PROJECT}/databases/(default)"
FIRESTORE_PARENT   = f"{FIRESTORE_DB}/documents/version/prod"
FIRESTORE_BASE_URL = "https://firestore.googleapis.com/v1"

# Endpoint for swapping email/password for a Firebase Auth JWT (idToken)
AUTH_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
    f"?key={FIREBASE_API_KEY}"
)

# Number of documents to fetch per REST API call
PAGE_SIZE = 50


# ── Auth ──────────────────────────────────────────────────────────────────────

def login(email: str, password: str) -> tuple[str, str]:
    """
    Authenticates with Firebase Identity Toolkit using an email and password.

    Args:
        email: User's registered email address.
        password: User's password.

    Returns:
        tuple: (idToken, localId) where `idToken` is the Bearer token for 
               Firestore requests, and `localId` is the user's UID.
    """
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
    
    # idToken = JWT for auth header; localId = Firebase User UID
    return data["idToken"], data["localId"]


def token_from_har(har_path: str) -> tuple[str, str]:
    """
    Extracts the most recent Firebase id_token and user_uid from a browser HAR file.
    Useful if standard login is blocked by Captchas or MFA.

    Args:
        har_path: Path to the .har JSON file.

    Returns:
        tuple: (idToken, localId). Exits the script if none are found.
    """
    print(f"[*] Extracting token from HAR: {har_path}")
    with open(har_path, encoding="utf-8") as f:
        har = json.load(f)

    id_token = user_uid = None
    
    # Iterate through all network requests recorded in the HAR file
    for entry in har["log"]["entries"]:
        req = entry["request"]
        # Look specifically for the Firebase Auth sign-in endpoint
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
    Loads an existing export file to support incremental updates.

    Args:
        path: Filepath to the previously exported JSON file.

    Returns:
        tuple: (receipts_list, set_of_firestore_ids). The set is used for O(1) 
               lookups to detect when we've reached already-downloaded receipts.
    """
    p = Path(path)
    if not p.exists():
        print(f"ERROR: File not found: {path}")
        sys.exit(1)
        
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        receipts = data["receipts"]
        # Extract unique document IDs to avoid duplicates during incremental fetch
        known_ids = {r["_firestore_id"] for r in receipts if r.get("_firestore_id")}
        
        print(f"[*] Loaded existing file: {path}")
        print(f"    → {len(receipts)} receipts already stored  |  latest: {receipts[0].get('app_issueDateTimestamp', '?')}")
        return receipts, known_ids
    except (KeyError, json.JSONDecodeError) as e:
        print(f"ERROR: Could not parse {path}: {e}")
        sys.exit(1)


# ── Firestore helpers ─────────────────────────────────────────────────────────

def _fs_value(v: dict):
    """
    Recursively unwraps Firestore's strongly-typed JSON format into standard Python types.
    Firestore REST API returns data like: {"stringValue": "apple"} instead of just "apple".
    
    Args:
        v: A single Firestore field dictionary.
        
    Returns:
        A native Python data type (str, int, float, bool, None, list, dict).
    """
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
        
    # Fallback to returning the raw dictionary if type is unknown
    return v


def doc_to_dict(doc: dict) -> dict:
    """
    Converts a full Firestore REST document payload into a flattened dictionary,
    and injects helpful metadata (ID, creation/update times) prefixed with underscores.

    Args:
        doc: The raw document dictionary from the REST response.

    Returns:
        A flattened, native Python dictionary representing the document.
    """
    fields = {k: _fs_value(v) for k, v in doc.get("fields", {}).items()}
    name = doc.get("name", "")
    
    # Extract the actual document ID from the end of the full path
    fields["_firestore_id"]         = name.split("/")[-1] if name else None
    fields["_firestore_createTime"] = doc.get("createTime")
    fields["_firestore_updateTime"] = doc.get("updateTime")
    
    return fields


# ── Receipt fetching ──────────────────────────────────────────────────────────

def _run_query(id_token: str, user_uid: str, limit: int, start_after: dict | None) -> list:
    """
    Constructs and executes a 'runQuery' POST request against the Firestore API.

    Args:
        id_token: Auth JWT.
        user_uid: Firebase UID (used to filter only the user's receipts).
        limit: Max number of documents to return.
        start_after: Cursor dictionary for pagination.

    Returns:
        list: Raw result list containing document payloads.
    """
    url = f"{FIRESTORE_BASE_URL}/{FIRESTORE_PARENT}:runQuery"
    headers = {"Authorization": f"Bearer {id_token}"}

    # Build the Firestore query object
    structured_query: dict = {
        "from": [{"collectionId": "receipt"}],
        "where": {
            "compositeFilter": {
                "op": "AND",
                "filters": [
                    # Security/Data rule: Only get receipts belonging to this user
                    {
                        "fieldFilter": {
                            "field": {"fieldPath": "app_userUid"},
                            "op": "EQUAL",
                            "value": {"stringValue": user_uid},
                        }
                    },
                    # Date boundaries (required if we want to order by date)
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
        # Sort newest-first. The secondary sort on __name__ ensures stability 
        # in case multiple receipts share the exact same timestamp.
        "orderBy": [
            {"field": {"fieldPath": "app_issueDateTimestamp"}, "direction": "DESCENDING"},
            {"field": {"fieldPath": "__name__"},               "direction": "DESCENDING"},
        ],
        "limit": limit,
    }

    # Inject pagination cursor if provided
    if start_after:
        # 'before': False effectively translates to "startAfter" the cursor values
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
    Handles pagination logic to download receipts newest-first.

    Args:
        id_token: Auth JWT.
        user_uid: Firebase UID.
        max_receipts: Hard cap on how many to fetch (None = unlimited).
        known_ids: Set of `_firestore_ids` already present in an existing file.

    Returns:
        tuple: (new_receipts, hit_existing)
            - new_receipts: List of parsed receipt dictionaries.
            - hit_existing: Boolean indicating if fetch stopped early because 
                            it reached a previously downloaded receipt.
    """
    receipts: list[dict] = []
    start_after = None
    hit_existing = False
    page = 1

    while True:
        limit = PAGE_SIZE
        
        # Adjust limit on the final page if max_receipts is specified
        if max_receipts is not None:
            remaining = max_receipts - len(receipts)
            if remaining <= 0:
                break
            limit = min(PAGE_SIZE, remaining)

        print(f"[*] Fetching page {page} (up to {limit} receipts) …")
        raw_results = _run_query(id_token, user_uid, limit=limit, start_after=start_after)

        page_docs = []
        last_doc  = None
        page_known = 0
        
        # Process raw API results
        for result in raw_results:
            doc = result.get("document")
            if not doc:
                continue

            receipt = doc_to_dict(doc)
            fid = receipt.get("_firestore_id")

            # Check if we've hit data we already have (Incremental Update Logic)
            if known_ids and fid in known_ids:
                hit_existing = True
                page_known += 1
            else:
                page_docs.append(receipt)

            last_doc = doc

        receipts.extend(page_docs)
        skip_note = f"  |  {page_known} already known (skipped)" if page_known else ""
        print(f"    → {len(page_docs)} new receipt(s) on this page{skip_note}")

        # Stop conditions
        # 1. We hit an existing receipt (everything older is already saved)
        if hit_existing:
            break
        # 2. Page returned fewer items than requested (we reached the end of the DB)
        if len(page_docs) < limit:
            break
        # 3. We hit the user-defined max cap
        if max_receipts is not None and len(receipts) >= max_receipts:
            break

        # Generate the cursor for the next page based on the last document fetched.
        # Must match the `orderBy` fields exactly.
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
        time.sleep(0.2) # Basic rate limiting

    return receipts, hit_existing


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Export eBlocky receipts to JSON via the Firestore REST API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    auth_group = parser.add_mutually_exclusive_group(required=True)
    auth_group.add_argument("--email", help="eBlocky account email")
    auth_group.add_argument("--har",   metavar="HAR_FILE",
                            help="Path to a HAR file with a recorded login session (token may expire in 1h)")

    parser.add_argument("--password", help="eBlocky account password (required with --email)")
    parser.add_argument("--limit",    type=int, default=None,
                        help="Max number of receipts to fetch (omit to download all)")
    parser.add_argument("--output",   default=None,
                        help="Output JSON file path (default: auto-generated timestamped name)")
    parser.add_argument("--update",   metavar="EXISTING_JSON",
                        help="Path to a previous export; fetches only newer receipts and outputs a new combined file")

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

    # ── Merge results
    # New receipts are placed on top to preserve the newest-first order
    if args.update:
        if not new_receipts:
            print(f"\n[+] Already up to date — no new receipts found  ({elapsed:.1f}s)")
            return
        merged = new_receipts + existing_receipts
        print(f"\n[+] {len(new_receipts)} new  +  {len(existing_receipts)} existing  =  {len(merged)} total  ({elapsed:.1f}s)")
    else:
        merged = new_receipts
        print(f"\n[+] Total receipts fetched: {len(merged)}  ({elapsed:.1f}s)")

    # ── Save to file
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
