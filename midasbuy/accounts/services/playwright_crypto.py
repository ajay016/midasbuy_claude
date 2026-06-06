"""
Makes Midasbuy API calls from inside a live browser session.

How encryption works (from 91.79b63beb.bundle.js + Chaos VM disassembly):
  ctoken      = <input id="xMidasToken">.value
  ctoken_ver  = <input id="xMidasVersion">.value
  encrypt_msg = Base64(hexToBytes(window.xMidas({d: JSON(fullPayload)})))

The Chaos VM is authoritative. A previous AES-only approximation can produce
bytes, but Midasbuy rejects them as an invalid encrypt_msg.

publicParams come from ei() in the bundle:
  appid, pf, zoneid, country, device_id (from __Report_INFO),
  muid (from __Report_INFO), tdrc_fp (= UUID cookie, NOT Forter),
  cgi_extend (base64 of device object), drm_info (base64 of empty obj),
  midasbuyArea, shopcode, buyType, midas_sdk, currency_type, _id (random).

The Chaos VM CDN script is intercepted via page.route and appended with a
configurable:false property lock so React SPA hydration cannot delete
window.xMidas.  The route must be registered BEFORE page.goto.

Note: forterToken / feh-- localStorage keys are NOT read by the Chaos VM
(confirmed by full disassembly — zero references).
"""
import json
import logging
import os
import time
from dataclasses import dataclass, field
from threading import Lock, get_ident
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class _CachedBrowserSession:
    manager: object
    browser: object
    context: object
    page: object
    storage_state_path: str
    country_code: str
    storage_mtime: float
    created_at: float
    last_used: float
    behavior_warmed_at: float = 0.0
    lock: Lock = field(default_factory=Lock)


_SESSION_CACHE: Dict[Tuple[str, str, int], _CachedBrowserSession] = {}
_SESSION_CACHE_LOCK = Lock()
_CACHED_ENDPOINTS = {
    "/interface/getCharac",
    "/interface/shelfProto/shelves_svr/QueryRedeemCodeInfo",
}

# ── Behavioral warming (anti risk-control) ──────────────────────────────────────
# Midasbuy's tdrc.js streams a behavioral heartbeat (mouse coords / click / key
# counts) to /cgi-bin/fp-behv every ~10s, and SUPPRESSES the report entirely when
# there was no real interaction (its msgIsValid check). A headless session that
# loads the page then immediately fetch()es a value-transfer endpoint (redeem)
# emits zero behavior, so the server scores it as a bot and returns
# FLEXIBLE_RISK_CONTROL:graphic. Read-only getCharac is scored leniently and is
# unaffected. Before a value-transfer call we therefore seed genuine pointer /
# scroll / key activity and dwell long enough for at least one *populated*
# heartbeat to fire. Tunable via env so it can be adjusted without code changes.
_BEHAVIOR_REQUIRED_ENDPOINTS = {
    "/interface/shelfProto/shelves_svr/QueryRedeemCodeInfo",
}
_BEHAVIOR_HEARTBEAT_MS = int(os.getenv("MIDASBUY_BEHAVIOR_HEARTBEAT_MS", "11000"))
_BEHAVIOR_REWARM_S     = float(os.getenv("MIDASBUY_BEHAVIOR_REWARM_S", "240"))

# Headless vs headful. Headless Chromium has an atypical canvas/WebGL/GPU
# fingerprint that Tencent's risk control scores as low-trust on value-transfer
# endpoints (redeem) even when behavior looks human — so a graphic captcha is
# forced. On a real desktop, headful (MIDASBUY_HEADFUL=1) uses the real GPU and
# Chrome rendering stack, which is the single biggest fingerprint-trust lever.
_HEADFUL  = os.getenv("MIDASBUY_HEADFUL", "").lower() in ("1", "true", "yes", "on")
_HEADLESS = not _HEADFUL

# Risk/anti-fraud telemetry endpoints — logged so we can confirm tdrc.js is
# actually loading and reporting (empty = warming is a no-op).
_RISK_TELEMETRY_HINTS = (
    "harvestsharp", "fp-behv", "risk_control", "riskcontrol", "tdrc",
    "captcha", "kepler", "forter", "online-metrix", "riskified",
)

# Local copy of the Chaos VM CDN script (served via page.route to avoid CDN latency)
_CHAOS_VM_LOCAL_PATH = os.path.normpath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "..",
        "kEc9hjFh5DQJbz_iPEWrfFxadMVk4PbLDS-5P8jE73pfdUuDwNGKNVZjdEztcHdofAVaHXo6zRGXgLwuvsK_afAEj6w_mKyiUmq-7AesIRU~.js",
    )
)

# Appended to the Chaos VM script (served via page.route).
# Tries to lock window.xMidas immediately; if the VM assigned it async,
# falls back to a 10ms poll so we catch it before SPA hydration deletes it.
_CHAOS_VM_PROTECTION = b"""
;(function(){
    function _lock(fn) {
        try {
            Object.defineProperty(window, 'xMidas', {
                get: function() { return fn; },
                set: function() {},
                configurable: false,
                enumerable: true,
            });
        } catch(e) {}
    }
    var _fn = window.xMidas;
    if (typeof _fn === 'function') { _lock(_fn); return; }
    var _t = setInterval(function() {
        var f = window.xMidas;
        if (typeof f === 'function') { clearInterval(_t); _lock(f); }
    }, 10);
    setTimeout(function() { clearInterval(_t); }, 30000);
})();
"""


def _get_playwright():
    """Prefer patchright (undetected); fall back to standard playwright."""
    try:
        from patchright.sync_api import sync_playwright, TimeoutError as PWTimeout
        logger.debug("[CRYPTO] using patchright")
        return sync_playwright, PWTimeout
    except ImportError:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        logger.warning("[CRYPTO] patchright not installed — falling back to standard playwright")
        return sync_playwright, PWTimeout


