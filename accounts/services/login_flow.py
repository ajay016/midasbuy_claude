import logging
from dataclasses import dataclass

from .page_helpers import (
    click_if_visible,
    fill_input,
    get_iframe_by_selector,
    text_contains,
    wait_for_visible,
)
from .selectors import (
    MIDASBUY_CONTINUE_BUTTON,
    MIDASBUY_COOKIE_ACCEPT,
    MIDASBUY_COOKIE_POPUP,
    MIDASBUY_DROPDOWN_SIGN_IN,
    MIDASBUY_EMAIL_INPUT,
    MIDASBUY_HEADER,
    MIDASBUY_HEADER_USER_NAME,
    MIDASBUY_KEEP_SIGNED_IN,
    MIDASBUY_LOGIN_IFRAME,
    MIDASBUY_NAV_SIGN_IN,
    MIDASBUY_OPTIONAL_MODAL_CLOSE,
    MIDASBUY_OPTIONAL_MODAL_CLOSE_ALT,
    MIDASBUY_PASSWORD_INPUT,
    MIDASBUY_SIGN_IN_BUTTON,
    MIDASBUY_SIGN_IN_SUCCESS_TEXT,
    MIDASBUY_SUCCESS_CLOSE_BUTTON,
)

logger = logging.getLogger(__name__)


@dataclass
class FlowResult:
    success: bool
    message: str
    step: str


def _safe_log_page_state(page, label: str) -> None:
    try:
        logger.info("[MIDASBUY] %s current_url=%s", label, page.url)
    except Exception:
        logger.exception("[MIDASBUY] failed logging page url for %s", label)

    try:
        html = page.content()
        logger.info("[MIDASBUY] %s page html length=%s", label, len(html))
    except Exception:
        logger.exception("[MIDASBUY] failed logging page html for %s", label)


def _dump_frame_debug(login_iframe) -> None:
    try:
        html = login_iframe.content()
        logger.info("[MIDASBUY] iframe html length=%s", len(html))
        logger.info(
            "[MIDASBUY] iframe markers email_input=%s password_input=%s continue_text=%s signin_text=%s keep_signed_in=%s success_text=%s",
            'autocomplete="username"' in html,
            'type="password"' in html,
            "Continue" in html,
            "SIGN IN" in html,
            "Keep me signed in" in html,
            "SIGN IN SUCCESSFULLY" in html,
        )
    except Exception:
        logger.exception("[MIDASBUY] failed to inspect iframe html")


# Promo popup ("PUBG MOBILE POINTS MALL..." etc.): an image card plus a
# full-screen backdrop (div.pop_bg_*) that intercepts EVERY click on the page.
# It is injected ~3s+ after load, so a single early check misses it.
_PROMO_CLOSE = 'div[class*="overlay"] div[class*="pop_content"] > div[class*="close_btn"]'
_PROMO_BACKDROP = 'div[class*="overlay"] > div[class*="pop_bg"]'


def dismiss_promo_overlay(page, wait_ms: int = 8000) -> bool:
    """Wait up to ``wait_ms`` for the late promo popup, close it via its own X,
    and confirm the click-blocking backdrop is gone. Returns True if the page is
    clear of it (closed, or it never appeared)."""
    backdrop = page.locator(_PROMO_BACKDROP).filter(visible=True)
    close = page.locator(_PROMO_CLOSE).filter(visible=True).first
    try:
        close.wait_for(state="visible", timeout=wait_ms)
    except Exception:
        if backdrop.count() == 0:
            return True  # no promo this time
    for attempt in range(3):
        try:
            if backdrop.count() == 0:
                return True
            close.click(timeout=2000)
            logger.info("[MIDASBUY] promo popup closed (attempt %s)", attempt + 1)
            backdrop.first.wait_for(state="hidden", timeout=3000)
            return True
        except Exception as exc:
            logger.warning("[MIDASBUY] promo close attempt %s failed: %s", attempt + 1, str(exc)[:200])
            page.wait_for_timeout(500)
    logger.error("[MIDASBUY] promo backdrop still blocking clicks")
    return False


