"""
Partner API reference (rendered at /docs/partner with the same docs.html template
as the client reference). Single source of truth for the partner endpoints; the
multi-language samples are generated from the formatters in api_docs.py so the two
references can never drift in style.

What's different from the client reference:
  * the signing helper takes an optional ``client_id`` -> sent as the X-Client-Id
    header to attribute a redeem/lookup to one of your clients (not part of the
    signature; the server reads it separately and checks you own that client);
  * a "Manage clients" group for the /api/partner/* self-service endpoints.
"""
from .api_docs import HOST, _fmt, _indent, _pretty, _samples


# ── Partner signing helper (adds optional X-Client-Id) ─────────────────────────
PARTNER_CLIENT_SETUP = {
    "python": '''import hashlib, hmac, json, time, uuid, requests
from urllib.parse import urlencode

BASE   = "''' + HOST + '''"
KEY_ID = "mk_..."   # your PARTNER key (panel -> API Keys)
SECRET = "sk_..."   # shown ONCE when the key was created

def signed_request(method, path, *, params=None, json_body=None, client_id=None):
    """Sign and send one request. Pass client_id to attribute (and meter) the call
    to one of your clients — it is sent as X-Client-Id and is NOT part of the
    signature; the server checks that you own that client."""
    body  = b"" if json_body is None else json.dumps(json_body, separators=(",", ":")).encode()
    query = urlencode(params) if params else ""
    ts    = str(int(time.time()))
    nonce = uuid.uuid4().hex
    canonical = "\\n".join([method.upper(), path, query,
                           hashlib.sha256(body).hexdigest(), ts, nonce])
    sig = hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    headers = {"X-Api-Key": KEY_ID, "X-Timestamp": ts, "X-Nonce": nonce,
               "X-Signature": sig, "Content-Type": "application/json"}
    if client_id:
        headers["X-Client-Id"] = client_id
    url = BASE + path + (("?" + query) if query else "")
    return requests.request(method, url, data=body, headers=headers, timeout=30)''',

    "django": '''import hashlib, hmac, json, time, uuid, requests
from urllib.parse import urlencode
from django.conf import settings   # set MIDASBUY_API_BASE / _KEY_ID / _SECRET

def signed_request(method, path, *, params=None, json_body=None, client_id=None):
    body  = b"" if json_body is None else json.dumps(json_body, separators=(",", ":")).encode()
    query = urlencode(params) if params else ""
    ts, nonce = str(int(time.time())), uuid.uuid4().hex
    canonical = "\\n".join([method.upper(), path, query,
                           hashlib.sha256(body).hexdigest(), ts, nonce])
    sig = hmac.new(settings.MIDASBUY_API_SECRET.encode(), canonical.encode(),
                   hashlib.sha256).hexdigest()
    headers = {"X-Api-Key": settings.MIDASBUY_API_KEY_ID, "X-Timestamp": ts,
               "X-Nonce": nonce, "X-Signature": sig, "Content-Type": "application/json"}
    if client_id:
        headers["X-Client-Id"] = client_id
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

    $headers = ['X-Api-Key' => $keyId, 'X-Timestamp' => $ts, 'X-Nonce' => $nonce,
                'X-Signature' => $sig, 'Content-Type' => 'application/json'];
    if (!empty($opts['client_id'])) { $headers['X-Client-Id'] = $opts['client_id']; }

    $url = $base . $path . ($query ? '?' . $query : '');
    return Http::withHeaders($headers)->withBody($body, 'application/json')
               ->send(strtoupper($method), $url);
}''',

    "node": '''import crypto from "crypto";
import axios from "axios";

const BASE   = "''' + HOST + '''";
const KEY_ID = "mk_...";   // your PARTNER key (panel -> API Keys)
const SECRET = "sk_...";   // shown ONCE when the key was created

export async function signedRequest(method, path, { params, jsonBody, clientId } = {}) {
  const body  = jsonBody === undefined ? "" : JSON.stringify(jsonBody);
  const query = params ? new URLSearchParams(params).toString() : "";
  const ts    = Math.floor(Date.now() / 1000).toString();
  const nonce = crypto.randomUUID().replace(/-/g, "");
  const bodyHash  = crypto.createHash("sha256").update(body).digest("hex");
  const canonical = [method.toUpperCase(), path, query, bodyHash, ts, nonce].join("\\n");
  const sig = crypto.createHmac("sha256", SECRET).update(canonical).digest("hex");
  const headers = { "X-Api-Key": KEY_ID, "X-Timestamp": ts, "X-Nonce": nonce,
    "X-Signature": sig, "Content-Type": "application/json" };
  if (clientId) headers["X-Client-Id"] = clientId;
  const url = BASE + path + (query ? "?" + query : "");
  return axios({ method, url, data: body, headers });
}''',
}