# ── Stealth init script ────────────────────────────────────────────────────────
_STEALTH_JS = """
(() => {
    try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch(e) {}

    if (!window.chrome) {
        try {
            Object.defineProperty(window, 'chrome', {
                writable: true, enumerable: true, configurable: false,
                value: {
                    app: { isInstalled: false, getDetails(){}, getIsInstalled(){ return false; },
                           installState(){}, runningState(){ return 'cannot_run'; } },
                    csi(){}, loadTimes(){ return {}; }, runtime: {},
                },
            });
        } catch(e) {}
    }

    try {
        if (!navigator.plugins || navigator.plugins.length === 0) {
            const mk = (n, f) => ({ name: n, filename: f,
                description: 'Portable Document Format', length: 1,
                0: { type: 'application/pdf', suffixes: 'pdf', description: '' } });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [mk('PDF Viewer','internal-pdf-viewer'),
                            mk('Chrome PDF Viewer','internal-pdf-viewer'),
                            mk('Chromium PDF Viewer','internal-pdf-viewer')],
                enumerable: true,
            });
        }
    } catch(e) {}

    try { Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] }); } catch(e) {}
    try { Object.defineProperty(navigator, 'platform',  { get: () => 'Win32' }); } catch(e) {}
    try {
        if (!navigator.hardwareConcurrency || navigator.hardwareConcurrency < 2)
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
    } catch(e) {}

    // Pre-intercept window.xMidas with a capturing setter so the Chaos VM's
    // synchronous assignment is caught before SPA hydration can delete it.
    // Works if this init script runs in the main JS world (standard Playwright
    // behaviour). The configurable:false lock prevents any subsequent deletion.
    try {
        var _xMidasFn = null;
        Object.defineProperty(window, 'xMidas', {
            get: function() { return _xMidasFn; },
            set: function(v) {
                if (typeof v === 'function' && _xMidasFn === null) {
                    _xMidasFn = v;
                    // Re-lock as non-configurable so the SPA cannot delete it
                    try {
                        Object.defineProperty(window, 'xMidas', {
                            get: function() { return _xMidasFn; },
                            set: function() {},
                            configurable: false,
                            enumerable: true,
                        });
                    } catch(e) {}
                }
            },
            configurable: true,
            enumerable: true,
        });
    } catch(e) {}
})();
"""

# ── JS evaluated inside the page ──────────────────────────────────────────────

_JS_CALL_API = """
async ({payloadJson, endpoint, method}) => {
    try {
        // Wait for xMidasToken (up to 15s)
        let tokenWait = 0;
        while ((!document.getElementById('xMidasToken')?.value) && tokenWait < 150) {
            await new Promise(r => setTimeout(r, 100));
            tokenWait++;
        }
        const tokenEl = document.getElementById('xMidasToken');
        if (!tokenEl || !tokenEl.value) return {error: 'no_xmidas_token'};

        const ctoken     = tokenEl.value;
        const versionEl  = document.getElementById('xMidasVersion');
        const ctoken_ver = (versionEl && versionEl.value) ? versionEl.value : '1.0.1';

        // Wait for window.xMidas (up to 15s).
        // With the route intercept + configurable:false lock, it should be
        // present immediately; this loop is a safety net.
        let xmWait = 0;
        while (typeof window.xMidas !== 'function' && xmWait < 150) {
            await new Promise(r => setTimeout(r, 100));
            xmWait++;
        }
        if (typeof window.xMidas !== 'function') return {
            error: 'no_xmidas_function',
            midasKeys: Object.keys(window).filter(k => k.toLowerCase().includes('midas')),
        };

        // Build publicParams — mirrors ei() in 91.79b63beb.bundle.js module 36453.
        // server validates the full merged payload; omitting these fields causes HTTP 500.
        const sd = window.SERVER_DATA || {};
        const payInfo = sd.payInfo || {};
        const shopInfo = sd.shopInfo || {};
        const ri = window.__Report_INFO || {};
        const rp = sd.reportParams || {};
        const queryString = (obj) => Object.keys(obj)
            .filter(k => obj[k] !== undefined && obj[k] !== null)
            .map(k => encodeURIComponent(k) + '=' + encodeURIComponent(obj[k]))
            .join('&');

        const device_id = rp.midasbuyDeviceId || ri.midasbuyDeviceId || '';
        const muid      = rp.midasuid        || ri.midasuid        || '';

        // tdrc_fp is the UUID cookie value — NOT a Forter token
        const uuidMatch = document.cookie.match(/UUID=([^;]*)/);
        const tdrc_fp   = uuidMatch ? uuidMatch[1] : '';

        const cgi_extend_obj = {device_id, pagetoken: '', tdrc_fp, muid};
        const cgi_extend     = queryString(cgi_extend_obj);
        const drm_info       = queryString(payInfo.drm_info || {});
        const buyType        = sd.buyType || (location.pathname.includes('/redeem/') ? 'REDEEM' : '');

        const publicParams = {
            appid:         payInfo.appid || sd.appid || '1450015065',
            pf:            payInfo.pf || 'mds_pc_browser-yy-android-midasweb-midasbuy-self.midasbuy_saas',
            zoneid:        String(payInfo.zoneid || payInfo.zone_id || payInfo.currentBindUser?.zoneid || '1'),
            country:       (payInfo.country || sd.country || 'BD').toUpperCase(),
            cgi_extend,
            drm_info,
            midasbuyArea:  payInfo.midasbuyArea || sd.midasbuyArea || '',
            shopcode:      shopInfo.shopcode || '',
            buyType,
            midas_sdk:     '1',
            currency_type: payInfo.currency_type || sd.currency_type || 'USD',
            _id:           Math.random(),
            sc:            '',
            from:          '',
            task_token:    '',
            cgi_extend_obj,
        };

        // Merge: actualPayload fields win over publicParams on overlap (e.g. country, appid)
        const actualPayload = JSON.parse(payloadJson);
        const fullPayload   = Object.assign({}, publicParams, actualPayload);
        for (const k of Object.keys(fullPayload)) {
            if (fullPayload[k] !== undefined && typeof fullPayload[k] !== 'object') {
                fullPayload[k] = String(fullPayload[k]);
            }
        }
        const fullJson      = JSON.stringify(fullPayload);

        try { window.xMidas(); } catch(e) {}
        const hexResult = window.xMidas({d: fullJson});
        if (!hexResult || typeof hexResult !== 'string' || hexResult.length === 0)
            return {error: 'xmidas_empty', got: JSON.stringify(hexResult)};

        const bytes       = (hexResult.match(/../g) || []).map(h => parseInt(h, 16));
        const encrypt_msg = btoa(String.fromCharCode(...bytes));

        const resp = await fetch('https://www.midasbuy.com' + endpoint, {
            method:      method || 'POST',
            headers:     {'Content-Type': 'application/json', 'Accept': 'application/json, text/plain, */*'},
            body:        JSON.stringify({encrypt_msg, ctoken_ver, ctoken}),
            credentials: 'include',
        });

        const text = await resp.text();
        let data;
        try { data = JSON.parse(text); }
        catch(e) {
            return {error: 'invalid_json', status: resp.status,
                    encrypt_msg_len: encrypt_msg.length, text: text.substring(0, 300)};
        }
        return {ok: true, status: resp.status, data, encrypt_msg_len: encrypt_msg.length};
    } catch(e) {
        return {error: 'js_exception', detail: String(e), stack: (e.stack||'').substring(0,500)};
    }
}
"""

