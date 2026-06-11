"""
No-browser request path for Midasbuy APIs.

`encrypt_msg` is produced by the REAL window.xMidas "Chaos VM" whitebox cipher,
run headless in Node (see accounts/services/xmidas_node.py + xmidas/). A previous
attempt reimplemented it as AES-256-CBC keyed by the ctoken; Midasbuy rejected
that because the production value is the VM output, not a token-derived AES
transform — so we run the actual VM instead.

    encrypt_msg = btoa(String.fromCharCode(...hexToBytes(window.xMidas({d: json}))))
    ctoken      = <input id="xMidasToken">.value   (rotates per session)
    ctoken_ver  = <input id="xMidasVersion">.value

publicParams mirrors the ei() function from 91.79b63beb.bundle.js module 36453.
They are captured once during login (page_data.json) and merged into every payload.

NOTE: the transport half (sending the request) may be 403'd by Midasbuy's edge
for non-browser clients depending on IP/cookies. Callers keep a browser fallback.
"""
import base64
import json
import logging
import os
import random
from typing import Optional

import httpx

from accounts.services.xmidas_node import encrypt_payload as _node_encrypt

logger = logging.getLogger(__name__)

_PF = "mds_pc_browser-yy-android-midasweb-midasbuy-self.midasbuy_saas"


def build_encrypted_payload(payload: dict, ctoken_hex: str, ctoken_ver: str) -> Optional[dict]:
    """Encrypt the payload with the real xMidas VM (via Node). None on failure."""
    out = _node_encrypt(payload, ctoken_hex, ctoken_ver)
    if not out:
        return None
    return {
        "encrypt_msg": out["encrypt_msg"],
        "ctoken_ver":  out["ctoken_ver"],
        "ctoken":      out["ctoken"],
    }


def build_public_params(page_data: dict, country_code: str = "bd") -> dict:
    """Build the publicParams dict that mirrors ei() in the Midasbuy SPA bundle."""
    device_id = page_data.get("midasbuyDeviceId", "")
    muid      = page_data.get("midasuid", "")
    tdrc_fp   = page_data.get("uuidCookie", "")

    cgi_extend_obj = json.dumps(
        {"device_id": device_id, "pagetoken": "", "tdrc_fp": tdrc_fp, "muid": muid},
        separators=(",", ":"),
    )
    cgi_extend = base64.b64encode(cgi_extend_obj.encode()).decode()
    drm_info   = base64.b64encode(b"{}").decode()

    return {
        "appid":         page_data.get("appid", "1900000047"),
        "pf":            _PF,
        "zoneid":        page_data.get("zoneid", "1"),
        "country":       (page_data.get("country") or country_code or "bd").upper(),
        "device_id":     device_id,
        "pagetoken":     "",
        "tdrc_fp":       tdrc_fp,
        "muid":          muid,
        "cgi_extend":    cgi_extend,
        "drm_info":      drm_info,
        "midasbuyArea":  page_data.get("midasbuyArea", ""),
        "shopcode":      "",
        "buyType":       "",
        "midas_sdk":     "1",
        "currency_type": page_data.get("currency_type", "USD"),
        "_id":           random.random(),
        "sc":            "",
        "from":          "",
        "task_token":    "",
    }


def call_api_python(
    payload: dict,
    endpoint: str,
    storage_state_path: str,
    country_code: str = "bd",
    xmidas_token: str = "",
    page_data: Optional[dict] = None,
) -> Optional[dict]:
    """
    Encrypt the full merged payload in Python and POST to Midasbuy via httpx.

    This is faster and more reliable than the browser-based approach because
    it avoids the window.xMidas availability race condition entirely.
    """
    if not xmidas_token:
        return None

    # Extract cookies that apply to www.midasbuy.com
    with open(storage_state_path, encoding="utf-8") as f:
        ss = json.load(f)

    cookies = {}
    uuid_cookie = ""
    for c in ss.get("cookies", []):
        name  = c.get("name", "")
        value = c.get("value", "")
        if not name or not value:
            continue
        domain = c.get("domain", "")
        if "midasbuy.com" in domain:
            cookies[name] = value
            if name == "UUID":
                uuid_cookie = value

    # Use UUID cookie from stored session as tdrc_fp if page_data doesn't have it
    effective_page_data = dict(page_data or {})
    if not effective_page_data.get("uuidCookie") and uuid_cookie:
        effective_page_data["uuidCookie"] = uuid_cookie

    public_params = build_public_params(effective_page_data, country_code)
    full_payload  = {**public_params, **payload}  # payload wins on overlap

    ctoken_ver = effective_page_data.get("xMidasVersion") or "1.0.1"
    body = build_encrypted_payload(full_payload, xmidas_token, ctoken_ver)
    if body is None:
        logger.warning("[XMIDAS-NODE] could not build encrypt_msg — caller should fall back")
        return None

    url = f"https://www.midasbuy.com{endpoint}"
    headers = {
        "Content-Type":    "application/json",
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent":      (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Origin":          "https://www.midasbuy.com",
        "Referer":         f"https://www.midasbuy.com/midasbuy/{country_code}/redeem/pubgm",
        "Sec-Fetch-Dest":  "empty",
        "Sec-Fetch-Mode":  "cors",
        "Sec-Fetch-Site":  "same-origin",
        "Sec-Ch-Ua":       '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    }

    # Prefer a browser-impersonating TLS client: Midasbuy's edge 403s plain
    # HTTP clients on TLS fingerprint. curl_cffi (impersonate=chrome) sends a
    # real Chrome ClientHello; fall back to httpx if it is unavailable.
    try:
        from curl_cffi import requests as _creq  # type: ignore

        resp = _creq.post(
            url, json=body, headers=headers, cookies=cookies,
            impersonate="chrome120", timeout=30,
        )
        if resp.status_code == 403:
            logger.warning("[XMIDAS-NODE] transport 403 (edge anti-bot) endpoint=%s", endpoint)
            return None
        return resp.json()
    except ImportError:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.post(url, json=body, headers=headers, cookies=cookies)
            if resp.status_code == 403:
                logger.warning("[XMIDAS-NODE] transport 403 (httpx) endpoint=%s", endpoint)
                return None
            resp.raise_for_status()
            return resp.json()
