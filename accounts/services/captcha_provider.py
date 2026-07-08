"""
Paid captcha provider clients (Tencent TCaptcha / TenDI).

Default provider: DecodeCaptcha (https://decodecaptcha.com) — a single GET returns
the decoded {ticket, randstr}, which we submit to Midasbuy's CommonCheck. 2Captcha
is still supported as a fallback (CAPTCHA_PROVIDER=2captcha).

Config (env):
  CAPTCHA_PROVIDER   = "decodecaptcha"  (default; or "2captcha")
  CAPTCHA_API_KEY    = "<your key>"     (required — the provider account key)
  CAPTCHA_APP_ID     = "188937037"      (TCaptcha aid from the slider URL)

DecodeCaptcha-only:
  CAPTCHA_DOMAIN     = "https://turing.captcha.qcloud.com"  (Tencent captcha domain)
  CAPTCHA_ENTRY_URL  = "" (optional; url-encoded entry_url from the prehandle request)
  CAPTCHA_USER_AGENT = "" (optional; modern browser UA — improves success rate)
  CAPTCHA_PROXY_TYPE / _ADDRESS / _PORT / _USER / _PASS  (optional; residential proxy
                       strongly recommended by the provider to avoid risk control)

Zero extra deps — uses urllib from the stdlib.
"""
import json
import logging
import os
import time
import urllib.request
from typing import Optional
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

_2CAPTCHA_BASE = os.getenv("CAPTCHA_2CAPTCHA_BASE", "https://api.2captcha.com")
_DECODECAPTCHA_BASE = os.getenv("CAPTCHA_DECODE_BASE", "https://api.decodecaptcha.com")
_DEFAULT_APP_ID = os.getenv("CAPTCHA_APP_ID", "188937037")


