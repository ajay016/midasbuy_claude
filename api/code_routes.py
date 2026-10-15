"""
Code vault endpoints (mounted under /api/codes). See docs/CODE_VAULT.md.

The vault belongs to the BILLING user: for signed calls that's the X-Client-Id
client (or the caller itself), for the panel it's the signed-in user. Partners see
their own and their clients' codes; admins see everything.
"""
import logging

from asgiref.sync import sync_to_async
from django.conf import settings
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .dependencies import charge, require_order

logger = logging.getLogger("api")
router = APIRouter(prefix="/codes", tags=["codes"], dependencies=[Depends(require_order)])


class StoreCodesRequest(BaseModel):
    codes:        list[str] = Field(..., description="Codes to store (max 1000 per call)")
    label:        str = Field("", description="Optional note shown next to the codes")
    country_code: str = Field("bd", description="Storefront the codes are for")


class CheckCodeRequest(BaseModel):
    player_id:    str | None = Field(None, description="Player to check against "
                                     "(default: server's MIDASBUY_CODE_CHECK_PLAYER_ID)")
    country_code: str | None = Field(None, description="Default: the code's storefront")


class RedeemStoredRequest(BaseModel):
    player_id:    str = Field(..., description="PUBG Mobile UID to receive the UC")
    country_code: str | None = Field(None, description="Default: the code's storefront")


class LookupRequest(BaseModel):
    code: str = Field(..., description="Any code, stored or not")


def _viewer(identity: dict):
    from apiauth.models import User

    return User.objects.get(pk=identity.get("billing_user_id", identity["user_id"]))


def _source(identity: dict) -> str:
    return "panel" if identity.get("auth") == "jwt" else "api"


async def _load(identity: dict, code_id: int):
    from codes import services as cs

    def _get():
        viewer = _viewer(identity)
        return viewer, cs.get_code(viewer, code_id)

    viewer, sc = await sync_to_async(_get)()
    if sc is None:
        raise HTTPException(404, "Code not found.")
    return viewer, sc


@router.post("", summary="Store codes in your vault")
async def store_codes(body: StoreCodesRequest, identity: dict = Depends(require_order)):
    from codes import services as cs

    if len(body.codes) > cs.MAX_CODES_PER_REQUEST:
        raise HTTPException(400, f"At most {cs.MAX_CODES_PER_REQUEST} codes per request.")

    def _do():
        viewer = _viewer(identity)
        out = cs.add_codes(viewer, body.codes, label=body.label,
                           country_code=body.country_code, source=_source(identity))
        return {"added": len(out["added"]), "duplicates": out["duplicates"],
                "skipped_empty": out["empty"],
                "codes": [cs.code_to_dict(sc, viewer) for sc in out["added"]]}

    return await sync_to_async(_do)()


