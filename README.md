# eBlocky Receipt Exporter

A Python script that exports all your receipts from [app.eblocky.sk](https://app.eblocky.sk) into a JSON file using the Firebase/Firestore REST API directly — no browser required.

---

## How it works

The eBlocky web app is built on Firebase. When you open a receipt list, the app:

1. Authenticates with **Firebase Auth** using your email and password
2. Receives a short-lived **ID token** (valid 1 hour)
3. Uses that token to query **Firestore** — Google's NoSQL cloud database — where all receipts are stored under your user account

This script replays exactly those steps via HTTP, without needing a browser or Selenium. It pages through your receipts in batches of 50 and saves everything to a single JSON file.

---

## Requirements

Python 3.10+ and the `requests` library:

```bash
pip install requests
```

---

## Usage

### Basic — export all receipts

```bash
python eblocky_export.py --email you@example.com --password yourpassword
```

Output file is auto-named: `eblocky_receipts_20260304_143000.json`

---

### Limit — useful for testing

Fetch only the first N receipts (most recent first):

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

If you've already captured a browser session as a HAR file, you can extract the token from it instead of logging in fresh. Note that tokens expire after **1 hour** — if you get a 401 error, switch to `--email`/`--password`.

```bash
python eblocky_export.py --har session.har
```

---

## Output format

The script produces a single `.json` file:

```json
{
  "exported_at": "2026-03-04T14:30:00+00:00",
  "user_uid": "jaNRobAQ...",
  "total": 142,
  "receipts": [
    {
      "app_userUid": "jaNRobAQ...",
      "app_issueDateTimestamp": "2025-11-01T10:23:00Z",
      "seller_name": "Tesco",
      "total_amount": 12.49,
      "_firestore_id": "jaNRobAQ..._O-1EB57...",
      "_firestore_createTime": "2025-11-01T10:24:05Z",
      "_firestore_updateTime": "2025-11-01T10:24:05Z"
    },
    ...
  ]
}
```

Each receipt contains all available fields as stored in Firestore, plus three metadata fields prefixed with `_firestore_`:

| Field | Description |
|---|---|
| `_firestore_id` | Internal document ID |
| `_firestore_createTime` | When the receipt was first stored |
| `_firestore_updateTime` | When it was last modified |

Receipts are ordered **newest first** (by issue date).

---

## All options

| Argument | Description |
|---|---|
| `--email` | Your eBlocky account email *(required unless using --har)* |
| `--password` | Your eBlocky account password *(required with --email)* |
| `--har` | Path to a HAR file with a recorded login session |
| `--limit` | Maximum number of receipts to fetch (default: all) |
| `--output` | Output file path (default: auto-generated with timestamp) |

`--email` and `--har` are mutually exclusive — use one or the other.

---

## Notes

- The script fetches in pages of 50 receipts and saves everything at the end of the run
- A small delay (0.2s) is added between pages to avoid hammering the API
- If the token expires mid-run (after 1 hour), re-run with `--email`/`--password` to get a fresh one
