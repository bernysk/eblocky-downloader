# eBlocky Receipt Exporter

A Python script that exports all your receipts from [app.eblocky.sk](https://app.eblocky.sk) into a JSON file using the Firebase/Firestore REST API directly — no browser required.

---

## How it works

The eBlocky web app is built on Firebase. When you open a receipt list, the app:

1. Authenticates with **Firebase Auth** using your email and password
2. Receives a short-lived **ID token** (valid 1 hour)
3. Uses that token to query **Firestore** — Google's NoSQL cloud database — where all receipts are stored under your user account

This script replays exactly those steps via HTTP. It authenticates, then pages through your receipts in batches of 50 (newest first) and saves everything to a single JSON file.

---

## Requirements

Python 3.10+ and the `requests` library:

```bash
pip install requests
```

---

## Usage

### Full export — download all receipts

```bash
python eblocky_export.py --email you@example.com --password yourpassword
```

Output file is auto-named with a timestamp: `eblocky_receipts_20260304_143000.json`

---

### Update — only fetch what's new

Point `--update` at a previously exported file. The script loads all known receipt IDs from that file, fetches receipts newest-first, and stops the moment it hits one it already has. New and existing receipts are then merged and saved to a **new file** with the current timestamp — the old file is left untouched.

```bash
python eblocky_export.py --email you@example.com --password yourpassword \
  --update eblocky_receipts_20260304_153203.json
```

The script always scans the **full page of 50** before deciding whether to stop. This handles receipts that were scanned later and appear slightly out of date order — they will still be picked up as long as they fall within the same page. Once at least one known receipt is found on a page, fetching stops — everything on subsequent pages is guaranteed to be older and already stored.

Example output:
```
[*] Logging in as you@example.com …
[+] Logged in  |  UID: jaNRobAQ...
[*] Loaded existing file: eblocky_receipts_20260304_153203.json
    → 142 receipts already stored  |  latest: 2026-03-04T15:30:00Z
[*] Update mode — fetching only receipts newer than the existing file

[*] Fetching page 1 (up to 50 receipts) …
    → 7 new receipt(s) on this page  |  43 already known (skipped)

[+] 7 new  +  142 existing  =  149 total  (1.4s)
[+] Saved → eblocky_receipts_20260304_172500.json
```

If nothing is new:
```
[+] Already up to date — no new receipts found  (0.9s)
```

---

### Limit — useful for testing

Fetch only the N most recent receipts:

```bash
python eblocky_export.py --email you@example.com --password yourpassword --limit 10
```

---

### Custom output filename

```bash
python eblocky_export.py --email you@example.com --password yourpassword --output my_receipts.json
```

---

### Use an existing HAR file instead of credentials

If you've captured a browser session as a HAR file, the token inside can be reused. Note that Firebase tokens expire after **1 hour** — if you get a 401 error, switch to `--email`/`--password`.

```bash
python eblocky_export.py --har session.har
```

---

## All options

| Argument | Description |
|---|---|
| `--email` | Your eBlocky account email *(required unless using --har)* |
| `--password` | Your eBlocky account password *(required with --email)* |
| `--har` | Path to a HAR file with a recorded login session |
| `--update` | Path to a previous export file; fetch only new receipts and save a fresh file |
| `--limit` | Maximum number of receipts to fetch (default: all) |
| `--output` | Output file path (default: auto-generated with timestamp) |

`--email` and `--har` are mutually exclusive — use one or the other.

---

## Output format

Each run produces a new `.json` file. The structure is always the same whether doing a full export or an update:

```json
{
  "exported_at": "2026-03-04T17:25:00+00:00",
  "user_uid": "jaNRobAQ...",
  "total": 149,
  "receipts": [
    {
      "app_userUid": "jaNRobAQ...",
      "app_issueDateTimestamp": "2026-03-04T17:10:00Z",
      "seller_name": "Tesco",
      "total_amount": 12.49,
      "_firestore_id": "jaNRobAQ..._O-1EB57...",
      "_firestore_createTime": "2026-03-04T17:11:05Z",
      "_firestore_updateTime": "2026-03-04T17:11:05Z"
    },
    ...
  ]
}
```

Receipts are always ordered **newest first**. Each receipt contains all available Firestore fields plus three metadata fields:

| Field | Description |
|---|---|
| `_firestore_id` | Internal document ID (used for deduplication in `--update` mode) |
| `_firestore_createTime` | When the receipt was first stored in Firestore |
| `_firestore_updateTime` | When it was last modified |

---

## Notes

- Receipts are fetched in pages of 50, newest first
- A small delay (0.2 s) is added between pages to avoid hammering the API
- `--update` never modifies the original file — it always writes a new one
- If the token expires mid-run (after 1 hour), re-run with `--email`/`--password` to get a fresh one