_JS_ENCRYPT_ONLY = """
async ({payloadJson}) => {
    try {
        let tokenWait = 0;
        while ((!document.getElementById('xMidasToken')?.value) && tokenWait < 150) {
            await new Promise(r => setTimeout(r, 100));
            tokenWait++;
        }
        const tokenEl   = document.getElementById('xMidasToken');
        const versionEl = document.getElementById('xMidasVersion');
        if (!tokenEl || !tokenEl.value) return {error: 'no_xmidas_token'};

        const ctoken     = tokenEl.value;
        const ctoken_ver = (versionEl && versionEl.value) ? versionEl.value : '1.0.1';

        let xmWait = 0;
        while (typeof window.xMidas !== 'function' && xmWait < 150) {
            await new Promise(r => setTimeout(r, 100));
            xmWait++;
        }
        if (typeof window.xMidas !== 'function') return {error: 'no_xmidas_function'};

        const sd = window.SERVER_DATA || {};
        const payInfo = sd.payInfo || {};
        const shopInfo = sd.shopInfo || {};
        const ri = window.__Report_INFO || {};
        const rp = sd.reportParams || {};
        const queryString = (obj) => Object.keys(obj)
            .filter(k => obj[k] !== undefined && obj[k] !== null)
            .map(k => encodeURIComponent(k) + '=' + encodeURIComponent(obj[k]))
            .join('&');

        const device_id = rp.midasbuyDeviceId || ri.midasbuyDeviceId || '';
        const muid      = rp.midasuid        || ri.midasuid        || '';
        const uuidMatch = document.cookie.match(/UUID=([^;]*)/);
        const tdrc_fp   = uuidMatch ? uuidMatch[1] : '';
        const cgi_extend_obj = {device_id, pagetoken: '', tdrc_fp, muid};
        const buyType = sd.buyType || (location.pathname.includes('/redeem/') ? 'REDEEM' : '');

        const publicParams = {
            appid: payInfo.appid || sd.appid || '1450015065',
            pf: payInfo.pf || 'mds_pc_browser-yy-android-midasweb-midasbuy-self.midasbuy_saas',
            zoneid: String(payInfo.zoneid || payInfo.zone_id || payInfo.currentBindUser?.zoneid || '1'),
            country: (payInfo.country || sd.country || 'BD').toUpperCase(),
            cgi_extend: queryString(cgi_extend_obj),
            drm_info: queryString(payInfo.drm_info || {}),
            midasbuyArea: payInfo.midasbuyArea || sd.midasbuyArea || '',
            shopcode: shopInfo.shopcode || '',
            buyType,
            midas_sdk: '1',
            currency_type: payInfo.currency_type || sd.currency_type || 'USD',
            _id: Math.random(),
            sc: '',
            from: '',
            task_token: '',
            cgi_extend_obj,
        };

        const actualPayload = JSON.parse(payloadJson);
        const fullPayload   = Object.assign({}, publicParams, actualPayload);
        for (const k of Object.keys(fullPayload)) {
            if (fullPayload[k] !== undefined && typeof fullPayload[k] !== 'object') {
                fullPayload[k] = String(fullPayload[k]);
            }
        }
        const fullJson      = JSON.stringify(fullPayload);

        try { window.xMidas(); } catch(e) {}
        const hexResult = window.xMidas({d: fullJson});
        if (!hexResult || typeof hexResult !== 'string' || hexResult.length === 0)
            return {error: 'xmidas_empty'};

        const bytes       = (hexResult.match(/../g) || []).map(h => parseInt(h, 16));
        const encrypt_msg = btoa(String.fromCharCode(...bytes));
        return {ok: true, ctoken, ctoken_ver, encrypt_msg};
    } catch(e) {
        return {error: 'js_exception', detail: String(e)};
    }
}
"""

