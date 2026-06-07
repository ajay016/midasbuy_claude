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
import random
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
    lock: Lock = field(default_factory=Lock)


_SESSION_CACHE: Dict[Tuple[str, str, int], _CachedBrowserSession] = {}
_SESSION_CACHE_LOCK = Lock()
_CACHED_ENDPOINTS = {
    "/interface/getCharac",
    "/interface/shelfProto/shelves_svr/QueryRedeemCodeInfo",
}

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


def _launch_context(p, storage_state_path: str, headless: bool = True, bypass_csp: bool = False):
    ss_data = _load_session_storage(storage_state_path)

    browser = p.chromium.launch(
        headless=headless,
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
        bypass_csp=bypass_csp,
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

        logger.info("[CRYPTO] warming cached page %s", redeem_url)
        page.goto(redeem_url, wait_until="domcontentloaded", timeout=timeout_ms)
        logger.info("[CRYPTO] cached page ready url=%s", page.url)

        if not _wait_for_xmidas(page, session_dir, timeout_ms):
            raise RuntimeError("xMidas did not become ready")

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


# ── Redeem CAPTCHA solve + retry (free TCaptcha slider solver) ──────────────────

# Main-world script: invoke window.midas.newRiskControl(source) (renders the
# TCaptcha slider and resolves with {rc_token, rc_uuid} once solved) and write
# the result to a DOM element the driver can poll.
_JS_TRIGGER_RISKCONTROL = r"""
(function(){
  var me = document.currentScript;
  var nonce = me && me.getAttribute('data-nonce');
  function out(o){ var el = document.getElementById('__rc_out_'+nonce); if (el) el.textContent = JSON.stringify(o); }
  try {
    var source = JSON.parse(document.getElementById('__rc_in_'+nonce).textContent).source;
    if (!window.midas || typeof window.midas.newRiskControl !== 'function') { out({error:'no_newRiskControl', midas: typeof window.midas}); return; }
    window.midas.newRiskControl(source).then(function(tok){ out({ok:true, tok: tok}); })
      .catch(function(e){ out({error:'rc_rejected', detail: String(e)}); });
  } catch(e) { out({error:'js_exception', detail: String(e)}); }
})();
"""

_SLIDER_SELECTORS = (
    "#riskControlComponent",
    "iframe[src*='harvestsharp']",
    "iframe[src*='slider']",
    "#tcaptcha_iframe",
    "iframe[src*='captcha']",
)

# Solver browser runs headful by default so the slider renders for solving and
# can be watched/assisted; set MIDASBUY_SOLVER_HEADLESS=1 to force headless.
_SOLVER_HEADLESS = os.getenv("MIDASBUY_SOLVER_HEADLESS", "").lower() in ("1", "true", "yes", "on")
# Manual mode: don't auto-drag — wait for the human to solve the slider in the
# headful window, then capture the token + run the retry (tests the whole
# downstream pipeline and the real token format).
_CAPTCHA_MANUAL = os.getenv("MIDASBUY_CAPTCHA_MANUAL", "").lower() in ("1", "true", "yes", "on")


def _install_net_capture(page, sink: list, session_dir: Optional[str] = None) -> None:
    """
    Dump EVERY request URL (scripts, images, xhr) to captcha_network.txt and log
    request/response bodies for the captcha config + verify calls — so we get the
    real puzzle image URLs and the verify token format automatically, without
    hand-copying from DevTools.
    """
    body_hints = ("captcha", "tcaptcha", "cap_union", "harvestsharp", "slider",
                  "risk", "shelfproto", "redeem", "secondary", "order", "verify", "show")
    dump_path = os.path.join(session_dir, "captcha_network.txt") if session_dir else None

    def _append(line: str):
        if not dump_path:
            return
        try:
            with open(dump_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def on_request(req):
        try:
            _append(f">> {req.resource_type:9} {req.method:5} {req.url}")
            u = req.url.lower()
            if any(h in u for h in body_hints):
                try:
                    pd = req.post_data
                except Exception:
                    pd = None
                if pd:
                    _append(f"   POST_DATA: {pd[:2000]}")
        except Exception:
            pass

    def on_resp(resp):
        try:
            u = resp.url.lower()
            if any(h in u for h in body_hints):
                body = None
                try:
                    body = resp.text()
                except Exception:
                    body = None
                sink.append({"status": resp.status, "url": resp.url, "body": (body or "")[:2000]})
                logger.info("[CAPTCHA] net << %s %s", resp.status, resp.url[:140])
                _append(f"<< {resp.status} {resp.url}")
                if body:
                    _append(f"   RESP: {body[:2000]}")
                    logger.info("[CAPTCHA] net body=%s", (body or "")[:400])
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", on_resp)


def _keep_open_if_requested(page, session_dir: str) -> None:
    """
    If MIDASBUY_CAPTCHA_KEEP_OPEN is set, leave the headful browser open so the
    user can inspect DevTools / grab JS + network links. Closes early when a
    `close_captcha.txt` sentinel appears in the session dir, else after a cap.
    """
    if os.getenv("MIDASBUY_CAPTCHA_KEEP_OPEN", "").lower() not in ("1", "true", "yes", "on"):
        return
    sentinel = os.path.join(session_dir, "close_captcha.txt")
    max_s = int(os.getenv("MIDASBUY_CAPTCHA_KEEP_OPEN_S", "900"))
    logger.warning(
        "[CAPTCHA] keeping browser open up to %ds. Network dump: %s/captcha_network.txt. "
        "Create %s to close early.", max_s, session_dir, sentinel,
    )
    waited = 0
    while waited < max_s:
        if os.path.exists(sentinel):
            try:
                os.remove(sentinel)
            except Exception:
                pass
            break
        try:
            page.wait_for_timeout(1000)
        except Exception:
            time.sleep(1)
        waited += 1


def _find_slider_container(page) -> Optional[str]:
    for sel in _SLIDER_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                box = el.bounding_box()
                if box and box["width"] > 40 and box["height"] > 40:
                    return sel
        except Exception:
            continue
    return None


def solve_redeem_captcha_and_retry(
    payload: dict,
    source: str,
    storage_state_path: str,
    country_code: str = "bd",
    endpoint: str = "/interface/shelfProto/shelves_svr/QueryRedeemCodeInfo",
    timeout_ms: int = 90_000,
) -> Optional[dict]:
    """
    Render the TCaptcha slider via newRiskControl, auto-solve it (free path), then
    retry the redeem query carrying the captcha token. Runs on its own headful
    page so lookups are untouched. Saves the slider screenshot + captures the
    post-solve network so a real run yields ground truth for the retry contract.
    """
    from . import captcha_solver

    sync_playwright, PWTimeout = _get_playwright()
    redeem_url  = f"https://www.midasbuy.com/midasbuy/{country_code}/redeem/pubgm"
    session_dir = os.path.dirname(storage_state_path)
    network: list = []

    try:
        with sync_playwright() as p:
            browser, context = _launch_context(
                p, storage_state_path, headless=_SOLVER_HEADLESS, bypass_csp=True,
            )
            page = context.new_page()
            _setup_chaos_vm_protection(page)
            _install_net_capture(page, network, session_dir)

            # Capture the clean TCaptcha puzzle images for accurate gap detection.
            captcha_imgs: dict = {}
            def _on_img(resp):
                try:
                    if "cap_union_new_getcapbysig" in resp.url:
                        import re as _re
                        m = _re.search(r"img_index=(\d+)", resp.url)
                        idx = m.group(1) if m else str(len(captcha_imgs))
                        if idx not in captcha_imgs:
                            b = resp.body()
                            captcha_imgs[idx] = b
                            try:
                                with open(os.path.join(session_dir, f"captcha_getcap_{idx}.png"), "wb") as f:
                                    f.write(b)
                            except Exception:
                                pass
                            logger.info("[CAPTCHA] captured puzzle image idx=%s bytes=%d", idx, len(b))
                except Exception:
                    pass
            page.on("response", _on_img)

            # Watch the TCaptcha verify result: errorCode 12 == "operation too
            # often" (IP/frequency penalty). Stop attempting so we don't extend it.
            vstate = {"freq_penalty": False}
            def _on_verify(resp):
                try:
                    if "cap_union_new_verify" in resp.url:
                        d = json.loads(resp.text())
                        if str(d.get("errorCode")) == "12":
                            vstate["freq_penalty"] = True
                            logger.error("[CAPTCHA] verify errorCode=12 (operation too often) — Tencent frequency/IP penalty")
                except Exception:
                    pass
            page.on("response", _on_verify)

            logger.info("[CAPTCHA] opening redeem page (headless=%s) for captcha solve", _SOLVER_HEADLESS)
            try:
                page.goto(redeem_url, wait_until="load", timeout=timeout_ms)
            except PWTimeout:
                browser.close()
                return {"ok": False, "error": "goto_timeout", "network": network}

            try:
                page.wait_for_function(
                    "() => typeof window.midas !== 'undefined' && typeof window.midas.newRiskControl === 'function'",
                    timeout=30_000,
                )
            except Exception:
                logger.error("[CAPTCHA] window.midas.newRiskControl not available")
                _save_debug(page, session_dir, "captcha_no_sdk")
                browser.close()
                return {"ok": False, "error": "no_newRiskControl", "network": network}

            nonce = os.urandom(8).hex()
            page.evaluate(
                """(a) => {
                    const inEl = document.createElement('div');
                    inEl.id = '__rc_in_' + a.nonce; inEl.style.display = 'none';
                    inEl.textContent = JSON.stringify({source: a.source});
                    document.documentElement.appendChild(inEl);
                    const outEl = document.createElement('div');
                    outEl.id = '__rc_out_' + a.nonce; outEl.style.display = 'none';
                    document.documentElement.appendChild(outEl);
                    const s = document.createElement('script');
                    s.setAttribute('data-nonce', a.nonce);
                    s.textContent = a.code;
                    document.documentElement.appendChild(s);
                }""",
                {"nonce": nonce, "source": source, "code": _JS_TRIGGER_RISKCONTROL},
            )

            token = None
            # Give a human time to solve in manual mode.
            deadline = time.time() + (max(timeout_ms / 1000.0, 240) if _CAPTCHA_MANUAL else timeout_ms / 1000.0)
            attempts = 0
            max_attempts = int(os.getenv("MIDASBUY_CAPTCHA_MAX_ATTEMPTS", "2"))
            diag_done = False
            if _CAPTCHA_MANUAL:
                logger.warning("[CAPTCHA] MANUAL mode — solve the slider in the browser window; waiting...")

            def _poll_token():
                out = page.evaluate(
                    "(n) => { const el = document.getElementById('__rc_out_' + n); return el && el.textContent ? el.textContent : null; }",
                    nonce,
                )
                return json.loads(out) if out else None

            while time.time() < deadline:
                if vstate["freq_penalty"]:
                    logger.error("[CAPTCHA] aborting: Tencent frequency penalty (errorCode 12). "
                                 "Stop retrying for a while or use a different IP / paid solver.")
                    _keep_open_if_requested(page, session_dir)
                    browser.close()
                    return {"ok": False, "error": "frequency_penalty", "network": network}
                res = _poll_token()
                if res:
                    if res.get("ok"):
                        token = res.get("tok")
                        logger.info("[CAPTCHA] newRiskControl resolved: %s", json.dumps(token))
                        break
                    if res.get("error"):
                        logger.warning("[CAPTCHA] newRiskControl error: %s", res)
                        break

                sel = _find_slider_container(page)
                if not sel:
                    page.wait_for_timeout(500)
                    continue

                if _CAPTCHA_MANUAL:
                    # Don't drag — just save a gap-detection diagnostic once, then
                    # wait for the human to solve it.
                    if not diag_done and captcha_imgs.get("1"):
                        try:
                            captcha_solver.detect_gap_offset(
                                captcha_imgs["1"],
                                save_debug_path=os.path.join(session_dir, "captcha_bg_1.png"),
                            )
                        except Exception:
                            pass
                        diag_done = True
                    page.wait_for_timeout(1000)
                    continue

                if attempts >= max_attempts:
                    # Don't keep dragging — that's what triggers "Operation too
                    # often". Just wait for the pending verify to resolve.
                    page.wait_for_timeout(1000)
                    continue

                attempts += 1
                # Human settle before grabbing the handle (instant drags look botty
                # and rapid retries get rate-limited).
                page.wait_for_timeout(random.randint(1300, 2600))
                logger.info("[CAPTCHA] slider visible (%s), solve attempt %d/%d", sel, attempts, max_attempts)
                bg = captcha_imgs.get("1") or captcha_imgs.get("0")
                if bg:
                    scale = float(os.getenv("MIDASBUY_CAPTCHA_SCALE", "0.5"))
                    x_off = int(os.getenv("MIDASBUY_CAPTCHA_X_OFFSET", "0"))
                    captcha_solver.solve_from_clean_bg(page, sel, bg, session_dir, scale, x_off, attempts)
                else:
                    captcha_solver.solve_slider_in_container(page, sel, session_dir, attempt=attempts)

                # Wait for this attempt's verify result before trying again.
                waited = 0
                while waited < 9000 and time.time() < deadline:
                    res = _poll_token()
                    if res:
                        break
                    page.wait_for_timeout(500)
                    waited += 500
                if not token and attempts < max_attempts:
                    # Back off well before the next attempt to avoid the frequency cap.
                    page.wait_for_timeout(random.randint(4000, 6500))

            _save_debug(page, session_dir, "captcha_after_solve")

            if not token:
                logger.error("[CAPTCHA] no token obtained (attempts=%d)", attempts)
                _keep_open_if_requested(page, session_dir)
                browser.close()
                return {"ok": False, "error": "captcha_unsolved", "attempts": attempts, "network": network}

            rc_token = (token or {}).get("rc_token") or (token or {}).get("ticket")
            rc_uuid  = (token or {}).get("rc_uuid")
            retry_payload = dict(payload)
            retry_payload["rc_token"] = rc_token
            retry_payload["rc_uuid"]  = rc_uuid
            retry_payload["verifyData"] = {"ticket": rc_token, "randstr": rc_uuid}

            logger.info("[CAPTCHA] retrying query with captcha token")
            retry = page.evaluate(_JS_CALL_API, {
                "payloadJson": json.dumps(retry_payload, separators=(",", ":")),
                "endpoint":    endpoint,
                "method":      "POST",
            })
            _save_debug(page, session_dir, "captcha_retry_done")
            _keep_open_if_requested(page, session_dir)
            browser.close()

            data = (retry or {}).get("data") if isinstance(retry, dict) else None
            logger.info("[CAPTCHA] retry result ret=%s", (data or {}).get("ret") if isinstance(data, dict) else "n/a")
            return {"ok": True, "token": token, "retry": retry, "data": data, "network": network}

    except Exception:
        logger.exception("[CAPTCHA] solve_redeem_captcha_and_retry crashed")
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