def _post_json(url: str, payload: dict, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def solve_tencent_2captcha(
    api_key: str,
    app_id: str,
    website_url: str,
    *,
    poll_timeout: int = 180,
    poll_interval: int = 5,
    retries: int = 3,
) -> Optional[dict]:
    """
    Solve Tencent TCaptcha via 2Captcha. Returns {'ticket','randstr','appid'} or None.
    Retries on ERROR_CAPTCHA_UNSOLVABLE (often transient / worker-dependent).
    """
    # Our site uses the GLOBAL TCaptcha, not the mainland turing.captcha default —
    # passing the wrong script makes 2Captcha render a different captcha and fail.
    captcha_script = os.getenv(
        "CAPTCHA_SCRIPT", "https://global.captcha.gtimg.com/TCaptcha-global.js"
    )
    for attempt in range(1, retries + 1):
        try:
            task = {
                "type": "TencentTaskProxyless",
                "appId": str(app_id),
                "websiteURL": website_url,
            }
            if captcha_script:
                task["captchaScript"] = captcha_script
            created = _post_json(f"{_2CAPTCHA_BASE}/createTask", {
                "clientKey": api_key,
                "task": task,
            })
            if created.get("errorId"):
                logger.error("[CAPTCHA] 2captcha createTask error: %s", created)
                return None
            task_id = created.get("taskId")
            logger.info("[CAPTCHA] 2captcha task created id=%s (attempt %d/%d)", task_id, attempt, retries)

            deadline = time.time() + poll_timeout
            unsolvable = False
            while time.time() < deadline:
                time.sleep(poll_interval)
                res = _post_json(f"{_2CAPTCHA_BASE}/getTaskResult", {
                    "clientKey": api_key, "taskId": task_id,
                })
                err = res.get("errorCode") or ""
                if err == "ERROR_CAPTCHA_UNSOLVABLE":
                    logger.warning("[CAPTCHA] 2captcha unsolvable (attempt %d/%d) — retrying", attempt, retries)
                    unsolvable = True
                    break
                if res.get("errorId"):
                    logger.error("[CAPTCHA] 2captcha getTaskResult error: %s", res)
                    return None
                if res.get("status") == "ready":
                    sol = res.get("solution", {}) or {}
                    logger.info(
                        "[CAPTCHA] 2captcha solved ret=%s cost=%s ticket_len=%s",
                        sol.get("ret"), res.get("cost"), len(str(sol.get("ticket") or "")),
                    )
                    return {
                        "ret": sol.get("ret", 0),
                        "ticket": sol.get("ticket"),
                        "randstr": sol.get("randstr"),
                        "appid": sol.get("appid") or app_id,
                    }
                logger.info("[CAPTCHA] 2captcha task pending...")
            if not unsolvable:
                logger.error("[CAPTCHA] 2captcha timed out after %ss", poll_timeout)
                return None
        except Exception:
            logger.exception("[CAPTCHA] 2captcha solve failed")
            return None
    logger.error("[CAPTCHA] 2captcha could not solve after %d attempts", retries)
    return None


def _decode_proxy_from_env() -> dict:
    """Optional proxy params for DecodeCaptcha (a residential proxy markedly raises
    the success rate). Returns {} when none configured."""
    ptype = os.getenv("CAPTCHA_PROXY_TYPE")
    paddr = os.getenv("CAPTCHA_PROXY_ADDRESS")
    if not (ptype and paddr):
        return {}
    proxy = {"proxy_type": ptype, "proxy_address": paddr}
    for env_key, param in (
        ("CAPTCHA_PROXY_PORT", "proxy_port"),
        ("CAPTCHA_PROXY_USER", "proxy_user"),
        ("CAPTCHA_PROXY_PASS", "proxy_pass"),
    ):
        val = os.getenv(env_key)
        if val:
            proxy[param] = val
    return proxy


def solve_tencent_decodecaptcha(
    api_key: str,
    aid: str,
    domain: str,
    *,
    entry_url: Optional[str] = None,
    user_agent: Optional[str] = None,
    proxy: Optional[dict] = None,
    timeout: int = 60,
    retries: int = 3,
) -> Optional[dict]:
    """
    Solve Tencent TCaptcha via DecodeCaptcha (https://api.decodecaptcha.com/tencent).
    One GET returns {"msg":"success","token":{"ticket","randstr",...}}. Returns
    {'ret','ticket','randstr','appid','user_agent'} or None.
    """
    params = {"key": api_key, "aid": str(aid), "domain": domain}
    if entry_url:
        params["entry_url"] = entry_url
    if user_agent:
        params["user_agent"] = user_agent
    if proxy:
        params.update(proxy)
    url = f"{_DECODECAPTCHA_BASE}/tencent?" + urlencode(params)

    for attempt in range(1, retries + 1):
        try:
            resp = _get_json(url, timeout=timeout)
            msg = str(resp.get("msg") or "").lower()
            if msg == "success":
                tok = resp.get("token") or {}
                ticket = tok.get("ticket")
                if ticket:
                    logger.info(
                        "[CAPTCHA] decodecaptcha solved ticket_len=%s randstr=%s",
                        len(str(ticket)), tok.get("randstr"),
                    )
                    return {
                        "ret": 0,
                        "ticket": ticket,
                        "randstr": tok.get("randstr"),
                        "appid": tok.get("aid") or aid,
                        "user_agent": resp.get("user_agent"),
                    }
                logger.error("[CAPTCHA] decodecaptcha success but no ticket: %s", resp)
                return None
            # "error" is usually a transient proxy/risk-control miss — retry.
            logger.warning(
                "[CAPTCHA] decodecaptcha msg=%s (attempt %d/%d)",
                resp.get("msg"), attempt, retries,
            )
        except Exception:
            logger.exception(
                "[CAPTCHA] decodecaptcha request failed (attempt %d/%d)", attempt, retries
            )
        time.sleep(2)
    logger.error("[CAPTCHA] decodecaptcha could not solve after %d attempts", retries)
    return None


def is_enabled() -> bool:
    return bool(os.getenv("CAPTCHA_API_KEY"))


def solve_tencent(website_url: str, app_id: Optional[str] = None) -> Optional[dict]:
    """
    Provider-agnostic entry point. Returns {'ticket','randstr','appid'} or None.
    Enabled only when CAPTCHA_API_KEY is set.
    """
    api_key = os.getenv("CAPTCHA_API_KEY", "")
    if not api_key:
        return None
    provider = os.getenv("CAPTCHA_PROVIDER", "decodecaptcha").lower()
    app_id = app_id or _DEFAULT_APP_ID

    if provider in ("decodecaptcha", "decode"):
        domain = os.getenv("CAPTCHA_DOMAIN", "https://turing.captcha.qcloud.com")
        return solve_tencent_decodecaptcha(
            api_key, app_id, domain,
            entry_url=os.getenv("CAPTCHA_ENTRY_URL") or None,
            user_agent=os.getenv("CAPTCHA_USER_AGENT") or None,
            proxy=_decode_proxy_from_env() or None,
        )

    if provider in ("2captcha", "twocaptcha"):
        return solve_tencent_2captcha(api_key, app_id, website_url)

    logger.error(
        "[CAPTCHA] unknown CAPTCHA_PROVIDER=%s (supported: decodecaptcha, 2captcha)",
        provider,
    )
    return None
