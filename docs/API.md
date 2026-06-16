# Midasbuy API

Base path: **`/api`** (e.g. `http://localhost:8000/api/...`).
**Every data endpoint requires authentication** — see [AUTH.md](AUTH.md).

Interactive, always-accurate docs (generated from the code by FastAPI):
- **Swagger UI:** `/api/docs`  ·  **ReDoc:** `/api/redoc`  ·  **OpenAPI JSON:** `/api/openapi.json`

Two families:

| Family | Style | Use it for |
|--------|-------|-----------|
| **Single** (`/player-info`, `/code-status`, `/redeem-now`) | synchronous | one player / one code |
| **Bulk** (`/bulk/*`) | asynchronous job | many players / many codes |

Bulk endpoints create a **job** and return immediately. You get results two ways:
- **polling** `GET /api/bulk/jobs/{id}` (used by the panel), or
- **webhook** — set `webhook_url`; the finished result is POSTed there (signed).

> Note on code checks: Midasbuy ties a redeem code to a **player**, so every
> code-status / redeem call needs a `player_id` (not the code alone).

---

## Single endpoints

### `GET /api/player-info` — player lookup
`?player_id=...&country_code=bd&account_id=1`
```json
{ "success": true, "player": { "player_id": "2584...", "username": "Name", "zone_id": "" } }
```

### `POST /api/code-status` — is a code valid / used / invalid (no redeem)
```json
// request
{ "account_id": 1, "country_code": "bd", "player_id": "2584...", "pin_code": "ABCD-EFGH-IJKL" }
// response
{ "success": true, "status": "valid", "message": "...", "username": "Name", "product_name": "60 UC" }
```
`status` ∈ `player_not_found | invalid | used | valid`.

### `POST /api/redeem-now` — all-in-one redeem
Looks up the player → validates the code → redeems, in one call. Same request
body as `/code-status`.
```json
{ "success": true, "status": "redeemed", "message": "Redeemed successfully.",
  "username": "Name", "product_name": "60 UC" }
```
`status` ∈ `player_not_found | invalid | used | redeemed | failed`.

> `POST /api/redeem` also exists — the **two-step** (validate → confirm) flow the
> panel uses. For programmatic use prefer `/redeem-now`.

---

## Bulk endpoints
All take `account_id`, `country_code`, and an optional `webhook_url`. They return
the **job** (see shape below).

### `POST /api/bulk/player-info`
```json
{ "account_id": 1, "country_code": "bd",
  "player_ids": ["2584...", "5123..."],
  "webhook_url": "https://you.example.com/hooks/midasbuy" }
```

### `POST /api/bulk/code-status` — one player, many codes (no redeem)
```json
{ "account_id": 1, "country_code": "bd",
  "player_id": "2584...",
  "pin_codes": ["ABCD-EFGH-IJKL", "WXYZ-1234-5678"],
  "webhook_url": "https://you.example.com/hooks/midasbuy" }
```

### `POST /api/bulk/redeem` — one player, many codes (redeem each)
Same body as `/bulk/code-status`.

#### Job response (returned by all three)
```json
{ "job_id": 42, "job_type": "redeem", "status": "pending",
  "account_id": 1, "country_code": "bd",
  "total_items": 2, "processed_items": 0, "succeeded_items": 0, "failed_items": 0,
  "progress_percent": 0, "webhook_url": "https://...", "created_at": "2026-06-12T10:00:00Z" }
```

### `GET /api/bulk/jobs/{job_id}` — poll
Returns the job plus a **valid / invalid split** and the full item list.
```json
{ "job_id": 42, "status": "completed",
  "total_items": 2, "succeeded_items": 1, "failed_items": 1, "progress_percent": 100,
  "valid":   [ { "player_id": "2584...", "pin_code": "ABCD-EFGH-IJKL",
                 "success": true, "status": "success", "message": "Redeemed successfully.",
                 "username": "Name", "product_name": "60 UC" } ],
  "invalid": [ { "player_id": "5123...", "pin_code": "WXYZ-1234-5678",
                 "success": false, "status": "failed", "message": "Redeem code is already used." } ],
  "items":   [ "...both of the above..." ] }
```
`status` ∈ `pending | running | completed | failed`.

### `GET /api/bulk/jobs/{job_id}/items?limit=100&offset=0`
Paginated items for large jobs.

### Webhook payload (POSTed to `webhook_url` when the job finishes)
Body = the same `{job_id, status, valid, invalid, items, ...}` shape as the poll
response. Verify it came from us:
```
X-Webhook-Signature = hex( HMAC_SHA256(WEBHOOK_SECRET, raw_request_body) )
X-Webhook-Event     = bulk_job.completed
```

---

## Client loop (bulk redeem, polling)
```python
import time, requests
BASE = "http://localhost:8000/api"
HEADERS = {"Authorization": "Bearer <access_token>"}  # or HMAC headers, see AUTH.md

job = requests.post(f"{BASE}/bulk/redeem", headers=HEADERS, json={
    "account_id": 1, "country_code": "bd",
    "player_id": "2584...", "pin_codes": ["ABCD-EFGH-IJKL", "WXYZ-1234-5678"],
}).json()
jid = job["job_id"]

while True:
    j = requests.get(f"{BASE}/bulk/jobs/{jid}", headers=HEADERS).json()
    print(j["status"], j["processed_items"], "/", j["total_items"])
    if j["status"] in ("completed", "failed"):
        break
    time.sleep(3)

print("valid:", [i["pin_code"] for i in j["valid"]])
print("invalid:", [(i["pin_code"], i["message"]) for i in j["invalid"]])
```

## Notes
- Items in one job run sequentially (one warm browser session). Different accounts
  run in parallel; same-account jobs are serialised by a Redis lock.
- One bad row never fails the whole job — it's marked `failed` with a message.
- Watch jobs in Django admin under **Bulk → Bulk jobs**.
