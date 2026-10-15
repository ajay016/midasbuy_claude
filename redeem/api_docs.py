"""
Single source of truth for the custom /docs API reference.

Each endpoint is described once (method, path, params, request/response example).
From that we render the reference AND generate idiomatic code samples for
Python (requests), Django, Laravel (PHP), and Node.js (axios) — so the docs and
the examples can never drift apart. Add an endpoint here and all four samples are
produced automatically.
"""
import json
import re

HOST = "https://YOUR_HOST"


# ── Multi-language literal formatter ───────────────────────────────────────────
def _fmt(value, lang: str, level: int = 0) -> str:
    pad = "    " * (level + 1)
    close = "    " * level

    if isinstance(value, dict):
        if not value:
            return "[]" if lang == "php" else "{}"
        ob, cb = ("[", "]") if lang == "php" else ("{", "}")
        rows = []
        for k, v in value.items():
            if lang == "php":
                key = f"'{k}' => "
            elif lang == "node":
                key = f"{k}: "
            else:  # python / json
                key = f'"{k}": '
            rows.append(pad + key + _fmt(v, lang, level + 1))
        return ob + "\n" + ",\n".join(rows) + "\n" + close + cb

    if isinstance(value, list):
        if not value:
            return "[]"
        rows = [pad + _fmt(v, lang, level + 1) for v in value]
        return "[\n" + ",\n".join(rows) + "\n" + close + "]"

    if isinstance(value, bool):
        if lang == "python":
            return "True" if value else "False"
        return "true" if value else "false"

    if value is None:
        return "None" if lang == "python" else "null"

    if isinstance(value, (int, float)):
        return str(value)

    s = str(value)
    if lang == "php":
        return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _pretty(value) -> str:
    return json.dumps(value, indent=2)


# ── Code-sample generators ─────────────────────────────────────────────────────
def _samples(ep: dict) -> dict:
    method = ep["method"].lower()
    url = HOST + ep["path"].replace("{job_id}", str(ep.get("path_example", "42")))
    body = ep.get("request")
    query = ep.get("query")
    auth = ep.get("auth", True)

    py_auth = '"Authorization": "Bearer <ACCESS_TOKEN>"'
    py_headers = f"headers={{{py_auth}}}," if auth else None

    # Python (requests)
    lines = ["import requests", "", f'resp = requests.{method}(', f'    "{url}",']
    if py_headers:
        lines.append("    " + py_headers)
    if body is not None:
        lines.append("    json=" + _indent(_fmt(body, "python"), 4) + ",")
    if query is not None:
        lines.append("    params=" + _indent(_fmt(query, "python"), 4) + ",")
    lines += ["    timeout=30,", ")", "print(resp.status_code, resp.json())"]
    python = "\n".join(lines)

    # Django (service function using settings)
    dj = ["import requests", "from django.conf import settings", "",
          "def call_midasbuy():", f'    resp = requests.{method}(',
          f'        f"{{settings.MIDASBUY_API_BASE}}{ep["path"].replace("{job_id}", str(ep.get("path_example", "42")))}",']
    if auth:
        dj.append('        headers={"Authorization": f"Bearer {settings.MIDASBUY_API_TOKEN}"},')
    if body is not None:
        dj.append("        json=" + _indent(_fmt(body, "python"), 8) + ",")
    if query is not None:
        dj.append("        params=" + _indent(_fmt(query, "python"), 8) + ",")
    dj += ["        timeout=30,", "    )", "    resp.raise_for_status()", "    return resp.json()"]
    django = "\n".join(dj)

    # Laravel (Http facade)
    php = ["use Illuminate\\Support\\Facades\\Http;", ""]
    head = "Http::withToken($accessToken)" if auth else "Http::asJson()"
    php.append(f"$response = {head}")
    if method == "get":
        arg = ", " + _indent(_fmt(query, "php"), 4) if query is not None else ""
        php.append(f"    ->get('{url}'{arg});")
    else:
        arg = ", " + _indent(_fmt(body, "php"), 4) if body is not None else ""
        php.append(f"    ->{method}('{url}'{arg});")
    php += ["", "return $response->json();"]
    laravel = "\n".join(php)

    # Node.js (axios)
    js = ['import axios from "axios";', ""]
    hdr = '{ headers: { Authorization: `Bearer ${accessToken}` } }' if auth else "{}"
    if method == "get":
        cfg = "{ "
        if query is not None:
            cfg = "{ params: " + _fmt(query, "node") + ", "
        cfg += ("headers: { Authorization: `Bearer ${accessToken}` } }" if auth else "}")
        js.append(f'const {{ data }} = await axios.get("{url}", {cfg});')
    else:
        parts = [f'  "{url}"']
        if body is not None:
            parts.append("  " + _indent(_fmt(body, "node"), 2))
        parts.append("  " + hdr)
        js.append(f"const {{ data }} = await axios.{method}(")
        js.append(",\n".join(parts))
        js.append(");")
    js.append("console.log(data);")
    node = "\n".join(js)

    return {"python": python, "django": django, "laravel": laravel, "node": node}


