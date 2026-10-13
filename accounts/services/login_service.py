from dataclasses import dataclass
import logging
import os
import shutil
from typing import Optional

from django.conf import settings
from django.utils import timezone

from accounts.models import MidasbuyAccount, MidasbuyLoginAttempt
from accounts.utils import (
    ensure_clean_session_dir,
    get_cookie_file_path,
    get_midasbuy_session_dir,
    get_storage_state_file_path,
    get_token_file_path,
    load_json_file,
)
from .playwright_client import run_browser_login_session


logger = logging.getLogger(__name__)


@dataclass
class LoginResult:
    success: bool
    message: str
    session_dir: Optional[str] = None
    cookie_path: Optional[str] = None
    storage_state_path: Optional[str] = None
    token_path: Optional[str] = None
    screenshot_path: Optional[str] = None
    html_snapshot_path: Optional[str] = None
    cookie_data: Optional[list] = None
    storage_state_data: Optional[dict] = None


def login_midasbuy_account(account: MidasbuyAccount) -> LoginResult:
    logger.info(
        "[MIDASBUY] login_midasbuy_account start account_id=%s email=%s",
        account.id,
        account.email,
    )

    session_dir = get_midasbuy_session_dir(settings.BASE_DIR, account.id)
    logger.info("[MIDASBUY] session_dir=%s", session_dir)

    # Log in into a STAGING dir and only swap it in on success. Previously the
    # live session dir was wiped first, so one failed (re-)login destroyed a
    # working session that the API was actively using.
    staging_dir = session_dir + "_staging"
    ensure_clean_session_dir(staging_dir)
    logger.info("[MIDASBUY] staging dir cleaned path=%s", staging_dir)

    browser_result = run_browser_login_session(account, staging_dir)
    logger.info("[MIDASBUY] browser_result=%s", browser_result)

    if browser_result.get("success"):
        try:
            if os.path.exists(session_dir):
                shutil.rmtree(session_dir)
            os.replace(staging_dir, session_dir)
        except OSError:
            # Windows can refuse to delete a file another process has open;
            # overwrite in place instead.
            shutil.copytree(staging_dir, session_dir, dirs_exist_ok=True)
            shutil.rmtree(staging_dir, ignore_errors=True)
        logger.info("[MIDASBUY] new session swapped in path=%s", session_dir)
        for key in ("screenshot_path", "html_snapshot_path"):
            if browser_result.get(key):
                browser_result[key] = browser_result[key].replace(staging_dir, session_dir)
    else:
        # Keep the existing session untouched; debug files stay in the staging dir.
        session_dir = staging_dir

    cookie_path = get_cookie_file_path(session_dir)
    storage_state_path = get_storage_state_file_path(session_dir)
    token_path = get_token_file_path(session_dir)

    logger.info(
        "[MIDASBUY] expected files cookie=%s storage=%s token=%s",
        cookie_path,
        storage_state_path,
        token_path,
    )

    cookie_data = load_json_file(cookie_path) or []
    storage_state_data = load_json_file(storage_state_path) or {}

    logger.info(
        "[MIDASBUY] loaded cookie_count=%s storage_state_keys=%s",
        len(cookie_data),
        list(storage_state_data.keys()) if isinstance(storage_state_data, dict) else [],
    )

    result = LoginResult(
        success=browser_result["success"],
        message=browser_result["message"],
        session_dir=session_dir,
        cookie_path=cookie_path,
        storage_state_path=storage_state_path,
        token_path=token_path,
        screenshot_path=browser_result.get("screenshot_path"),
        html_snapshot_path=browser_result.get("html_snapshot_path"),
        cookie_data=cookie_data,
        storage_state_data=storage_state_data,
    )

    logger.info(
        "[MIDASBUY] login_midasbuy_account end account_id=%s success=%s message=%s",
        account.id,
        result.success,
        result.message,
    )
    return result


def apply_login_result(account: MidasbuyAccount, result: LoginResult) -> None:
    logger.info(
        "[MIDASBUY] apply_login_result start account_id=%s success=%s",
        account.id,
        result.success,
    )

    if result.success:
        account.status = 1
        account.last_error = ""
        account.last_login_at = timezone.now()
        account.session_path = result.cookie_path
        account.storage_state_path = result.storage_state_path
        account.token_path = result.token_path
        account.cookie_data = result.cookie_data or []
        account.storage_state_data = result.storage_state_data or {}
    else:
        # A failed re-login leaves the previous session files in place (see the
        # staging dir above), so an account that was logged in keeps serving.
        # The stored path may be from another OS (host vs container), so also
        # check this machine's own session dir.
        previous = account.storage_state_path
        local = get_storage_state_file_path(get_midasbuy_session_dir(settings.BASE_DIR, account.id))
        if account.status == 1 and any(p and os.path.exists(p) for p in (previous, local)):
            logger.warning("[MIDASBUY] re-login failed; keeping existing session account_id=%s",
                           account.id)
        else:
            account.status = 2
        account.last_error = result.message

    account.save(
        update_fields=[
            "status",
            "last_error",
            "last_login_at",
            "session_path",
            "storage_state_path",
            "token_path",
            "cookie_data",
            "storage_state_data",
            "updated_at",
        ]
    )

    logger.info(
        "[MIDASBUY] account saved account_id=%s status=%s session_path=%s",
        account.id,
        account.status,
        account.session_path,
    )

    MidasbuyLoginAttempt.objects.create(
        account=account,
        result="success" if result.success else "failed",
        message=result.message,
        screenshot_path=result.screenshot_path,
        html_snapshot_path=result.html_snapshot_path,
    )

    logger.info("[MIDASBUY] login attempt row created account_id=%s", account.id)


def login_account_and_persist(account: MidasbuyAccount) -> LoginResult:
    logger.info("[MIDASBUY] login_account_and_persist called account_id=%s", account.id)
    result = login_midasbuy_account(account)
    apply_login_result(account, result)
    logger.info(
        "[MIDASBUY] login_account_and_persist finished account_id=%s success=%s",
        account.id,
        result.success,
    )
    return result
