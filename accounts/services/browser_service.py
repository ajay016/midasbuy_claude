from django.conf import settings


def get_browser_launch_options() -> dict:
    return {
        "headless": getattr(settings, "MIDASBUY_BROWSER_HEADLESS", True),
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    }


def get_browser_context_options() -> dict:
    options = {
        "viewport": getattr(
            settings,
            "MIDASBUY_BROWSER_VIEWPORT",
            {"width": 1440, "height": 900},
        ),
    }
    user_agent = getattr(settings, "MIDASBUY_BROWSER_USER_AGENT", "")
    if user_agent:
        options["user_agent"] = user_agent
    return options


# ── Login display fallback (Docker) ────────────────────────────────────────────
# In Docker the headful login browser uses $DISPLAY. When that points at an X
# server on the host (LOGIN_DISPLAY=host.docker.internal:0.0 -> VcXsrv) that
# isn't running/reachable, Chromium exits with "Missing X server" and the login
# crashed with HTTP 500. Fall back to a private virtual display instead.
_FALLBACK_DISPLAY = ":98"


def _is_missing_x_server(exc: Exception) -> bool:
    text = str(exc)
    return "Missing X server" in text or "without having a XServer" in text


def _display_alive(display: str) -> bool:
    """True only if an X server actually ANSWERS on ``display``. A socket file
    alone isn't enough: a dead Xvfb leaves /tmp/.X11-unix/X<n> behind."""
    import socket

    path = f"/tmp/.X11-unix/X{display.lstrip(':')}"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            sock.connect(path)
        return True
    except OSError:
        return False


def _start_fallback_display() -> bool:
    """Start Xvfb on _FALLBACK_DISPLAY unless one is already answering. Linux only."""
    import os
    import shutil
    import subprocess
    import time

    num = _FALLBACK_DISPLAY.lstrip(":")
    if _display_alive(_FALLBACK_DISPLAY):
        return True
    if not shutil.which("Xvfb"):
        return False
    # Clear leftovers of a dead Xvfb, otherwise the new one refuses to start.
    for stale in (f"/tmp/.X{num}-lock", f"/tmp/.X11-unix/X{num}"):
        try:
            os.remove(stale)
        except OSError:
            pass
    subprocess.Popen(
        ["Xvfb", _FALLBACK_DISPLAY, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,  # survive the request thread / reloader
    )
    for _ in range(30):
        if _display_alive(_FALLBACK_DISPLAY):
            return True
        time.sleep(0.1)
    return False


def launch_login_browser(playwright):
    """Launch the (headful) login browser, falling back to an invisible virtual
    display when the configured $DISPLAY has no reachable X server."""
    import logging
    import os

    log = logging.getLogger(__name__)
    options = get_browser_launch_options()
    try:
        return playwright.chromium.launch(**options)
    except Exception as exc:
        if options.get("headless") or not _is_missing_x_server(exc):
            raise
        log.warning(
            "[MIDASBUY] no X server at DISPLAY=%s (is XLaunch/VcXsrv running with "
            "'Disable access control'?) - logging in on an invisible display instead",
            os.environ.get("DISPLAY"),
        )
        if not _start_fallback_display():
            raise
        env = dict(os.environ, DISPLAY=_FALLBACK_DISPLAY)
        return playwright.chromium.launch(**options, env=env)