def close_optional_home_popups(page) -> None:
    for attempt in range(2):
        logger.info("[MIDASBUY] popup close pass=%s", attempt + 1)
        closed_any = False

        if click_if_visible(page, MIDASBUY_OPTIONAL_MODAL_CLOSE_ALT, timeout=700):
            logger.info("[MIDASBUY] closed popup via ALT close button")
            page.wait_for_timeout(200)
            closed_any = True

        if click_if_visible(page, MIDASBUY_OPTIONAL_MODAL_CLOSE, timeout=700):
            logger.info("[MIDASBUY] closed popup via old popup selector")
            page.wait_for_timeout(200)
            closed_any = True

        if click_if_visible(page, "div[class*='close_btn']", timeout=700):
            logger.info("[MIDASBUY] closed popup via generic close button")
            page.wait_for_timeout(200)
            closed_any = True

        if not closed_any:
            logger.info("[MIDASBUY] no optional popup closed in pass=%s", attempt + 1)
            break


def click_sign_in_button(page) -> bool:
    logger.info("[MIDASBUY] trying primary navbar sign-in selector: %s", MIDASBUY_NAV_SIGN_IN)
    if click_if_visible(page, MIDASBUY_NAV_SIGN_IN, timeout=3000):
        logger.info("[MIDASBUY] navbar Sign in clicked via primary selector")
        return True

    logger.info("[MIDASBUY] trying fallback: div[data-id='login']")
    if click_if_visible(page, "div[data-id='login']", timeout=2000):
        logger.info("[MIDASBUY] navbar Sign in clicked via data-id selector")
        return True

    logger.info("[MIDASBUY] trying fallback: div[class*='sign_in']")
    if click_if_visible(page, "div[class*='sign_in']", timeout=2000):
        logger.info("[MIDASBUY] navbar Sign in clicked via class pattern")
        return True

    logger.info("[MIDASBUY] trying fallback: click by text 'Sign in'")
    try:
        sign_in_by_text = page.get_by_text("Sign in", exact=True).first
        if sign_in_by_text.is_visible(timeout=2000):
            sign_in_by_text.click()
            logger.info("[MIDASBUY] navbar Sign in clicked via text match")
            return True
    except Exception as e:
        logger.debug("[MIDASBUY] text click failed: %s", e)

    logger.info("[MIDASBUY] trying JavaScript click on div[data-id='login']")
    try:
        result = page.evaluate("""
            () => {
                const btn = document.querySelector('div[data-id="login"]');
                if (btn) {
                    btn.click();
                    return true;
                }
                return false;
            }
        """)
        if result:
            logger.info("[MIDASBUY] navbar Sign in clicked via JavaScript")
            return True
    except Exception as e:
        logger.debug("[MIDASBUY] JavaScript click failed: %s", e)

    logger.error("[MIDASBUY] all sign-in button strategies failed")
    return False


def _login_dialog_opened(page, timeout_ms: int) -> bool:
    """The login iframe is ALWAYS in the DOM (hidden). Only a visible iframe means
    the click actually opened the dialog."""
    try:
        page.locator(MIDASBUY_LOGIN_IFRAME).first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