def _indent(text: str, spaces: int) -> str:
    """Re-indent a multi-line literal so nested lines line up under their key."""
    pad = " " * spaces
    out = []
    for i, line in enumerate(text.split("\n")):
        out.append(line if i == 0 else pad + line)
    return "\n".join(out)


# ── HMAC signing: the secure server-to-server scheme ───────────────────────────
# These helpers are shown once under "Authentication → Client setup". Every data
# endpoint sample below calls them, so clients copy the helper once and the rest
# of the samples stay short — and, crucially, correct (sign the EXACT bytes sent).
CLIENT_SETUP = {
    "python": '''import hashlib, hmac, json, time, uuid, requests
from urllib.parse import urlencode

BASE      = "''' + HOST + '''"
KEY_ID    = "mk_..."   # from the panel -> API Keys
SECRET    = "sk_..."   # shown ONCE when the key was created
CLIENT_ID = "..."      # REQUIRED X-Client-Id: your own client_ref (panel -> API Keys),
                       # or one of your clients' refs to attribute the request to them

def signed_request(method, path, *, params=None, json_body=None):
    """Sign and send one API request. The secret never leaves your server."""
    body  = b"" if json_body is None else json.dumps(json_body, separators=(",", ":")).encode()
    query = urlencode(params) if params else ""
    ts    = str(int(time.time()))
    nonce = uuid.uuid4().hex
    canonical = "\\n".join([method.upper(), path, query,
                           hashlib.sha256(body).hexdigest(), ts, nonce])
    sig = hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    headers = {"X-Api-Key": KEY_ID, "X-Timestamp": ts, "X-Nonce": nonce,
               "X-Signature": sig, "X-Client-Id": CLIENT_ID,
               "Content-Type": "application/json"}
    url = BASE + path + (("?" + query) if query else "")
    return requests.request(method, url, data=body, headers=headers, timeout=30)''',

    "django": '''import hashlib, hmac, json, time, uuid, requests
from urllib.parse import urlencode
from django.conf import settings   # set MIDASBUY_API_BASE / _KEY_ID / _SECRET

def signed_request(method, path, *, params=None, json_body=None):
    body  = b"" if json_body is None else json.dumps(json_body, separators=(",", ":")).encode()
    query = urlencode(params) if params else ""
    ts, nonce = str(int(time.time())), uuid.uuid4().hex
    canonical = "\\n".join([method.upper(), path, query,
                           hashlib.sha256(body).hexdigest(), ts, nonce])
    sig = hmac.new(settings.MIDASBUY_API_SECRET.encode(), canonical.encode(),
                   hashlib.sha256).hexdigest()
    headers = {"X-Api-Key": settings.MIDASBUY_API_KEY_ID, "X-Timestamp": ts,
               "X-Nonce": nonce, "X-Signature": sig,
               "X-Client-Id": settings.MIDASBUY_CLIENT_ID,  # REQUIRED: your client_ref
               "Content-Type": "application/json"}
    url = settings.MIDASBUY_API_BASE + path + (("?" + query) if query else "")
    return requests.request(method, url, data=body, headers=headers, timeout=30)''',

    "laravel": '''<?php
use Illuminate\\Support\\Facades\\Http;

// Reads config('services.midasbuy.*') — base, key_id, secret.
function signedRequest(string $method, string $path, array $opts = []) {
    $base   = config('services.midasbuy.base');
    $keyId  = config('services.midasbuy.key_id');
    $secret = config('services.midasbuy.secret');

    $body  = isset($opts['json']) ? json_encode($opts['json'], JSON_UNESCAPED_SLASHES) : '';
    $query = isset($opts['params']) ? http_build_query($opts['params']) : '';
    $ts    = (string) time();
    $nonce = bin2hex(random_bytes(16));
    $canonical = implode("\\n", [strtoupper($method), $path, $query,
                                 hash('sha256', $body), $ts, $nonce]);
    $sig = hash_hmac('sha256', $canonical, $secret);

    $url = $base . $path . ($query ? '?' . $query : '');
    return Http::withHeaders([
        'X-Api-Key'   => $keyId, 'X-Timestamp' => $ts, 'X-Nonce' => $nonce,
        'X-Signature' => $sig,   'Content-Type' => 'application/json',
    ])->withBody($body, 'application/json')->send(strtoupper($method), $url);
}''',

    "node": '''import crypto from "crypto";
import axios from "axios";

const BASE   = "''' + HOST + '''";
const KEY_ID = "mk_...";   // from the panel -> API Keys
const SECRET = "sk_...";   // shown ONCE when the key was created

export async function signedRequest(method, path, { params, jsonBody } = {}) {
  const body  = jsonBody === undefined ? "" : JSON.stringify(jsonBody);
  const query = params ? new URLSearchParams(params).toString() : "";
  const ts    = Math.floor(Date.now() / 1000).toString();
  const nonce = crypto.randomUUID().replace(/-/g, "");
  const bodyHash  = crypto.createHash("sha256").update(body).digest("hex");
  const canonical = [method.toUpperCase(), path, query, bodyHash, ts, nonce].join("\\n");
  const sig = crypto.createHmac("sha256", SECRET).update(canonical).digest("hex");
  const url = BASE + path + (query ? "?" + query : "");
  return axios({ method, url, data: body, headers: {
    "X-Api-Key": KEY_ID, "X-Timestamp": ts, "X-Nonce": nonce,
    "X-Signature": sig, "Content-Type": "application/json" } });
}''',
}