def _psub(ep: dict) -> str:
    """Path with the {client_ref} placeholder replaced by an example value."""
    return ep["path"].replace("{client_ref}", str(ep.get("path_example", "cl_3f8a9b2c1d4e5f60")))


def _partner_signed_samples(ep: dict) -> dict:
    """Samples that call signed_request(); add client_id for attributed calls."""
    method = ep["method"].upper()
    path = _psub(ep)
    body = ep.get("request")
    query = ep.get("query")
    attributed = ep.get("attributed", False)

    # Python
    py = ["# signed_request() is defined under Partner setup.",
          f'resp = signed_request("{method}", "{path}",']
    if query is not None:
        py.append("    params=" + _indent(_fmt(query, "python"), 4) + ",")
    if body is not None:
        py.append("    json_body=" + _indent(_fmt(body, "python"), 4) + ",")
    if attributed:
        py.append('    client_id="cl_<your client_ref>",')
    py += [")", "print(resp.status_code, resp.json())"]
    python = "\n".join(py)

    # Django
    dj = ["# signed_request() reads credentials from settings (see Partner setup).",
          f'resp = signed_request("{method}", "{path}",']
    if query is not None:
        dj.append("    params=" + _indent(_fmt(query, "python"), 4) + ",")
    if body is not None:
        dj.append("    json_body=" + _indent(_fmt(body, "python"), 4) + ",")
    if attributed:
        dj.append('    client_id="cl_<your client_ref>",')
    dj += [")", "resp.raise_for_status()", "data = resp.json()"]
    django = "\n".join(dj)

    # Laravel
    php = ["// signedRequest() is defined under Partner setup."]
    rows = []
    if query is not None:
        rows.append("    'params' => " + _indent(_fmt(query, "php"), 4) + ",")
    if body is not None:
        rows.append("    'json' => " + _indent(_fmt(body, "php"), 4) + ",")
    if attributed:
        rows.append("    'client_id' => 'cl_<your client_ref>',")
    if rows:
        php.append(f"$response = signedRequest('{method}', '{path}', [")
        php += rows
        php.append("]);")
    else:
        php.append(f"$response = signedRequest('{method}', '{path}');")
    php.append("return $response->json();")
    laravel = "\n".join(php)

    # Node
    js = ["// signedRequest() is defined under Partner setup."]
    opts = []
    if query is not None:
        opts.append("params: " + _fmt(query, "node"))
    if body is not None:
        opts.append("jsonBody: " + _indent(_fmt(body, "node"), 2))
    if attributed:
        opts.append('clientId: "cl_<your client_ref>"')
    if opts:
        js.append(f'const {{ data }} = await signedRequest("{method}", "{path}", {{')
        js.append("  " + ", ".join(opts) + ",")
        js.append("});")
    else:
        js.append(f'const {{ data }} = await signedRequest("{method}", "{path}");')
    js.append("console.log(data);")
    node = "\n".join(js)

    return {"python": python, "django": django, "laravel": laravel, "node": node}


# ── Reusable example payloads ──────────────────────────────────────────────────
_CLIENT_OBJ = {
    "client_ref": "cl_3f8a9b2c1d4e5f60",
    "name": "Acme Store",
    "email": "client+ab12cd34@partner-7.local",
    "allowed_ips": "203.0.113.7",
    "is_active": True,
    "subscriptions": {
        "panel": None,
        "api": {"active": True, "used": 128, "limit": 5000,
                "unlimited": False, "period_end": "2026-07-17T00:00:00+00:00"},
    },
}