def click_dropdown_sign_in(page) -> bool:
    """Click "Sign in / Sign up" in the header dropdown and CONFIRM the login
    dialog opened. A click can "succeed" without opening it (e.g. on the outer
    wrapper), which previously surfaced later as "email input not visible"."""
    strategies = [
        # The inner label is what the site's click handler responds to.
        ("text", lambda: page.get_by_text("Sign in / Sign up", exact=True).filter(visible=True).first),
        ("dropdown selector", lambda: page.locator(MIDASBUY_DROPDOWN_SIGN_IN).filter(visible=True).first),
        ("sidebar pattern", lambda: page.locator("div[class*='ulContent'] div[class*='btn_wrap']").filter(visible=True).first),
    ]
    for name, make in strategies:
        # A previous click may have opened the dialog slowly (Docker/Xvfb is much
        # slower than a desktop browser); its full-screen iframe then blocks clicks.
        if _login_dialog_opened(page, timeout_ms=500):
            logger.info("[MIDASBUY] login dialog already open")
            return True
        try:
            target = make()
            target.wait_for(state="visible", timeout=2000)
            target.click(timeout=3000)
        except Exception as e:
            logger.info("[MIDASBUY] dropdown '%s' click failed: %s", name, str(e)[:200])
            continue
        if _login_dialog_opened(page, timeout_ms=15000):
            logger.info("[MIDASBUY] login dialog opened via dropdown '%s'", name)
            return True
        logger.warning("[MIDASBUY] dropdown '%s' clicked but login dialog did not open", name)
        # The dropdown may have closed; reopen it before the next strategy.
        if not page.get_by_text("Sign in / Sign up", exact=True).filter(visible=True).count():
            click_sign_in_button(page)
            page.wait_for_timeout(400)

    logger.error("[MIDASBUY] all dropdown sign-in strategies failed to open the login dialog")
    return False


def _header_shows_account(page, email: str) -> bool:
    """The page header lists the signed-in account's email (and drops the Sign in
    button) only after a real login. Reliable, unlike URL or cookie heuristics."""
    try:
        name = page.locator(MIDASBUY_HEADER_USER_NAME).first
        text = (name.text_content(timeout=1000) or "").strip().lower()
        if email and email.lower() in text:
            return True
        header = page.locator(MIDASBUY_HEADER).first
        return bool(text) and header.locator('div[data-id="login"]').count() == 0
    except Exception:
        return False


def _check_login_success(login_iframe, page, email: str = "", timeout_ms: int = 15000) -> bool:
    """True only on positive evidence of a login: the iframe's "SIGN IN
    SUCCESSFULLY" message, or the header showing the account. The old fallbacks
    (URL has no "login", any cookie containing "midas", generic avatar divs) were
    always true on the home page, so a FAILED login was reported as success and a
    logged-out session got saved."""
    waited = 0
    while waited <= timeout_ms:
        if text_contains(login_iframe, MIDASBUY_SIGN_IN_SUCCESS_TEXT, "SIGN IN SUCCESSFULLY", timeout=1000):
            logger.info("[MIDASBUY] success modal detected via iframe text")
            return True
        if _header_shows_account(page, email):
            logger.info("[MIDASBUY] header shows the signed-in account")
            return True
        page.wait_for_timeout(1000)
        waited += 2000
    return False


def _wait_for_email_or_fail(login_iframe) -> bool:
    selectors = [
        MIDASBUY_EMAIL_INPUT,
        'input[type="email"]',
        'input[autocomplete="username"]',
    ]

    # The dialog's iframe content can take a few seconds to render after the
    # dialog opens, so give the first (exact) selector a longer window.
    for i, selector in enumerate(selectors):
        logger.info("[MIDASBUY] trying email selector=%s", selector)
        if wait_for_visible(login_iframe, selector, timeout=8000 if i == 0 else 1800):
            logger.info("[MIDASBUY] email selector visible=%s", selector)
            return True

    return False


def _fill_email(login_iframe, email: str) -> bool:
    selectors = [
        MIDASBUY_EMAIL_INPUT,
        'input[type="email"]',
        'input[autocomplete="username"]',
    ]

    for selector in selectors:
        logger.info("[MIDASBUY] trying fill email selector=%s", selector)
        if fill_input(login_iframe, selector, email, timeout=1800):
            logger.info("[MIDASBUY] email filled via selector=%s", selector)
            return True

    return False