def _signed_samples(ep: dict) -> dict:
    """Per-endpoint samples that call the signedRequest() helper from Client setup."""
    method = ep["method"].upper()
    path = re.sub(r"\{[a-z_]+\}", str(ep.get("path_example", "42")), ep["path"])
    body = ep.get("request")
    query = ep.get("query")

    # Python
    py = ["# signed_request() is defined under Authentication -> Client setup."]
    call = [f'resp = signed_request("{method}", "{path}",']
    if query is not None:
        call.append("    params=" + _indent(_fmt(query, "python"), 4) + ",")
    if body is not None:
        call.append("    json_body=" + _indent(_fmt(body, "python"), 4) + ",")
    call.append(")")
    py += call + ["print(resp.status_code, resp.json())"]
    python = "\n".join(py)

    # Django (same helper, credentials from settings)
    dj = ["# signed_request() reads credentials from settings (see Client setup).",
          f'resp = signed_request("{method}", "{path}",']
    if query is not None:
        dj.append("    params=" + _indent(_fmt(query, "python"), 4) + ",")
    if body is not None:
        dj.append("    json_body=" + _indent(_fmt(body, "python"), 4) + ",")
    dj += [")", "resp.raise_for_status()", "data = resp.json()"]
    django = "\n".join(dj)

    # Laravel
    php = ["// signedRequest() is defined under Authentication -> Client setup."]
    if query is not None:
        php.append(f"$response = signedRequest('{method}', '{path}', [")
        php.append("    'params' => " + _indent(_fmt(query, "php"), 4) + ",")
        php.append("]);")
    elif body is not None:
        php.append(f"$response = signedRequest('{method}', '{path}', [")
        php.append("    'json' => " + _indent(_fmt(body, "php"), 4) + ",")
        php.append("]);")
    else:
        php.append(f"$response = signedRequest('{method}', '{path}');")
    php.append("return $response->json();")
    laravel = "\n".join(php)

    # Node
    js = ["// signedRequest() is defined under Authentication -> Client setup."]
    opts = []
    if query is not None:
        opts.append("params: " + _fmt(query, "node"))
    if body is not None:
        opts.append("jsonBody: " + _indent(_fmt(body, "node"), 2))
    if opts:
        js.append(f'const {{ data }} = await signedRequest("{method}", "{path}", {{')
        js.append("  " + ", ".join(opts) + ",")
        js.append("});")
    else:
        js.append(f'const {{ data }} = await signedRequest("{method}", "{path}");')
    js.append("console.log(data);")
    node = "\n".join(js)

    return {"python": python, "django": django, "laravel": laravel, "node": node}


