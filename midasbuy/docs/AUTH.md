# API Authentication

Every `/api/*` data endpoint requires authentication. There are two ways in, both
resolving to the same **Merchant**:

| Caller | Method | How |
|--------|--------|-----|
| Dashboard / browser user | **JWT** | log in → `Authorization: Bearer <access_token>` |
| Server-to-server client | **HMAC signing** | sign each request with an API key secret |

Public (no auth): `POST /api/auth/register`, `/login`, `/refresh`.

---

## A) Dashboard users — JWT

```bash
# Register (or your panel does this once)
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"name":"Acme","email":"ops@acme.com","password":"a-strong-password"}'

# Login -> tokens
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"ops@acme.com","password":"a-strong-password"}'
# => {"access_token":"...","refresh_token":"...","token_type":"bearer"}

# Call any endpoint with the access token
curl "http://localhost:8000/api/player-info?player_id=2584...&account_id=1" \
  -H "Authorization: Bearer <access_token>"

# Access token expires in 15 min — get a new one with the refresh token
curl -X POST http://localhost:8000/api/auth/refresh \
  -H "Content-Type: application/json" -d '{"refresh_token":"<refresh_token>"}'
```
For a browser dashboard, store the access token in an **httpOnly cookie** (not
localStorage) so XSS can't read it.

---

## B) Server-to-server clients — HMAC signing

### 1. Create an API key (once, while logged in)
```bash
curl -X POST http://localhost:8000/api/auth/api-keys \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" -d '{"label":"prod server"}'
# => {"key_id":"mk_...","secret":"sk_...","label":"prod server"}
```
**The `secret` is shown once.** Store it securely; the server keeps it encrypted.

### 2. Sign every request
Build a canonical string, fields joined by a newline (`\n`) in this exact order:

```
METHOD            e.g. POST
PATH              e.g. /api/bulk/redeem
QUERY             raw query string, '' if none
BODY_SHA256       hex sha256 of the exact request body ('' body -> sha256 of empty)
TIMESTAMP         unix seconds (also sent as X-Timestamp)
NONCE             unique random string per request (also sent as X-Nonce)
```
```
signature = hex( HMAC_SHA256(secret, canonical_string) )
```
Send these headers:

| Header | Value |
|--------|-------|
| `X-Api-Key` | the `key_id` |
| `X-Timestamp` | unix seconds (must be within ±5 min of server time) |
| `X-Nonce` | unique per request (replays are rejected) |
| `X-Signature` | the hex signature above |

The server recomputes the signature, checks the timestamp window, and rejects any
nonce it has already seen — so a captured request **cannot be replayed**, and the
secret **never travels over the network**.

### 3. Python client (drop-in)
```python
import hashlib, hmac, json, time, uuid, requests

BASE = "http://localhost:8000"
KEY_ID = "mk_..."
SECRET = "sk_..."

def signed_request(method, path, *, params=None, json_body=None):
    body = b"" if json_body is None else json.dumps(json_body, separators=(",", ":")).encode()
    query = ""
    if params:
        from urllib.parse import urlencode
        query = urlencode(params)
    ts = str(int(time.time()))
    nonce = uuid.uuid4().hex
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = "\n".join([method.upper(), path, query, body_hash, ts, nonce])
    sig = hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-Api-Key": KEY_ID, "X-Timestamp": ts, "X-Nonce": nonce, "X-Signature": sig,
        "Content-Type": "application/json",
    }
    url = BASE + path + (("?" + query) if query else "")
    return requests.request(method, url, data=body, headers=headers)

# Bulk redeem
r = signed_request("POST", "/api/bulk/redeem", json_body={
    "account_id": 1, "country_code": "bd",
    "items": [{"player_id": "2584...", "pin_code": "ABCD-EFGH-IJKL"}],
})
print(r.status_code, r.json())
```
> Important: sign the **exact bytes** you send. The example signs a compact JSON
> (`separators=(",", ":")`) and sends those same bytes via `data=body` — don't let
> the HTTP library re-serialize the body, or the hash won't match.

### 4. Revoke a key
```bash
curl -X DELETE http://localhost:8000/api/auth/api-keys/mk_... \
  -H "Authorization: Bearer <access_token>"
```

---

## Server configuration
- `JWT_SECRET` — signs JWTs (defaults to `SECRET_KEY`; set a dedicated value in prod).
- `APIAUTH_FERNET_KEY` — encrypts API secrets at rest. Generate:
  ```bash
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
  If you change this key, existing API secrets can no longer be decrypted — rotate keys, don't lose them.

## Security properties
- Secrets are **encrypted at rest** (Fernet); a DB-only leak can't forge requests.
- HMAC requests are **replay-proof** (timestamp window + one-time nonce in Redis) and the secret is **never sent**.
- If Redis is down, signed requests **fail closed** (rejected) rather than skipping replay protection.
- New merchant registration is open by default — gate it behind admin approval for production by setting new merchants `is_active=False` and approving in Django admin.
