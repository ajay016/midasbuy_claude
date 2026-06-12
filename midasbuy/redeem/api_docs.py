"""
Single source of truth for the custom /docs API reference.

Each endpoint is described once (method, path, params, request/response example).
From that we render the reference AND generate idiomatic code samples for
Python (requests), Django, Laravel (PHP), and Node.js (axios) — so the docs and
the examples can never drift apart. Add an endpoint here and all four samples are
produced automatically.
"""
import json

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


# ── The spec ───────────────────────────────────────────────────────────────────
GROUPS = [
    {
        "id": "auth",
        "title": "Authentication",
        "blurb": "Get a Bearer token with email + password, or create a server-to-server "
                 "API key. Every data endpoint requires the token (or a signed request).",
        "endpoints": [
            {
                "id": "login", "method": "POST", "path": "/api/auth/login", "auth": False,
                "title": "Login", "desc": "Exchange merchant credentials for a JWT.",
                "params": [
                    ("email", "body · string", "Merchant email"),
                    ("password", "body · string", "Merchant password"),
                ],
                "request": {"email": "merchant@company.com", "password": "••••••••"},
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
        "blurb": "Synchronous calls for one player / one code.",
        "endpoints": [
            {
                "id": "player-info", "method": "GET", "path": "/api/player-info", "auth": True,
                "title": "Player lookup", "desc": "Resolve a PUBG Mobile UID to a username.",
                "params": [
                    ("player_id", "query · string", "PUBG Mobile UID"),
                    ("country_code", "query · string", "Storefront code, e.g. bd"),
                    ("account_id", "query · int", "MidasbuyAccount to use"),
                ],
                "query": {"player_id": "25849080551179560", "country_code": "bd", "account_id": 1},
                "response": {"success": True,
                             "player": {"player_id": "25849080551179560", "username": "ProGamer",
                                        "zone_id": ""}},
            },
            {
                "id": "code-status", "method": "POST", "path": "/api/code-status", "auth": True,
                "title": "Code status", "desc": "Is a code valid / used / invalid? Does NOT redeem.",
                "params": [
                    ("account_id", "body · int", "MidasbuyAccount to use"),
                    ("country_code", "body · string", "Storefront code"),
                    ("player_id", "body · string", "Player UID the code belongs to"),
                    ("pin_code", "body · string", "UC redeem pin code"),
                ],
                "request": {"account_id": 1, "country_code": "bd",
                            "player_id": "25849080551179560", "pin_code": "ABCD-EFGH-IJKL"},
                "response": {"success": True, "status": "valid", "message": "Redeem code is valid.",
                             "username": "ProGamer", "product_name": "60 UC"},
                "status_values": "player_not_found · invalid · used · valid",
            },
            {
                "id": "redeem-now", "method": "POST", "path": "/api/redeem-now", "auth": True,
                "title": "Redeem (all-in-one)",
                "desc": "Look up the player, validate the code, and redeem — in one call.",
                "params": [
                    ("account_id", "body · int", "MidasbuyAccount to use"),
                    ("country_code", "body · string", "Storefront code"),
                    ("player_id", "body · string", "Player UID"),
                    ("pin_code", "body · string", "UC redeem pin code"),
                ],
                "request": {"account_id": 1, "country_code": "bd",
                            "player_id": "25849080551179560", "pin_code": "ABCD-EFGH-IJKL"},
                "response": {"success": True, "status": "redeemed",
                             "message": "Redeemed successfully.", "username": "ProGamer",
                             "product_name": "60 UC"},
                "status_values": "player_not_found · invalid · used · redeemed · failed",
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
                    ("account_id", "body · int", "MidasbuyAccount to use"),
                    ("country_code", "body · string", "Storefront code"),
                    ("player_ids", "body · string[]", "UIDs to look up"),
                    ("webhook_url", "body · string?", "Optional: POST the result here when done"),
                ],
                "request": {"account_id": 1, "country_code": "bd",
                            "player_ids": ["25849080551179560", "51234567890123456"],
                            "webhook_url": "https://you.example.com/hooks/midasbuy"},
                "response": {"job_id": 42, "job_type": "player_info", "status": "pending",
                             "total_items": 2, "progress_percent": 0},
            },
            {
                "id": "bulk-code-status", "method": "POST", "path": "/api/bulk/code-status", "auth": True,
                "title": "Bulk code status", "desc": "One player, many codes. Does NOT redeem.",
                "params": [
                    ("account_id", "body · int", "MidasbuyAccount to use"),
                    ("country_code", "body · string", "Storefront code"),
                    ("player_id", "body · string", "Player UID"),
                    ("pin_codes", "body · string[]", "UC redeem pin codes"),
                    ("webhook_url", "body · string?", "Optional completion webhook"),
                ],
                "request": {"account_id": 1, "country_code": "bd", "player_id": "25849080551179560",
                            "pin_codes": ["ABCD-EFGH-IJKL", "WXYZ-1234-5678"]},
                "response": {"job_id": 43, "job_type": "validate", "status": "pending",
                             "total_items": 2, "progress_percent": 0},
            },
            {
                "id": "bulk-redeem", "method": "POST", "path": "/api/bulk/redeem", "auth": True,
                "title": "Bulk redeem", "desc": "One player, redeem many codes.",
                "params": [
                    ("account_id", "body · int", "MidasbuyAccount to use"),
                    ("country_code", "body · string", "Storefront code"),
                    ("player_id", "body · string", "Player UID"),
                    ("pin_codes", "body · string[]", "UC redeem pin codes"),
                    ("webhook_url", "body · string?", "Optional completion webhook"),
                ],
                "request": {"account_id": 1, "country_code": "bd", "player_id": "25849080551179560",
                            "pin_codes": ["ABCD-EFGH-IJKL", "WXYZ-1234-5678"]},
                "response": {"job_id": 44, "job_type": "redeem", "status": "pending",
                             "total_items": 2, "progress_percent": 0},
            },
            {
                "id": "bulk-job", "method": "GET", "path": "/api/bulk/jobs/{job_id}", "auth": True,
                "path_example": 44,
                "title": "Poll a job", "desc": "Status + a valid/invalid split + every item.",
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


def build_context() -> dict:
    """Expand the spec into render-ready data (adds samples + pretty JSON)."""
    groups = []
    for g in GROUPS:
        eps = []
        for ep in g["endpoints"]:
            eps.append({
                **ep,
                "params": ep.get("params", []),
                "request_pretty": _pretty(ep["request"]) if ep.get("request") is not None else None,
                "query_pretty": _pretty(ep["query"]) if ep.get("query") is not None else None,
                "response_pretty": _pretty(ep["response"]) if ep.get("response") is not None else None,
                "samples": _samples(ep),
            })
        groups.append({**g, "endpoints": eps})
    return {"groups": groups, "host": HOST}