# ── The spec ───────────────────────────────────────────────────────────────────
GROUPS = [
    {
        "id": "auth",
        "title": "Authentication",
        "blurb": "Server-to-server clients authenticate by SIGNING each request with an API "
                 "key (HMAC-SHA256) — the most secure scheme: the secret never travels over "
                 "the network and captured requests can't be replayed. Create a key in the "
                 "panel under API Keys (the secret is shown once), drop the Client-setup "
                 "helper below into your code, and every endpoint sample calls it. (Browser "
                 "dashboards may instead use a short-lived Bearer token from Login.) "
                 "Each data request counts against your API plan's quota; when it's exhausted "
                 "the API replies 402 until the 30-day window resets. If an admin has set an IP "
                 "allow-list on your account, signed requests are only accepted from those IPs. "
                 "Error codes: 401 (missing/invalid auth), 402 (quota exhausted), 403 (IP not "
                 "allowed or capability missing), 429 (per-minute rate limit exceeded).",
        "endpoints": [
            {
                "id": "login", "method": "POST", "path": "/api/auth/login", "auth": False,
                "title": "Login", "desc": "Exchange account credentials for a JWT.",
                "params": [
                    ("email", "body · string", "Account email"),
                    ("password", "body · string", "Account password"),
                ],
                "request": {"email": "user@company.com", "password": "••••••••"},
                "response": {"access_token": "eyJhbGciOi...", "refresh_token": "eyJhbGciOi...",
                             "token_type": "bearer"},
            },
            {
                "id": "api-keys", "method": "POST", "path": "/api/auth/api-keys", "auth": True,
                "title": "Create API key", "desc": "Server-to-server key. The secret is shown ONCE.",
                "params": [("label", "body · string", "Human label for the key")],
                "request": {"label": "production-server"},
                "response": {"key_id": "mk_live_8f2a...", "secret": "sk_live_3b91...",
                             "label": "production-server"},
            },
        ],
    },
    {
        "id": "single",
        "title": "Single",
        "blurb": "Synchronous calls for one player / one code. The bot account is chosen "
                 "server-side (rotated across accounts, respecting each one's per-minute "
                 "limit) — you never pass an account id.",
        "endpoints": [
            {
                "id": "player-info", "method": "GET", "path": "/api/player-info", "auth": True,
                "title": "Player lookup", "desc": "Resolve a PUBG Mobile UID to a username.",
                "params": [
                    ("player_id", "query · string", "PUBG Mobile UID"),
                    ("country_code", "query · string", "Storefront code, e.g. bd"),
                ],
                "query": {"player_id": "25849080551179560", "country_code": "bd"},
                "response": {"success": True,
                             "player": {"player_id": "25849080551179560", "username": "ProGamer",
                                        "zone_id": ""}},
            },
            {
                "id": "code-status", "method": "POST", "path": "/api/code-status", "auth": True,
                "title": "Code status", "desc": "Is a code valid / used / expired / invalid? Does NOT redeem. A used code our app redeemed also returns redeemed_to (see Code vault).",
                "params": [
                    ("country_code", "body · string", "Storefront code"),
                    ("player_id", "body · string", "Player UID the code belongs to"),
                    ("pin_code", "body · string", "UC redeem pin code"),
                ],
                "request": {"country_code": "bd",
                            "player_id": "25849080551179560", "pin_code": "ABCD-EFGH-IJKL"},
                "response": {"success": True, "status": "valid", "message": "Redeem code is valid.",
                             "username": "ProGamer", "product_name": "60 UC"},
                "status_values": "player_not_found · invalid · used · expired · valid",
            },
            {
                "id": "redeem-now", "method": "POST", "path": "/api/redeem-now", "auth": True,
                "title": "Redeem (all-in-one)",
                "desc": "Look up the player, validate the code, and redeem — in one call.",
                "params": [
                    ("country_code", "body · string", "Storefront code"),
                    ("player_id", "body · string", "Player UID"),
                    ("pin_code", "body · string", "UC redeem pin code"),
                ],
                "request": {"country_code": "bd",
                            "player_id": "25849080551179560", "pin_code": "ABCD-EFGH-IJKL"},
                "response": {"success": True, "status": "redeemed",
                             "message": "Redeemed successfully.", "username": "ProGamer",
                             "product_name": "60 UC"},
                "status_values": "player_not_found · invalid · used · expired · redeemed · failed",
            },
        ],
    },
    {
        "id": "bulk",
        "title": "Bulk",
        "blurb": "Asynchronous jobs for many players / many codes. The call returns a job "
                 "immediately; get results by polling GET /api/bulk/jobs/{id} or by setting "
                 "webhook_url (the signed result is POSTed when the job finishes).",
        "endpoints": [
            {
                "id": "bulk-player-info", "method": "POST", "path": "/api/bulk/player-info", "auth": True,
                "title": "Bulk player lookup", "desc": "Look up many UIDs.",
                "params": [
                    ("country_code", "body · string", "Storefront code"),
                    ("player_ids", "body · string[]", "UIDs to look up"),
                    ("webhook_url", "body · string?", "Optional: POST the result here when done"),
                ],
                "request": {"country_code": "bd",
                            "player_ids": ["25849080551179560", "51234567890123456"],
                            "webhook_url": "https://you.example.com/hooks/midasbuy"},
                "response": {"job_id": 42, "job_type": "player_info", "status": "pending",
                             "total_items": 2, "progress_percent": 0},
            },
            {
                "id": "bulk-code-status", "method": "POST", "path": "/api/bulk/code-status", "auth": True,
                "title": "Bulk code status", "desc": "One player, many codes. Does NOT redeem.",
                "params": [
                    ("country_code", "body · string", "Storefront code"),
                    ("player_id", "body · string", "Player UID"),
                    ("pin_codes", "body · string[]", "UC redeem pin codes"),
                    ("webhook_url", "body · string?", "Optional completion webhook"),
                ],
                "request": {"country_code": "bd", "player_id": "25849080551179560",
                            "pin_codes": ["ABCD-EFGH-IJKL", "WXYZ-1234-5678"]},
                "response": {"job_id": 43, "job_type": "validate", "status": "pending",
                             "total_items": 2, "progress_percent": 0},
            },
            {
                "id": "bulk-redeem", "method": "POST", "path": "/api/bulk/redeem", "auth": True,
                "title": "Bulk redeem", "desc": "One player, redeem many codes.",
                "params": [
                    ("country_code", "body · string", "Storefront code"),
                    ("player_id", "body · string", "Player UID"),
                    ("pin_codes", "body · string[]", "UC redeem pin codes"),
                    ("webhook_url", "body · string?", "Optional completion webhook"),
                ],
                "request": {"country_code": "bd", "player_id": "25849080551179560",
                            "pin_codes": ["ABCD-EFGH-IJKL", "WXYZ-1234-5678"]},
                "response": {"job_id": 44, "job_type": "redeem", "status": "pending",
                             "total_items": 2, "progress_percent": 0},
            },
            {
                "id": "bulk-job", "method": "GET", "path": "/api/bulk/jobs/{job_id}", "auth": True,
                "path_example": 44,
                "title": "Poll a job", "desc": "Status + a valid/invalid split + every item. "
                        "`invalid` holds every item that did not succeed (used, expired "
                        "or wrong code, unknown player, or an error); its `message` says why.",
                "params": [("job_id", "path · int", "Job id returned by a bulk call")],
                "response": {
                    "job_id": 44, "status": "completed", "total_items": 2,
                    "succeeded_items": 1, "failed_items": 1, "progress_percent": 100,
                    "valid": [{"player_id": "25849080551179560", "pin_code": "ABCD-EFGH-IJKL",
                               "success": True, "message": "Redeemed successfully.",
                               "product_name": "60 UC"}],
                    "invalid": [{"player_id": "25849080551179560", "pin_code": "WXYZ-1234-5678",
                                 "success": False, "message": "Redeem code is already used."}],
                },
            },
        ],
    },
]