# ── The partner spec ───────────────────────────────────────────────────────────
GROUPS = [
    {
        "id": "auth",
        "title": "Authentication & attribution",
        "blurb": "Partners authenticate exactly like clients — sign every request with your own "
                 "API key (HMAC-SHA256). What's different is ATTRIBUTION: to bill a redeem/lookup "
                 "to one of your clients, add the header X-Client-Id: <client_ref> (the value "
                 "returned when you create the client). That request is then metered and "
                 "rate-limited against THAT client's subscription. Calls WITHOUT X-Client-Id are "
                 "billed to you — your own meter is unlimited but still counted. If an admin set "
                 "an IP allow-list on your account, signed requests are only accepted from those "
                 "IPs. Errors: 401 (auth), 402 (the client's quota is exhausted), 403 (IP not "
                 "allowed, you're not a partner, or X-Client-Id is unknown / not one of yours), "
                 "429 (per-minute rate limit).",
        "endpoints": [
            {
                "id": "login", "method": "POST", "path": "/api/auth/login", "auth": False,
                "title": "Login", "desc": "Exchange your account credentials for a JWT (panel use).",
                "params": [
                    ("email", "body · string", "Account email"),
                    ("password", "body · string", "Account password"),
                ],
                "request": {"email": "partner@company.com", "password": "••••••••"},
                "response": {"access_token": "eyJhbGciOi...", "refresh_token": "eyJhbGciOi...",
                             "token_type": "bearer"},
            },
            {
                "id": "api-keys", "method": "POST", "path": "/api/auth/api-keys", "auth": True,
                "title": "Create API key", "desc": "Your server-to-server key. The secret is shown ONCE.",
                "params": [("label", "body · string", "Human label for the key")],
                "request": {"label": "partner-prod"},
                "response": {"key_id": "mk_live_8f2a...", "secret": "sk_live_3b91...",
                             "label": "partner-prod"},
            },
        ],
    },
    {
        "id": "clients",
        "title": "Manage clients",
        "blurb": "Create and manage your own clients entirely over the API — no panel login. "
                 "Every call is scoped to you: you can only see and modify clients you own. The "
                 "client_ref returned by Create is the X-Client-Id you send when redeeming for "
                 "that client. Set request_limit for a capped plan, or unlimited:true for an "
                 "uncapped (still-metered) one.",
        "endpoints": [
            {
                "id": "clients-list", "method": "GET", "path": "/api/partner/clients", "auth": True,
                "title": "List clients", "desc": "Every client you own, with their usage meters.",
                "response": {"clients": [_CLIENT_OBJ], "count": 1},
            },
            {
                "id": "clients-create", "method": "POST", "path": "/api/partner/clients", "auth": True,
                "title": "Create client",
                "desc": "Create a client under you. The returned client_ref is the X-Client-Id "
                        "you send on redeem/lookup calls for this client.",
                "params": [
                    ("name", "body · string", "Display name for the client"),
                    ("email", "body · string?", "Optional; a unique synthetic one is made if omitted"),
                    ("allowed_ips", "body · string?", "Optional IP/CIDR allow-list for this client"),
                    ("request_limit", "body · int?", "API quota per 30 days (omit for none yet)"),
                    ("unlimited", "body · bool?", "true = uncapped api plan (still metered)"),
                ],
                "request": {"name": "Acme Store", "allowed_ips": "203.0.113.7",
                            "request_limit": 5000},
                "response": _CLIENT_OBJ,
            },
            {
                "id": "clients-get", "method": "GET", "path": "/api/partner/clients/{client_ref}",
                "auth": True, "path_example": "cl_3f8a9b2c1d4e5f60",
                "title": "Get client", "desc": "One client's detail and usage.",
                "params": [("client_ref", "path · string", "The client's identifier")],
                "response": _CLIENT_OBJ,
            },
            {
                "id": "clients-update", "method": "PATCH", "path": "/api/partner/clients/{client_ref}",
                "auth": True, "path_example": "cl_3f8a9b2c1d4e5f60",
                "title": "Update client", "desc": "Change a client's name, IP allow-list, or active state.",
                "params": [
                    ("client_ref", "path · string", "The client's identifier"),
                    ("name", "body · string?", "New display name"),
                    ("allowed_ips", "body · string?", "Replace the IP/CIDR allow-list (empty = any)"),
                    ("is_active", "body · bool?", "false disables the client"),
                ],
                "request": {"allowed_ips": "203.0.113.7, 198.51.100.0/24"},
                "response": _CLIENT_OBJ,
            },
            {
                "id": "clients-subscription", "method": "POST",
                "path": "/api/partner/clients/{client_ref}/subscription",
                "auth": True, "path_example": "cl_3f8a9b2c1d4e5f60",
                "title": "Set subscription", "desc": "Grant or renew a client's plan (resets the window).",
                "params": [
                    ("client_ref", "path · string", "The client's identifier"),
                    ("plan", "body · string", "api (default) or panel"),
                    ("request_limit", "body · int?", "Quota per period (omit + unlimited:true for uncapped)"),
                    ("unlimited", "body · bool?", "true = uncapped (still metered)"),
                ],
                "request": {"plan": "api", "request_limit": 10000},
                "response": _CLIENT_OBJ,
            },
        ],
    },
    {
        "id": "redeem",
        "title": "Redeem for a client",
        "blurb": "The normal lookup/redeem endpoints, called with YOUR key plus the "
                 "X-Client-Id header to attribute and meter the call against that client. Omit "
                 "X-Client-Id to bill yourself (unlimited, still counted). The bot account is "
                 "still chosen server-side. These mirror the client reference exactly — only the "
                 "X-Client-Id header is added.",
        "endpoints": [
            {
                "id": "p-player-info", "method": "GET", "path": "/api/player-info", "auth": True,
                "attributed": True,
                "title": "Player lookup", "desc": "Resolve a PUBG Mobile UID to a username.",
                "params": [
                    ("X-Client-Id", "header · string", "client_ref to attribute & meter this call"),
                    ("player_id", "query · string", "PUBG Mobile UID"),
                    ("country_code", "query · string", "Storefront code, e.g. bd"),
                ],
                "query": {"player_id": "25849080551179560", "country_code": "bd"},
                "response": {"success": True,
                             "player": {"player_id": "25849080551179560", "username": "ProGamer",
                                        "zone_id": ""}},
            },
            {
                "id": "p-code-status", "method": "POST", "path": "/api/code-status", "auth": True,
                "attributed": True,
                "title": "Code status", "desc": "Is a code valid / used / invalid? Does NOT redeem.",
                "params": [
                    ("X-Client-Id", "header · string", "client_ref to attribute & meter this call"),
                    ("country_code", "body · string", "Storefront code"),
                    ("player_id", "body · string", "Player UID the code belongs to"),
                    ("pin_code", "body · string", "UC redeem pin code"),
                ],
                "request": {"country_code": "bd", "player_id": "25849080551179560",
                            "pin_code": "ABCD-EFGH-IJKL"},
                "response": {"success": True, "status": "valid", "message": "Redeem code is valid.",
                             "username": "ProGamer", "product_name": "60 UC"},
                "status_values": "player_not_found · invalid · used · valid",
            },
            {
                "id": "p-redeem-now", "method": "POST", "path": "/api/redeem-now", "auth": True,
                "attributed": True,
                "title": "Redeem (all-in-one)",
                "desc": "Look up the player, validate the code, and redeem — in one call.",
                "params": [
                    ("X-Client-Id", "header · string", "client_ref to attribute & meter this call"),
                    ("country_code", "body · string", "Storefront code"),
                    ("player_id", "body · string", "Player UID"),
                    ("pin_code", "body · string", "UC redeem pin code"),
                ],
                "request": {"country_code": "bd", "player_id": "25849080551179560",
                            "pin_code": "ABCD-EFGH-IJKL"},
                "response": {"success": True, "status": "redeemed",
                             "message": "Redeemed successfully.", "username": "ProGamer",
                             "product_name": "60 UC"},
                "status_values": "player_not_found · invalid · used · redeemed · failed",
            },
        ],
    },
]