@router.get("", summary="List your stored codes")
async def list_codes(
    identity: dict = Depends(require_order),
    status: str = Query("", description="unused · unchecked · valid · used · expired · invalid"),
    search: str = Query("", description="Last 4 characters, label, or redeemed player id"),
    client_ref: str = Query("", description="Partners/admins: only this client's codes"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    from codes import services as cs

    return await sync_to_async(lambda: cs.list_codes(
        _viewer(identity), status=status, search=search, client_ref=client_ref,
        page=page, page_size=page_size))()


@router.post("/lookup", summary="Everything we know about any code")
async def lookup_code(body: LookupRequest, identity: dict = Depends(require_order)):
    from codes import services as cs

    return await sync_to_async(lambda: cs.lookup(_viewer(identity), body.code))()


@router.get("/{code_id}", summary="One stored code: full code + history")
async def get_code(code_id: int, identity: dict = Depends(require_order)):
    from codes import services as cs

    viewer, sc = await _load(identity, code_id)
    return await sync_to_async(cs.code_detail)(viewer, sc)


@router.delete("/{code_id}", summary="Remove a code from the vault (history is kept)")
async def delete_code(code_id: int, identity: dict = Depends(require_order)):
    from codes import services as cs

    viewer, sc = await _load(identity, code_id)
    await sync_to_async(cs.delete_code)(viewer, sc)
    return {"deleted": True, "id": code_id}


@router.post("/{code_id}/check", summary="Check a stored code's status on Midasbuy")
async def check_code(code_id: int, body: CheckCodeRequest | None = None,
                     identity: dict = Depends(require_order)):
    from codes import services as cs

    from .app import _report, _select_session
    from .service import check_code_status

    body = body or CheckCodeRequest()
    player_id = body.player_id or getattr(settings, "MIDASBUY_CODE_CHECK_PLAYER_ID", "")
    if not player_id:
        raise HTTPException(400, "player_id is required (Midasbuy checks a code for a "
                                 "player). Pass one, or set MIDASBUY_CODE_CHECK_PLAYER_ID.")
    viewer, sc = await _load(identity, code_id)
    code = await sync_to_async(sc.get_code)()
    country = (body.country_code or sc.country_code or "bd").lower()

    await charge(identity, 1)
    account_id, ssp, cookies = await _select_session(metered=True)
    result = await check_code_status(player_id, code, country, ssp, cookies)
    await _report(account_id, result)

    status = cs.definite_status(result.get("status"), result.get("raw"))
    await sync_to_async(cs.record_check)(code, status, actor=viewer, source="vault",
                                         product_name=result.get("product_name") or "",
                                         player_id=player_id)
    await sync_to_async(sc.refresh_from_db)()
    return {
        "status": result.get("status"),
        "message": result.get("message"),
        "product_name": result.get("product_name") or "",
        "redeemed_to": (await sync_to_async(cs.redeemed_to_for)(viewer, code)
                        if result.get("status") == "used" else None),
        "code": await sync_to_async(cs.code_to_dict)(sc, viewer),
    }


@router.post("/{code_id}/redeem", summary="Redeem a stored code to a player")
async def redeem_code(code_id: int, body: RedeemStoredRequest,
                      identity: dict = Depends(require_order)):
    from codes import services as cs

    from .app import _report, _select_session
    from .service import redeem_all_in_one

    viewer, sc = await _load(identity, code_id)
    if not await sync_to_async(cs.can_redeem)(viewer, sc.fingerprint):
        raise HTTPException(403, "You may not redeem this code.")
    previous = sc.status
    if not await sync_to_async(cs.begin_redeem)(sc):
        raise HTTPException(409, "This code is already used or being redeemed right now.")
    try:
        code = await sync_to_async(sc.get_code)()
        country = (body.country_code or sc.country_code or "bd").lower()
        await charge(identity, 1)
        account_id, ssp, cookies = await _select_session(metered=True)
        result = await redeem_all_in_one(body.player_id, code, country, ssp, cookies)
        await _report(account_id, result)
        await sync_to_async(_record_redeem_result)(code, result, viewer, body.player_id, "vault",
                                                   holds_lock=True)
    finally:
        # Releases the lock unless the code was marked used above.
        await sync_to_async(cs.end_redeem)(sc, previous)
    await sync_to_async(sc.refresh_from_db)()
    return {
        "success": bool(result.get("success")),
        "status": result.get("status"),
        "message": result.get("message"),
        "username": result.get("username", ""),
        "product_name": result.get("product_name", ""),
        "redeemed_to": (await sync_to_async(cs.redeemed_to_for)(viewer, code)
                        if result.get("status") in ("used", "redeemed") else None),
        "code": await sync_to_async(cs.code_to_dict)(sc, viewer),
    }


def _record_redeem_result(code: str, result: dict, actor, player_id: str, source: str,
                          holds_lock: bool = False) -> None:
    """Shared by every path that runs redeem_all_in_one (vault, /redeem-now, bulk)."""
    from codes import services as cs

    status = result.get("status")
    if result.get("success") and status == "redeemed":
        order = ((result.get("raw") or {}).get("result") or {}).get("order_no", "")
        cs.record_redemption(code, actor=actor, source=source, player_id=player_id,
                             username=result.get("username", ""),
                             product_name=result.get("product_name", ""), order_no=order or "")
        return
    cs.record_check(code, cs.definite_status(status, result.get("raw")), actor=actor,
                    source=source, product_name=result.get("product_name", ""),
                    player_id=player_id, include_redeeming=holds_lock)
    if status == "failed":
        cs.record_redeem_failure(code, actor=actor, source=source, player_id=player_id,
                                 message=result.get("message", ""))