# ── Code vault (shared with partner_api_docs.py) ───────────────────────────────
_EX_CODE = {"id": 17, "code": "••••Z8Z4", "code_last4": "Z8Z4", "label": "Batch A",
            "country_code": "bd", "status": "used", "product_name": "60 UC",
            "checked_at": "2026-10-15T11:20:04Z",
            "redeemed_to": {"player_id": "5421364032", "username": "ErrorSajal",
                            "redeemed_at": "2026-10-15T11:20:04Z"},
            "created_at": "2026-10-15T09:00:00Z"}

CODE_VAULT_GROUP = {
    "id": "codes",
    "title": "Code vault",
    "blurb": "Store your codes with us, then list them by status, check them, redeem them "
             "and see each code's history. Codes are stored encrypted and listed masked "
             "(last 4 characters); GET /api/codes/{code_id} returns the full code. The vault "
             "belongs to the billed client (X-Client-Id). Partners see their own and their "
             "clients' codes. Any code our app redeems is remembered, so a later check of a "
             "used code includes redeemed_to (the player it went to) when you may see it. "
             "Midasbuy itself never reveals that, so it only works for codes redeemed "
             "through this service. Storing, listing and history are free; check and "
             "redeem cost 1 request each.",
    "endpoints": [
        {
            "id": "codes-store", "method": "POST", "path": "/api/codes", "auth": True,
            "title": "Store codes", "desc": "Add up to 1000 codes. Duplicates are skipped.",
            "params": [
                ("codes", "body · string[]", "Codes to store"),
                ("label", "body · string?", "Optional note"),
                ("country_code", "body · string?", "Storefront (default bd)"),
            ],
            "request": {"codes": ["EdtcgQsZ2n26n2V8Z4", "ABCD-EFGH-IJKL"], "label": "Batch A",
                        "country_code": "bd"},
            "response": {"added": 2, "duplicates": [], "skipped_empty": 0,
                         "codes": [{**_EX_CODE, "status": "unchecked", "redeemed_to": None}]},
        },
        {
            "id": "codes-list", "method": "GET", "path": "/api/codes", "auth": True,
            "title": "List codes", "desc": "Filter by status; counts per status included.",
            "params": [
                ("status", "query · string?", "unused · unchecked · valid · used · expired · invalid"),
                ("search", "query · string?", "Last 4 characters, label, or redeemed player id"),
                ("client_ref", "query · string?", "Partners/admins: one client's codes"),
                ("page", "query · int?", "Default 1"),
                ("page_size", "query · int?", "Default 50, max 200"),
            ],
            "query": {"status": "unused", "page": 1},
            "response": {"items": [_EX_CODE], "total": 1, "page": 1, "page_size": 50,
                         "counts": {"unchecked": 3, "valid": 5, "used": 12}},
        },
        {
            "id": "codes-get", "method": "GET", "path": "/api/codes/{code_id}", "auth": True,
            "path_example": 17,
            "title": "Code detail + history", "desc": "Full code and everything that happened to it.",
            "params": [("code_id", "path · int", "Id from the list")],
            "response": {**_EX_CODE, "code": "EdtcgQsZ2n26n2V8Z4", "history": [
                {"at": "2026-10-15T11:20:04Z", "kind": "redeemed", "source": "vault",
                 "by": {"name": "Acme Store", "client_ref": "cl_3f8a9b2c1d4e5f60"},
                 "player_id": "5421364032", "username": "ErrorSajal", "product_name": "60 UC",
                 "order_no": "-CART1001-20261015-H8D39Ockh5W8", "status": "redeemed",
                 "message": ""},
                {"at": "2026-10-15T09:00:00Z", "kind": "added", "source": "api", "by": None,
                 "player_id": "", "username": "", "product_name": "", "order_no": "",
                 "status": "", "message": ""}]},
        },
        {
            "id": "codes-check", "method": "POST", "path": "/api/codes/{code_id}/check",
            "auth": True, "path_example": 17,
            "title": "Check a stored code",
            "desc": "Asks Midasbuy whether it is valid / used / expired / invalid and updates "
                    "the vault.",
            "params": [
                ("player_id", "body · string?", "Player to check against (Midasbuy checks per "
                                                "player); default is the server's check player"),
                ("country_code", "body · string?", "Default: the code's storefront"),
            ],
            "request": {"player_id": "5421364032"},
            "response": {"status": "used", "message": "Redeem code is already used.",
                         "product_name": "", "redeemed_to": _EX_CODE["redeemed_to"],
                         "code": _EX_CODE},
        },
        {
            "id": "codes-redeem", "method": "POST", "path": "/api/codes/{code_id}/redeem",
            "auth": True, "path_example": 17,
            "title": "Redeem a stored code",
            "desc": "Lookup + validate + redeem to a player. 409 if the code is used or "
                    "already being redeemed.",
            "params": [
                ("player_id", "body · string", "PUBG Mobile UID to receive the UC"),
                ("country_code", "body · string?", "Default: the code's storefront"),
            ],
            "request": {"player_id": "5421364032"},
            "response": {"success": True, "status": "redeemed", "message": "Redeemed successfully.",
                         "username": "ErrorSajal", "product_name": "60 UC",
                         "redeemed_to": _EX_CODE["redeemed_to"], "code": _EX_CODE},
            "status_values": "player_not_found · invalid · used · expired · redeemed · failed",
        },
        {
            "id": "codes-lookup", "method": "POST", "path": "/api/codes/lookup", "auth": True,
            "title": "Look up any code",
            "desc": "Stored copies, redeemed_to and history for any code, stored or not. "
                    "Doesn't call Midasbuy (free).",
            "params": [("code", "body · string", "The code")],
            "request": {"code": "EdtcgQsZ2n26n2V8Z4"},
            "response": {"code": "••••Z8Z4", "stored": [_EX_CODE],
                         "redeemed_to": {**_EX_CODE["redeemed_to"], "product_name": "60 UC"},
                         "history": []},
        },
        {
            "id": "codes-delete", "method": "DELETE", "path": "/api/codes/{code_id}", "auth": True,
            "path_example": 17,
            "title": "Remove a code", "desc": "Deletes it from the vault; its history is kept.",
            "params": [("code_id", "path · int", "Id from the list")],
            "response": {"deleted": True, "id": 17},
        },
    ],
}
GROUPS.append(CODE_VAULT_GROUP)


def build_context() -> dict:
    """Expand the spec into render-ready data (adds samples + pretty JSON)."""
    # Data endpoints are documented with the secure HMAC-signed helper; the auth
    # endpoints (login / key creation) use plain calls since you have no key yet.
    signed_groups = {"single", "bulk", "codes"}
    groups = []
    for g in GROUPS:
        eps = []
        for ep in g["endpoints"]:
            sampler = _signed_samples if (g["id"] in signed_groups and ep.get("auth", True)) else _samples
            eps.append({
                **ep,
                "params": ep.get("params", []),
                "request_pretty": _pretty(ep["request"]) if ep.get("request") is not None else None,
                "query_pretty": _pretty(ep["query"]) if ep.get("query") is not None else None,
                "response_pretty": _pretty(ep["response"]) if ep.get("response") is not None else None,
                "samples": sampler(ep),
            })
        groups.append({**g, "endpoints": eps})
    return {"groups": groups, "host": HOST, "client_setup": CLIENT_SETUP}