# ── Redeem COMMIT (window.midas.buyGoods) ───────────────────────────────────────
#
# PROVISIONAL — needs validation against live traffic. From the bundle trace:
#   commit = window.midas.buyGoods(em, {onMessage})
#   buyGoods -> _getPayUrl(em,'v3') (requires appid,pf,openid,currency_type,
#               productid) -> _processUrl form-POSTs `em` into an iframe target
#               (redeemCodeChannelIframe); the iframe submits the real order and
#               reports back via postMessage (a key=value&... querystring).
#
# There is NO standalone JSON commit endpoint. We assemble `em` in-page from
# window.SERVER_DATA.payInfo + the successful QueryRedeemCodeInfo response
# (offer productid / shop_id) + the #xMidasToken DOM inputs, create a hidden
# iframe target, drive buyGoods, and capture every postMessage. FLEXIBLE_RISK_
# CONTROL is handled via window.midas.newRiskControl(source) -> POST
# /h5/overseah5/v1/secondary_order. The Python side ALSO records the network
# request/response so a live run yields the ground-truth order payload even if
# this assembly is incomplete.
_JS_COMMIT_REDEEM = """
async ({redeemCode, roleId, redeemInfoJson, timeoutMs}) => {
    const log = [];
    const messages = [];
    try {
        // tokens + xMidas must be ready
        let w = 0;
        while ((typeof window.midas === 'undefined' || typeof window.midas.buyGoods !== 'function') && w < 150) {
            await new Promise(r => setTimeout(r, 100)); w++;
        }
        if (!window.midas || typeof window.midas.buyGoods !== 'function')
            return {error: 'no_buyGoods', midasType: typeof window.midas};

        const sd       = window.SERVER_DATA || {};
        const payInfo  = sd.payInfo  || {};
        const shopInfo = sd.shopInfo || {};
        let redeemInfo = {};
        try { redeemInfo = JSON.parse(redeemInfoJson || '{}'); } catch(e) {}

        // Offer fields come from the successful QueryRedeemCodeInfo response.
        // Field names are best-guess until we capture a real success payload —
        // we log the whole thing so the shape can be confirmed.
        const rinfo    = redeemInfo.redeem_code_info || redeemInfo.redeemCodeInfo || redeemInfo;
        const products = rinfo.products || rinfo.product_list || [];
        const product  = products[0] || rinfo.product || {};
        const productid = product.productid || product.product_id || product.offer_id
                        || rinfo.productid || rinfo.product_id || '';

        const tokenEl   = document.getElementById('xMidasToken');
        const versionEl = document.getElementById('xMidasVersion');

        // Hidden iframe target that _processUrl will POST the order into.
        let iframe = document.getElementById('redeemCodeChannelIframe');
        if (!iframe) {
            iframe = document.createElement('iframe');
            iframe.id = 'redeemCodeChannelIframe';
            iframe.style.cssText = 'position:fixed;width:1px;height:1px;left:-9999px;top:-9999px;border:0;';
            document.body.appendChild(iframe);
        }

        const returnUrl = location.origin + location.pathname;
        const em = {
            appid:         payInfo.appid || '1450015065',
            pf:            payInfo.pf || 'mds_pc_browser-yy-android-midasweb-midasbuy-self.midasbuy_saas',
            pfkey:         payInfo.pfkey || 'pfKey',
            openid:        payInfo.openid || roleId || '',
            zoneid:        String(payInfo.zoneid || payInfo.zone_id || '1'),
            country:       (payInfo.country || sd.country || 'BD').toUpperCase(),
            currency_type: product.currency_type || payInfo.currency_type || 'USD',
            shop_id:       shopInfo.shop_id || shopInfo.shopId || rinfo.shop_id || payInfo.shop_id || '',
            productid:     String(productid),
            num:           String(product.num || 1),
            quantity:      1,
            version:       'midasbuy_v2',
            // ← the 18-char redeem code (merged onto em via arg-3 in the real flow)
            redeem_code:   redeemCode,
            channel:       'midasbuy_redeem',
            subchannel:    'midasbuy_redeem',
            id:            'MIDASBUY_REDEEM',
            buyTypeKey:    'REDEEM',
            buy_type_key:  'REDEEM',
            successUrl:    returnUrl + '/success?isFromJsx=true&buy_type_key=REDEEM',
            pendingUrl:    returnUrl,
            failUrl:       returnUrl,
            useIFrame:     '1',
            usePost:       '1',
            newtab:        '0',
            ctoken:        tokenEl ? tokenEl.value : '',
            ctoken_ver:    (versionEl && versionEl.value) ? versionEl.value : '1.0.1',
            target:        iframe.contentWindow,
        };
        log.push('em assembled productid=' + em.productid + ' shop_id=' + em.shop_id + ' openid=' + em.openid);

        const finished = {done: false, result: null};
        const onMsg = (e) => {
            try {
                let raw = e;
                if (typeof e === 'string') {
                    const o = {}; e.split('&').forEach(kv => {
                        const i = kv.indexOf('='); if (i>0) o[decodeURIComponent(kv.slice(0,i))] = decodeURIComponent(kv.slice(i+1));
                    }); raw = o;
                }
                messages.push(raw);
                const status = raw.status || raw.orderStatus || raw.orderInfo?.status;
                if (status === 'success' || raw.orderInfo?.status === 'Created') {
                    finished.done = true; finished.result = {outcome: 'success', data: raw};
                } else if (status === 'error') {
                    finished.done = true; finished.result = {outcome: 'error', data: raw};
                }
            } catch(err) { log.push('onMsg err ' + err); }
        };

        // Capture iframe -> parent postMessages too (the real result channel).
        window.addEventListener('message', (ev) => {
            try { if (ev && ev.data !== undefined) onMsg(ev.data); } catch(e) {}
        }, false);

        try {
            window.midas.buyGoods(em, {onMessage: onMsg});
            log.push('buyGoods invoked');
        } catch(err) {
            return {error: 'buyGoods_threw', detail: String(err), log};
        }

        const deadline = Date.now() + (timeoutMs || 25000);
        while (!finished.done && Date.now() < deadline) {
            await new Promise(r => setTimeout(r, 200));

            // Risk-control branch: a message carrying FLEXIBLE_RISK_CONTROL.
            const rc = messages.find(m => {
                const d = (() => { try { return typeof m.data === 'string' ? JSON.parse(m.data) : (m.data || m); } catch(e) { return m; } })();
                return d && (d.name === 'FLEXIBLE_RISK_CONTROL' || String(d.err_code||'').startsWith('FLEXIBLE_RISK_CONTROL'));
            });
            if (rc && typeof window.midas.newRiskControl === 'function') {
                try {
                    const d = (() => { try { return typeof rc.data === 'string' ? JSON.parse(rc.data) : (rc.data || rc); } catch(e) { return rc; } })();
                    const source = d.details?.[0]?.source || d.source;
                    log.push('risk-control challenge source=' + source);
                    const tok = await window.midas.newRiskControl(source);
                    log.push('newRiskControl -> rc_uuid=' + (tok && tok.rc_uuid));
                    if (tok && tok.rc_token && tok.rc_uuid) {
                        const resp = await fetch(location.origin + '/h5/overseah5/v1/secondary_order', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({rc_token: tok.rc_token, rc_uuid: tok.rc_uuid, channel: 'MIDASBUY_REDEEM'}),
                            credentials: 'include',
                        });
                        const sj = await resp.json().catch(() => ({}));
                        log.push('secondary_order status=' + resp.status);
                        if (sj?.orderInfo?.status === 'Created') {
                            finished.done = true; finished.result = {outcome: 'success', data: sj, viaRiskControl: true};
                        } else {
                            finished.done = true; finished.result = {outcome: 'risk_control', data: sj, viaRiskControl: true};
                        }
                    } else {
                        finished.done = true; finished.result = {outcome: 'risk_control', data: {note: 'newRiskControl returned no token'}};
                    }
                } catch(err) { log.push('risk-control handling err ' + err); }
            }
        }

        return {
            ok: true,
            finished: finished.done,
            result: finished.result,
            messages,
            em_debug: Object.assign({}, em, {target: undefined}),
            redeem_info_seen: redeemInfo,
            log,
        };
    } catch(e) {
        return {error: 'js_exception', detail: String(e), stack: (e.stack||'').slice(0,500), log};
    }
}
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _setup_chaos_vm_protection(page) -> None:
    """
    Register a page.route intercept for the Chaos VM CDN URL BEFORE page.goto.

    Serves the local JS file (or fetches from CDN as fallback) with
    _CHAOS_VM_PROTECTION appended.  The protection runs synchronously during
    script execution — before any React hydration task — so window.xMidas
    is locked with configurable:false and can never be deleted by the SPA.
    """
    def handle_route(route):
        try:
            with open(_CHAOS_VM_LOCAL_PATH, "rb") as f:
                original = f.read()
            route.fulfill(
                status=200,
                headers={
                    "content-type": "application/javascript; charset=utf-8",
                    "cache-control": "no-cache",
                },
                body=original + _CHAOS_VM_PROTECTION,
            )
            logger.info(
                "[CRYPTO] chaos VM served from local file + protection (%d bytes total)",
                len(original) + len(_CHAOS_VM_PROTECTION),
            )
        except Exception as e:
            logger.warning("[CRYPTO] local chaos VM unavailable (%s) — fetching from CDN", e)
            try:
                response = route.fetch()
                body = response.body() + _CHAOS_VM_PROTECTION
                hdrs = dict(response.headers)
                hdrs.pop("content-length", None)
                route.fulfill(status=response.status, headers=hdrs, body=body)
                logger.info("[CRYPTO] chaos VM fetched from CDN + protection appended")
            except Exception as e2:
                logger.error("[CRYPTO] CDN fetch also failed (%s) — no protection", e2)
                route.continue_()

    page.route("**cdn.midasbuy.com/js/x-midas/**", handle_route)
    logger.info("[CRYPTO] chaos VM route intercept registered")


def _setup_lightweight_routes(page) -> None:
    """Skip heavy resources that are not needed for hidden tokens or xMidas."""
    def handle_route(route):
        try:
            if route.request.resource_type in {"image", "font", "media"}:
                route.abort()
            else:
                route.continue_()
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    page.route("**/*", handle_route)
    logger.info("[CRYPTO] lightweight resource route registered")


# ── Human-behaviour simulation (anti risk-control) ──────────────────────────────

def _simulate_human_activity(page, *, moves: int = 10, scrolls: int = 3, keys: bool = True) -> None:
    """
    Generate genuine pointer/scroll/key events so tdrc.js records non-empty
    behavioral telemetry.

    tdrc.js samples mousemove on a ~200ms throttle, so moves are spaced >200ms
    apart to register as distinct coordinates. Mouse *movement alone* satisfies
    tdrc's msgIsValid gate; we deliberately avoid synthetic clicks because a
    click at an arbitrary coordinate could hit a link and navigate the cached
    page away, breaking the warmed session. Tab/Shift+Tab give safe key counts.
    """
    import random

    vw, vh = 1440, 900
    step_every = max(1, moves // max(1, scrolls))
    try:
        for i in range(moves):
            nx = random.randint(60, vw - 60)
            ny = random.randint(90, vh - 120)
            page.mouse.move(nx, ny, steps=random.randint(4, 10))
            page.wait_for_timeout(random.randint(230, 430))
            if scrolls and i % step_every == 0:
                page.mouse.wheel(0, random.randint(120, 420))
                page.wait_for_timeout(random.randint(180, 320))
        if keys:
            for _ in range(random.randint(2, 4)):
                page.keyboard.press("Tab")
                page.wait_for_timeout(random.randint(90, 180))
            page.keyboard.press("Shift+Tab")
    except Exception as exc:
        logger.debug("[CRYPTO] human activity simulation skipped: %s", exc)


def _ensure_redeem_behavior(session: "_CachedBrowserSession") -> None:
    """
    Seed human behavior + dwell before a value-transfer call, so at least one
    populated /cgi-bin/fp-behv heartbeat reaches the risk backend first. Re-warms
    only if the session has gone cold (older than _BEHAVIOR_REWARM_S) to keep
    repeat redeems on a warmed session fast.
    """
    now = time.time()
    if session.behavior_warmed_at and (now - session.behavior_warmed_at) < _BEHAVIOR_REWARM_S:
        logger.debug("[CRYPTO] behavior still warm — skipping re-seed")
        return

    logger.info("[CRYPTO] seeding human behavior before value-transfer call")
    _simulate_human_activity(session.page, moves=12, scrolls=4, keys=True)
    try:
        # dwell so a populated behavioral heartbeat fires before we redeem
        session.page.wait_for_timeout(_BEHAVIOR_HEARTBEAT_MS)
    except Exception:
        pass
    session.behavior_warmed_at = time.time()
    logger.info("[CRYPTO] behavior seeding complete (dwell=%dms)", _BEHAVIOR_HEARTBEAT_MS)
    _log_risk_probe(session.page)


def _install_risk_telemetry_log(page) -> None:
    """
    Log requests to risk/anti-fraud telemetry endpoints. If nothing appears,
    tdrc.js is NOT reporting behavior (so warming can't possibly help and the
    real lever is the browser fingerprint / headful mode).
    """
    def on_request(req):
        try:
            u = req.url.lower()
            if any(h in u for h in _RISK_TELEMETRY_HINTS):
                logger.info("[RISK-TELEMETRY] %s %s", req.method, req.url[:160])
        except Exception:
            pass

    page.on("request", on_request)


_JS_RISK_PROBE = """
() => {
    const fonts = (() => { try { return document.fonts ? document.fonts.size : -1; } catch(e){ return -2; } })();
    let webglVendor = '', webglRenderer = '';
    try {
        const gl = document.createElement('canvas').getContext('webgl');
        const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
        if (dbg) { webglVendor = gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL);
                   webglRenderer = gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL); }
    } catch(e) {}
    return {
        webdriver:    navigator.webdriver,
        hasXT:        !!window._XT,                 // tdrc.js config present?
        xtLen:        (window._XT && window._XT.length) || 0,
        hasMidas:     typeof window.midas,
        hasNewRC:     typeof (window.midas && window.midas.newRiskControl),
        hasChaosVM:   typeof window.xMidas,
        uuidCookie:   /UUID=/.test(document.cookie),
        plugins:      navigator.plugins.length,
        fonts,
        webglVendor, webglRenderer,
        hardwareConcurrency: navigator.hardwareConcurrency,
        ua:           navigator.userAgent.slice(0, 80),
    };
}
"""


def _log_risk_probe(page) -> None:
    """One-shot snapshot of the signals risk-control scores. Send these to debug."""
    try:
        diag = page.evaluate(_JS_RISK_PROBE)
        logger.info("[RISK-PROBE] %s", json.dumps(diag, default=str))
    except Exception as exc:
        logger.warning("[RISK-PROBE] failed: %s", exc)


def _load_session_storage(storage_state_path: str) -> dict:
    ss_path = os.path.join(os.path.dirname(storage_state_path), "session_storage.json")
    if not os.path.exists(ss_path):
        return {}
    try:
        with open(ss_path, encoding="utf-8") as f:
            data = json.load(f)
        logger.info("[CRYPTO] loaded %d sessionStorage keys", len(data))
        return data
    except Exception as e:
        logger.warning("[CRYPTO] could not load session_storage.json: %s", e)
        return {}


def _launch_context(p, storage_state_path: str):
    ss_data = _load_session_storage(storage_state_path)

    browser = p.chromium.launch(
        headless=_HEADLESS,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )

    context = browser.new_context(
        storage_state=storage_state_path,
        viewport={"width": 1440, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        timezone_id="America/New_York",
    )

    context.add_init_script(_STEALTH_JS)

    if ss_data:
        ss_json = json.dumps(ss_data)
        context.add_init_script(f"""
            (() => {{
                const data = {ss_json};
                for (const [k,v] of Object.entries(data)) {{
                    try {{ sessionStorage.setItem(k, v); }} catch(e) {{}}
                }}
            }})();
        """)

    return browser, context


def _storage_mtime(storage_state_path: str) -> float:
    try:
        return os.path.getmtime(storage_state_path)
    except OSError:
        return 0.0


def _session_cache_key(storage_state_path: str, country_code: str) -> Tuple[str, str, int]:
    # Playwright sync objects are thread-affine, so cache per worker thread.
    return (os.path.abspath(storage_state_path), country_code.lower(), get_ident())


def _session_is_usable(session: _CachedBrowserSession, storage_mtime: float) -> bool:
    if session.storage_mtime != storage_mtime:
        return False
    try:
        if session.page.is_closed():
            return False
        if hasattr(session.browser, "is_connected") and not session.browser.is_connected():
            return False
    except Exception:
        return False
    return True


def _close_cached_session(session: _CachedBrowserSession) -> None:
    for obj, method_name in (
        (session.context, "close"),
        (session.browser, "close"),
        (session.manager, "stop"),
    ):
        try:
            getattr(obj, method_name)()
        except Exception:
            pass


def _discard_cached_session(session: _CachedBrowserSession) -> None:
    with _SESSION_CACHE_LOCK:
        for key, cached in list(_SESSION_CACHE.items()):
            if cached is session:
                _SESSION_CACHE.pop(key, None)
    _close_cached_session(session)


def close_cached_browser_sessions() -> None:
    """Best-effort shutdown hook for warmed browser sessions."""
    with _SESSION_CACHE_LOCK:
        sessions = list(_SESSION_CACHE.values())
        _SESSION_CACHE.clear()

    for session in sessions:
        with session.lock:
            _close_cached_session(session)


def _create_cached_session(
    storage_state_path: str,
    country_code: str,
    timeout_ms: int,
) -> _CachedBrowserSession:
    sync_playwright, PWTimeout = _get_playwright()

    storage_state_path = os.path.abspath(storage_state_path)
    redeem_url = f"https://www.midasbuy.com/midasbuy/{country_code}/redeem/pubgm"
    session_dir = os.path.dirname(storage_state_path)
    manager = sync_playwright()
    p = manager.start()
    browser = None
    context = None

    try:
        browser, context = _launch_context(p, storage_state_path)
        page = context.new_page()

        _setup_lightweight_routes(page)
        _setup_chaos_vm_protection(page)
        _install_risk_telemetry_log(page)

        logger.info("[CRYPTO] warming cached page %s (headless=%s)", redeem_url, _HEADLESS)
        page.goto(redeem_url, wait_until="domcontentloaded", timeout=timeout_ms)
        logger.info("[CRYPTO] cached page ready url=%s", page.url)

        if not _wait_for_xmidas(page, session_dir, timeout_ms):
            raise RuntimeError("xMidas did not become ready")

        # Light initial seeding so the session is never stone-cold (no long dwell
        # here — read-only lookups stay fast; the full dwell is gated to redeems).
        _simulate_human_activity(page, moves=4, scrolls=1, keys=False)
        _log_risk_probe(page)

        now = time.time()
        return _CachedBrowserSession(
            manager=manager,
            browser=browser,
            context=context,
            page=page,
            storage_state_path=storage_state_path,
            country_code=country_code.lower(),
            storage_mtime=_storage_mtime(storage_state_path),
            created_at=now,
            last_used=now,
        )
    except PWTimeout:
        logger.exception("[CRYPTO] cached page warm-up timed out")
        if context:
            try:
                _save_debug(context.pages[0], session_dir, "crypto_cache_timeout")
            except Exception:
                pass
        if context:
            try:
                context.close()
            except Exception:
                pass
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        try:
            manager.stop()
        except Exception:
            pass
        raise
    except Exception:
        logger.exception("[CRYPTO] cached page warm-up failed")
        if context:
            try:
                context.close()
            except Exception:
                pass
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        try:
            manager.stop()
        except Exception:
            pass
        raise


def _get_or_create_cached_session(
    storage_state_path: str,
    country_code: str,
    timeout_ms: int,
) -> _CachedBrowserSession:
    key = _session_cache_key(storage_state_path, country_code)
    storage_mtime = _storage_mtime(os.path.abspath(storage_state_path))

    with _SESSION_CACHE_LOCK:
        session = _SESSION_CACHE.get(key)
        if session and _session_is_usable(session, storage_mtime):
            session.last_used = time.time()
            logger.info("[CRYPTO] reusing cached browser session")
            return session

        if session:
            _SESSION_CACHE.pop(key, None)
            _close_cached_session(session)

        session = _create_cached_session(storage_state_path, country_code, timeout_ms)
        _SESSION_CACHE[key] = session
        return session


def _wait_for_xmidas(page, session_dir: str, timeout_ms: int) -> bool:
    from playwright.sync_api import TimeoutError as PWTimeout
    try:
        from patchright.sync_api import TimeoutError as PWTimeout
    except ImportError:
        pass

    try:
        page.wait_for_function(
            "() => !!document.getElementById('xMidasToken')?.value",
            timeout=timeout_ms,
        )
        logger.info("[CRYPTO] xMidasToken ready")
    except PWTimeout:
        logger.error("[CRYPTO] timeout waiting for xMidasToken")
        _save_debug(page, session_dir, "crypto_no_token")
        return False

    try:
        has_xmidas = page.evaluate("() => typeof window.xMidas === 'function'")
    except Exception:
        has_xmidas = False

    if not has_xmidas:
        try:
            logger.info("[CRYPTO] window.xMidas missing - injecting local Chaos VM")
            with open(_CHAOS_VM_LOCAL_PATH, "r", encoding="utf-8") as f:
                vm_source = f.read()
            page.evaluate("(source) => { (0, eval)(source); }", vm_source)
            has_xmidas = page.evaluate("() => typeof window.xMidas === 'function'")
            logger.info("[CRYPTO] local Chaos VM injection xMidas=%s", has_xmidas)
        except Exception as exc:
            logger.warning("[CRYPTO] local Chaos VM injection failed: %s", exc)

    try:
        page.wait_for_function(
            "() => typeof window.xMidas === 'function'",
            timeout=30_000,
        )
        logger.info("[CRYPTO] window.xMidas ready")
    except PWTimeout:
        logger.warning("[CRYPTO] window.xMidas not detected after 30s — JS evaluate will poll")

    try:
        diag = page.evaluate("""
            () => ({
                xMidasType:  typeof window.xMidas,
                xMidasToken: !!document.getElementById('xMidasToken')?.value,
                url:         location.href,
                readyState:  document.readyState,
                midasKeys:   Object.keys(window).filter(k => k.toLowerCase().includes('midas')),
            })
        """)
        logger.info("[CRYPTO] pre-call diag: %s", diag)
    except Exception as _e:
        logger.warning("[CRYPTO] diag failed: %s", _e)

    return True


# ── Public API ────────────────────────────────────────────────────────────────

def _extract_browser_result(result: Optional[dict], endpoint: str) -> Optional[dict]:
    if not result:
        logger.error("[CRYPTO] evaluate returned None")
        return None

    if result.get("error"):
        logger.error(
            "[CRYPTO] fetch failed  endpoint=%s  error=%s  status=%s  encrypt_msg_len=%s  midasKeys=%s",
            endpoint,
            result.get("error"),
            result.get("status"),
            result.get("encrypt_msg_len", "?"),
            result.get("midasKeys"),
        )
        return None

    logger.info(
        "[CRYPTO] ok  status=%s  encrypt_msg_len=%s",
        result.get("status"),
        result.get("encrypt_msg_len"),
    )
    return result.get("data")


def _call_api_in_cached_browser(
    payload: dict,
    endpoint: str,
    storage_state_path: str,
    country_code: str,
    method: str,
    timeout_ms: int,
) -> Optional[dict]:
    payload_json = json.dumps(payload, separators=(",", ":"))

    for attempt in (1, 2):
        try:
            session = _get_or_create_cached_session(storage_state_path, country_code, timeout_ms)
        except Exception:
            logger.exception("[CRYPTO] could not create cached browser session")
            return None

        with session.lock:
            try:
                if endpoint.rstrip("/") in _BEHAVIOR_REQUIRED_ENDPOINTS:
                    _ensure_redeem_behavior(session)
                    # Always emit a fingerprint snapshot on a redeem (even when the
                    # session was already warm) so diagnostics are never missing.
                    _log_risk_probe(session.page)
                logger.info("[CRYPTO] cached in-browser fetch endpoint=%s attempt=%d", endpoint, attempt)
                result = session.page.evaluate(_JS_CALL_API, {
                    "payloadJson": payload_json,
                    "endpoint":    endpoint,
                    "method":      method,
                })
                session.last_used = time.time()
            except Exception:
                logger.exception("[CRYPTO] cached page evaluate failed")
                _discard_cached_session(session)
                if attempt == 1:
                    continue
                return None

            if result and result.get("error") in {
                "no_xmidas_token",
                "no_xmidas_function",
                "xmidas_empty",
                "js_exception",
            }:
                logger.warning("[CRYPTO] cached page stale (%s), rebuilding", result.get("error"))
                _discard_cached_session(session)
                if attempt == 1:
                    continue

            return _extract_browser_result(result, endpoint)

    return None


def call_api_in_browser(
    payload: dict,
    endpoint: str,
    storage_state_path: str,
    country_code: str = "bd",
    method: str = "POST",
    timeout_ms: int = 60_000,
) -> Optional[dict]:
    """
    Navigate to the Midasbuy redeem page, run window.xMidas and fetch()
    entirely inside the browser, return the parsed JSON response.
    """
    if endpoint.rstrip("/") in _CACHED_ENDPOINTS:
        cached_data = _call_api_in_cached_browser(
            payload,
            endpoint,
            storage_state_path,
            country_code,
            method,
            timeout_ms,
        )
        if cached_data is not None:
            return cached_data
        logger.warning("[CRYPTO] cached call failed; falling back to a fresh browser endpoint=%s", endpoint)

    sync_playwright, PWTimeout = _get_playwright()

    redeem_url  = f"https://www.midasbuy.com/midasbuy/{country_code}/redeem/pubgm"
    session_dir = os.path.dirname(storage_state_path)

    try:
        with sync_playwright() as p:
            browser, context = _launch_context(p, storage_state_path)
            page = context.new_page()

            # Route intercept must be registered BEFORE page.goto
            _setup_chaos_vm_protection(page)

            logger.info("[CRYPTO] navigating to %s", redeem_url)
            try:
                page.goto(redeem_url, wait_until="load", timeout=timeout_ms)
            except PWTimeout:
                logger.error("[CRYPTO] page.goto timed out")
                _save_debug(page, session_dir, "crypto_timeout")
                browser.close()
                return None

            logger.info("[CRYPTO] page loaded  url=%s", page.url)

            if not _wait_for_xmidas(page, session_dir, timeout_ms):
                browser.close()
                return None

            if endpoint.rstrip("/") in _BEHAVIOR_REQUIRED_ENDPOINTS:
                _simulate_human_activity(page, moves=12, scrolls=4, keys=True)
                try:
                    page.wait_for_timeout(_BEHAVIOR_HEARTBEAT_MS)
                except Exception:
                    pass

            payload_json = json.dumps(payload, separators=(",", ":"))
            logger.info("[CRYPTO] calling in-browser fetch  endpoint=%s", endpoint)

            result = page.evaluate(_JS_CALL_API, {
                "payloadJson": payload_json,
                "endpoint":    endpoint,
                "method":      method,
            })

            _save_debug(page, session_dir, "crypto_done")
            browser.close()

            return _extract_browser_result(result, endpoint)

    except Exception:
        logger.exception("[CRYPTO] call_api_in_browser crashed")
        return None


# ── Redeem COMMIT driver ────────────────────────────────────────────────────────

_COMMIT_CAPTURE_HINTS = ("order", "secondary", "/pay", "provide", "shelfproto", "trade")


def _install_order_capture(page, sink: list) -> None:
    """
    Log every request/response that looks like order/payment traffic so a live
    redeem yields the ground-truth commit endpoint + payload even if our in-page
    `em` assembly is incomplete. This is the mechanism that lets us finalize the
    commit against real data.
    """
    def _interesting(url: str) -> bool:
        u = url.lower()
        return any(h in u for h in _COMMIT_CAPTURE_HINTS)

    def on_request(req):
        try:
            if _interesting(req.url):
                entry = {"kind": "request", "method": req.method, "url": req.url}
                try:
                    entry["post_data"] = req.post_data
                except Exception:
                    entry["post_data"] = None
                sink.append(entry)
                logger.info("[COMMIT] >> %s %s", req.method, req.url)
                if entry.get("post_data"):
                    logger.info("[COMMIT]    payload=%s", str(entry["post_data"])[:1500])
        except Exception:
            pass

    def on_response(resp):
        try:
            if _interesting(resp.url):
                body = None
                try:
                    body = resp.text()
                except Exception:
                    body = None
                sink.append({"kind": "response", "status": resp.status, "url": resp.url, "body": body})
                logger.info("[COMMIT] << %s %s", resp.status, resp.url)
                if body:
                    logger.info("[COMMIT]    response=%s", str(body)[:1500])
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", on_response)


def commit_redeem_in_browser(
    redeem_info: dict,
    redeem_code: str,
    role_id: str,
    storage_state_path: str,
    country_code: str = "bd",
    timeout_ms: int = 60_000,
) -> Optional[dict]:
    """
    Drive window.midas.buyGoods for the redeem commit on a dedicated page (kept
    separate from the cached API page so its DOM state is not disturbed).

    PROVISIONAL: the order bag assembly is best-effort until validated against a
    real successful QueryRedeemCodeInfo + order capture. The returned dict always
    includes `network` (captured order/payment traffic) so the live request can
    be inspected and the commit finalized.
    """
    sync_playwright, PWTimeout = _get_playwright()

    redeem_url  = f"https://www.midasbuy.com/midasbuy/{country_code}/redeem/pubgm"
    session_dir = os.path.dirname(storage_state_path)
    network: list = []

    try:
        with sync_playwright() as p:
            browser, context = _launch_context(p, storage_state_path)
            page = context.new_page()

            _setup_chaos_vm_protection(page)
            _install_order_capture(page, network)

            logger.info("[COMMIT] navigating to %s", redeem_url)
            try:
                page.goto(redeem_url, wait_until="load", timeout=timeout_ms)
            except PWTimeout:
                logger.error("[COMMIT] page.goto timed out")
                _save_debug(page, session_dir, "commit_timeout")
                browser.close()
                return {"ok": False, "error": "goto_timeout", "network": network}

            if not _wait_for_xmidas(page, session_dir, timeout_ms):
                browser.close()
                return {"ok": False, "error": "no_xmidas", "network": network}

            # Behaviour first so the commit is not flagged by risk-control.
            _simulate_human_activity(page, moves=12, scrolls=4, keys=True)
            try:
                page.wait_for_timeout(_BEHAVIOR_HEARTBEAT_MS)
            except Exception:
                pass

            logger.info("[COMMIT] driving buyGoods for code=***%s", redeem_code[-4:] if redeem_code else "")
            result = page.evaluate(_JS_COMMIT_REDEEM, {
                "redeemCode":     redeem_code,
                "roleId":         role_id,
                "redeemInfoJson": json.dumps(redeem_info or {}),
                "timeoutMs":      min(timeout_ms, 30_000),
            })

            _save_debug(page, session_dir, "commit_done")
            browser.close()

            if not isinstance(result, dict):
                result = {"ok": False, "error": "no_result"}
            result["network"] = network
            logger.info(
                "[COMMIT] result finished=%s outcome=%s captured=%d",
                result.get("finished"),
                (result.get("result") or {}).get("outcome"),
                len(network),
            )
            return result

    except Exception:
        logger.exception("[COMMIT] commit_redeem_in_browser crashed")
        return {"ok": False, "error": "exception", "network": network}


def get_browser_payload(
    payload: dict,
    storage_state_path: str,
    country_code: str = "bd",
    timeout_ms: int = 45_000,
) -> Optional[dict]:
    """Return {encrypt_msg, ctoken, ctoken_ver, fresh_cookies} or None."""
    sync_playwright, PWTimeout = _get_playwright()

    redeem_url  = f"https://www.midasbuy.com/midasbuy/{country_code}/redeem/pubgm"
    session_dir = os.path.dirname(storage_state_path)

    try:
        with sync_playwright() as p:
            browser, context = _launch_context(p, storage_state_path)
            page = context.new_page()

            _setup_chaos_vm_protection(page)

            logger.info("[CRYPTO] navigating to %s", redeem_url)
            try:
                page.goto(redeem_url, wait_until="load", timeout=timeout_ms)
            except PWTimeout:
                logger.error("[CRYPTO] page.goto timed out")
                _save_debug(page, session_dir, "crypto_timeout")
                browser.close()
                return None

            if not _wait_for_xmidas(page, session_dir, timeout_ms):
                browser.close()
                return None

            payload_json = json.dumps(payload, separators=(",", ":"))
            result = page.evaluate(_JS_ENCRYPT_ONLY, {"payloadJson": payload_json})

            fresh_cookies = {
                c["name"]: c["value"]
                for c in context.cookies()
                if c.get("name") and c.get("value")
            }

            _save_debug(page, session_dir, "crypto_done")
            browser.close()

            if not result or result.get("error"):
                logger.error("[CRYPTO] JS error: %s", result)
                return None

            logger.info(
                "[CRYPTO] ok  ctoken_prefix=%s  encrypt_msg_len=%d",
                result["ctoken"][:20],
                len(result["encrypt_msg"]),
            )
            return {
                "encrypt_msg":   result["encrypt_msg"],
                "ctoken":        result["ctoken"],
                "ctoken_ver":    result["ctoken_ver"],
                "fresh_cookies": fresh_cookies,
            }

    except Exception:
        logger.exception("[CRYPTO] get_browser_payload crashed")
        return None


def _save_debug(page, session_dir: str, name: str) -> None:
    try:
        page.screenshot(path=os.path.join(session_dir, f"{name}.png"), full_page=True)
        with open(os.path.join(session_dir, f"{name}.html"), "w", encoding="utf-8") as f:
            f.write(page.content())
        logger.info("[CRYPTO] debug artifacts saved: %s/%s.*", session_dir, name)
    except Exception as e:
        logger.debug("[CRYPTO] could not save debug artifacts: %s", e)