def build_partner_context() -> dict:
    """Expand the partner spec into render-ready data for docs.html."""
    groups = []
    for g in GROUPS:
        eps = []
        for ep in g["endpoints"]:
            # Auth group uses plain Bearer samples (you have no key yet); everything
            # else is HMAC-signed via the partner helper.
            sampler = _samples if g["id"] == "auth" else _partner_signed_samples
            eps.append({
                **ep,
                "params": ep.get("params", []),
                "request_pretty": _pretty(ep["request"]) if ep.get("request") is not None else None,
                "query_pretty": _pretty(ep["query"]) if ep.get("query") is not None else None,
                "response_pretty": _pretty(ep["response"]) if ep.get("response") is not None else None,
                "samples": sampler(ep),
            })
        groups.append({**g, "endpoints": eps})

    intro_html = (
        f"Base URL <code>{HOST}/api</code>. You're a <b>partner</b>: sign every request with "
        "your own API key, and add <code>X-Client-Id: &lt;client_ref&gt;</code> to attribute a "
        "redeem/lookup to one of your clients. Create and manage your clients under "
        "<b>Manage clients</b>; each client's <code>client_ref</code> is what you send as "
        "<code>X-Client-Id</code>. Your own usage is unlimited but still counted."
    )
    return {
        "groups": groups,
        "host": HOST,
        "client_setup": PARTNER_CLIENT_SETUP,
        "doc_title": "Partner API · Midasbuy",
        "intro_heading": "Partner API Reference",
        "intro_html": intro_html,
        "setup_heading": "Partner setup — request signing",
        "setup_nav_label": "Partner setup (signing)",
        "setup_blurb": (
            "Copy this helper once. It signs like the client helper, plus an optional "
            "<code>client_id</code> that's sent as the <code>X-Client-Id</code> header to "
            "attribute a call to one of your clients. <code>X-Client-Id</code> is NOT part of "
            "the signature — the server reads it separately and verifies you own that client. "
            "Keep <code>SECRET</code> on the server only."
        ),
    }