def _click_continue(login_iframe) -> bool:
    selectors = [
        MIDASBUY_CONTINUE_BUTTON,
        'div.btn.comfirm-btn:has-text("Continue")',
        'text=Continue',
        'button:has-text("Continue")',
    ]

    for selector in selectors:
        logger.info("[MIDASBUY] trying continue selector=%s", selector)
        if click_if_visible(login_iframe, selector, timeout=1800):
            logger.info("[MIDASBUY] Continue clicked via selector=%s", selector)
            return True

    return False


def _wait_for_password(login_iframe) -> bool:
    selectors = [
        MIDASBUY_PASSWORD_INPUT,
        'input[type="password"]',
    ]

    for selector in selectors:
        logger.info("[MIDASBUY] trying password selector=%s", selector)
        if wait_for_visible(login_iframe, selector, timeout=2500):
            logger.info("[MIDASBUY] password selector visible=%s", selector)
            return True

    return False


def _fill_password(login_iframe, password: str) -> bool:
    selectors = [
        MIDASBUY_PASSWORD_INPUT,
        'input[type="password"]',
    ]

    for selector in selectors:
        logger.info("[MIDASBUY] trying fill password selector=%s", selector)
        if fill_input(login_iframe, selector, password, timeout=1800):
            logger.info("[MIDASBUY] password filled via selector=%s", selector)
            return True

    return False


def _click_keep_signed_in(login_iframe) -> None:
    selectors = [
        MIDASBUY_KEEP_SIGNED_IN,
        'div.keep:has-text("Keep me signed in")',
        'div.keep',
    ]

    for selector in selectors:
        logger.info("[MIDASBUY] trying keep-signed-in selector=%s", selector)
        if click_if_visible(login_iframe, selector, timeout=1000):
            logger.info("[MIDASBUY] Keep me signed in clicked via selector=%s", selector)
            return


def _click_sign_in(login_iframe) -> bool:
    selectors = [
        MIDASBUY_SIGN_IN_BUTTON,
        'div.btn.comfirm-btn:has-text("SIGN IN")',
        'text=SIGN IN',
        'button:has-text("SIGN IN")',
    ]

    for selector in selectors:
        logger.info("[MIDASBUY] trying sign-in selector=%s", selector)
        if click_if_visible(login_iframe, selector, timeout=1800):
            logger.info("[MIDASBUY] SIGN IN clicked via selector=%s", selector)
            return True

    return False


def _close_success_modal(login_iframe) -> None:
    selectors = [
        MIDASBUY_SUCCESS_CLOSE_BUTTON,
        'div.close-btn',
        'div[class*="close"]',
    ]

    # Since ~Oct 2026 the success popup also offers "Create Passkey"
    # (iframe hash #webauthn-sign-up). It's optional: close it, never accept it.
    for selector in selectors:
        logger.info("[MIDASBUY] trying success-close selector=%s", selector)
        if click_if_visible(login_iframe, selector, timeout=1000):
            logger.info("[MIDASBUY] success modal closed via selector=%s", selector)
            return


