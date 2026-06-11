"""
No-browser xMidas encryption via a Node subprocess.

window.xMidas (the Midasbuy "Chaos VM" whitebox cipher) is pure JS — it runs in
plain Node with a tiny DOM shim, ~5 ms per call, with no Playwright/browser. This
module shells out to accounts/services/xmidas/xmidas_runner.js to produce the
real `encrypt_msg`/`ctoken` for an API payload.

It is the encryption half of a potential pure-HTTP request path. The transport
half (sending the request without a browser) is gated separately because
Midasbuy's edge may 403 non-browser clients depending on IP/cookies — callers
should keep a browser fallback.
"""
import json
import logging
import os
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

_RUNNER = os.path.join(os.path.dirname(__file__), "xmidas", "xmidas_runner.js")


def _node_bin() -> Optional[str]:
    return (
        os.getenv("MIDASBUY_NODE_BIN")
        or shutil.which("node")
        or ("/opt/node22/bin/node" if os.path.exists("/opt/node22/bin/node") else None)
    )


def is_available() -> bool:
    """True if Node and the runner script are both present."""
    return bool(_node_bin()) and os.path.exists(_RUNNER)


def encrypt_payload(
    payload: dict,
    token: str,
    version: str = "1.0.1",
    timeout: float = 15.0,
) -> Optional[dict]:
    """
    Encrypt a full payload with the real xMidas VM.

    Returns {"encrypt_msg", "ctoken", "ctoken_ver"} ready to POST, or None on
    failure (so callers can fall back to the browser path).
    """
    node = _node_bin()
    if not node:
        logger.warning("[XMIDAS-NODE] node binary not found — cannot encrypt without browser")
        return None
    if not token:
        logger.warning("[XMIDAS-NODE] no xMidas token provided")
        return None
    if not os.path.exists(_RUNNER):
        logger.warning("[XMIDAS-NODE] runner script missing: %s", _RUNNER)
        return None

    req = json.dumps({"token": token, "version": version, "payload": payload})
    try:
        proc = subprocess.run(
            [node, _RUNNER],
            input=req,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("[XMIDAS-NODE] encryption timed out after %ss", timeout)
        return None
    except Exception as exc:
        logger.warning("[XMIDAS-NODE] node invocation failed: %s", exc)
        return None

    if proc.returncode != 0:
        logger.warning("[XMIDAS-NODE] node exited %s stderr=%s", proc.returncode, proc.stderr[:300])
        return None

    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as exc:
        logger.warning("[XMIDAS-NODE] could not parse node output (%s): %s", exc, proc.stdout[:200])
        return None

    if not out.get("ok"):
        logger.warning("[XMIDAS-NODE] encryption error: %s", out.get("error"))
        return None

    return {
        "encrypt_msg": out["encrypt_msg"],
        "ctoken": out["ctoken"],
        "ctoken_ver": out["ctoken_ver"],
    }