def run_midasbuy_login_flow(page, account) -> FlowResult:
    logger.info("[MIDASBUY] starting login flow account_id=%s email=%s", account.id, account.email)
    _safe_log_page_state(page, "flow_start")

    page.wait_for_timeout(800)

    dismiss_promo_overlay(page)
    close_optional_home_popups(page)

    if wait_for_visible(page, MIDASBUY_COOKIE_POPUP, timeout=1500):
        logger.info("[MIDASBUY] cookie popup visible")
        if click_if_visible(page, MIDASBUY_COOKIE_ACCEPT, timeout=1500):
            logger.info("[MIDASBUY] cookie accept clicked")
            page.wait_for_timeout(300)
        else:
            logger.warning("[MIDASBUY] cookie popup visible but accept click failed")
    else:
        logger.info("[MIDASBUY] cookie popup not shown")

    close_optional_home_popups(page)
    dismiss_promo_overlay(page, wait_ms=500)

    if not click_sign_in_button(page):
        _safe_log_page_state(page, "navbar_signin_failed")
        return FlowResult(False, "Could not click navbar Sign in.", "navbar_sign_in")

    logger.info("[MIDASBUY] navbar Sign in clicked")
    page.wait_for_timeout(400)
    dismiss_promo_overlay(page, wait_ms=500)

    if not click_dropdown_sign_in(page):
        _safe_log_page_state(page, "dropdown_signin_failed")
        return FlowResult(False, "Could not click dropdown Sign in / Sign up.", "dropdown_sign_in")

    logger.info("[MIDASBUY] dropdown Sign in / Sign up clicked")
    page.wait_for_timeout(900)

    logger.info("[MIDASBUY] waiting for login iframe selector=%s", MIDASBUY_LOGIN_IFRAME)
    login_iframe = get_iframe_by_selector(page, MIDASBUY_LOGIN_IFRAME, timeout=5000)
    if not login_iframe:
        logger.error("[MIDASBUY] login iframe not found")
        _safe_log_page_state(page, "iframe_not_found")
        return FlowResult(False, "Login iframe not found.", "login_iframe")

    logger.info("[MIDASBUY] login iframe found")
    page.wait_for_timeout(500)
    _dump_frame_debug(login_iframe)

    logger.info("[MIDASBUY] waiting for email input inside iframe")
    if not _wait_for_email_or_fail(login_iframe):
        logger.error("[MIDASBUY] email input not visible inside iframe")
        _dump_frame_debug(login_iframe)
        return FlowResult(False, "Email input not visible inside iframe.", "email_input_visible")

    logger.info("[MIDASBUY] filling email=%s", account.email)
    if not _fill_email(login_iframe, account.email):
        logger.error("[MIDASBUY] failed filling email input")
        _dump_frame_debug(login_iframe)
        return FlowResult(False, "Could not fill email field.", "email_input")

    page.wait_for_timeout(250)

    logger.info("[MIDASBUY] clicking Continue")
    if not _click_continue(login_iframe):
        logger.error("[MIDASBUY] failed clicking Continue")
        _dump_frame_debug(login_iframe)
        return FlowResult(False, "Could not click Continue button.", "continue_button")

    page.wait_for_timeout(900)
    _dump_frame_debug(login_iframe)

    logger.info("[MIDASBUY] waiting for password field")
    if not _wait_for_password(login_iframe):
        logger.error("[MIDASBUY] password input not visible after Continue")
        _dump_frame_debug(login_iframe)
        return FlowResult(False, "Password input not visible.", "password_input_visible")

    logger.info("[MIDASBUY] filling password")
    if not _fill_password(login_iframe, account.user_password):
        logger.error("[MIDASBUY] failed filling password")
        _dump_frame_debug(login_iframe)
        return FlowResult(False, "Could not fill password field.", "password_input")

    page.wait_for_timeout(250)

    logger.info("[MIDASBUY] trying Keep me signed in")
    _click_keep_signed_in(login_iframe)

    page.wait_for_timeout(200)

    logger.info("[MIDASBUY] clicking SIGN IN")
    if not _click_sign_in(login_iframe):
        logger.error("[MIDASBUY] failed clicking SIGN IN")
        _dump_frame_debug(login_iframe)
        return FlowResult(False, "Could not click SIGN IN button.", "sign_in_button")

    logger.info("[MIDASBUY] SIGN IN clicked, waiting for login completion")
    page.wait_for_timeout(3000)

    if _check_login_success(login_iframe, page, account.email):
        logger.info("[MIDASBUY] login successful detected!")

        _close_success_modal(login_iframe)

        page.wait_for_timeout(1000)
        _safe_log_page_state(page, "flow_done")

        logger.info("[MIDASBUY] login flow completed successfully")
        return FlowResult(True, "Midasbuy login flow completed successfully.", "done")
    else:
        logger.error("[MIDASBUY] login success not detected")
        _dump_frame_debug(login_iframe)
        return FlowResult(False, "Sign-in success not detected.", "sign_in_success")
