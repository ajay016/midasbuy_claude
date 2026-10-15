"""
Code vault + redemption records — all business rules live here (sync Django ORM;
call via sync_to_async from the async API). See docs/CODE_VAULT.md.
"""
import hashlib
import hmac

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone

from .models import CodeEvent, CodeStatus, EventKind, StoredCode

MAX_CODES_PER_REQUEST = 1000
DEFINITE_STATUSES = {CodeStatus.VALID, CodeStatus.USED, CodeStatus.EXPIRED, CodeStatus.INVALID}
_ERR_TO_STATUS = {
    "REDEEM_CODE_ALREADY_USED": CodeStatus.USED,
    "REDEEM_CODE_EXPIRED": CodeStatus.EXPIRED,
    "INVALID_REDEEM_CODE": CodeStatus.INVALID,
}


# ── Code identity ──────────────────────────────────────────────────────────────
def normalize_code(code) -> str:
    """Whitespace removed, case kept (Midasbuy codes are case-sensitive)."""
    return "".join(str(code or "").split())


def fingerprint(code) -> str:
    key = settings.CODE_FINGERPRINT_KEY.encode("utf-8")
    return hmac.new(key, normalize_code(code).encode("utf-8"), hashlib.sha256).hexdigest()


def last4(code) -> str:
    return normalize_code(code)[-4:]


def mask(code_last4: str) -> str:
    return "••••" + code_last4


def definite_status(status: str | None, raw: dict | None) -> str | None:
    """valid / used / expired / invalid when Midasbuy gave a DEFINITE answer about
    the code; None for anything else (timeouts, captcha, unknown player...), so a
    transient error never marks a stored code invalid."""
    if status == CodeStatus.VALID:
        return CodeStatus.VALID
    err = str((raw or {}).get("err_code") or "")
    return _ERR_TO_STATUS.get(err)


# ── Scope / permissions ────────────────────────────────────────────────────────
def owner_scope_q(viewer, prefix: str = "owner") -> Q:
    """Owners whose data ``viewer`` may see: admins -> everyone; partners -> self +
    own clients; everyone else -> self."""
    if viewer.is_admin:
        return Q()
    q = Q(**{prefix: viewer})
    if viewer.is_partner:
        q |= Q(**{f"{prefix}__partner": viewer})
    return q


def can_redeem(user, code_fingerprint: str) -> bool:
    """Policy hook: may ``user`` redeem this code? Currently anyone allowed to order
    may redeem any code — stored or not, theirs or not. Tighten here (one place)
    if codes stored in someone else's vault should become off-limits."""
    return True


def _event_visible_q(viewer) -> Q:
    """History rows a viewer may see: their own (or their clients') actions, plus
    anything on a stored copy that belongs to them (or their clients)."""
    if viewer.is_admin:
        return Q()
    return owner_scope_q(viewer, "actor") | owner_scope_q(viewer, "stored_code__owner")


# ── Serialization ──────────────────────────────────────────────────────────────
def _owner_dict(user) -> dict | None:
    if user is None:
        return None
    return {"name": user.name, "client_ref": user.client_ref}


def code_to_dict(sc: StoredCode, viewer, include_code: bool = False) -> dict:
    d = {
        "id": sc.id,
        "code": sc.get_code() if include_code else mask(sc.code_last4),
        "code_last4": sc.code_last4,
        "label": sc.label,
        "country_code": sc.country_code,
        "status": sc.status,
        "product_name": sc.product_name,
        "checked_at": sc.checked_at,
        "redeemed_to": None,
        "created_at": sc.created_at,
    }
    if sc.redeemed_player_id:
        d["redeemed_to"] = {
            "player_id": sc.redeemed_player_id,
            "username": sc.redeemed_username,
            "redeemed_at": sc.redeemed_at,
        }
    if viewer.is_admin or viewer.is_partner:
        d["owner"] = _owner_dict(sc.owner)
    return d


def event_to_dict(e: CodeEvent) -> dict:
    return {
        "at": e.created_at,
        "kind": e.kind,
        "source": e.source,
        "by": _owner_dict(e.actor),
        "player_id": e.player_id,
        "username": e.username,
        "product_name": e.product_name,
        "order_no": e.order_no,
        "status": e.result_status,
        "message": e.message,
    }


# ── Vault operations ───────────────────────────────────────────────────────────
def add_codes(owner, codes, *, actor=None, label: str = "", country_code: str = "bd",
              source: str = "") -> dict:
    from apiauth.security import encrypt_secret

    actor = actor or owner
    seen, added, duplicates, empty = set(), [], [], 0
    for raw in codes:
        code = normalize_code(raw)
        if not code:
            empty += 1
            continue
        fp = fingerprint(code)
        if fp in seen:
            duplicates.append(mask(last4(code)))
            continue
        seen.add(fp)
        try:
            with transaction.atomic():
                sc = StoredCode.objects.create(
                    owner=owner, code_encrypted=encrypt_secret(code), fingerprint=fp,
                    code_last4=last4(code), label=label[:120],
                    country_code=(country_code or "bd").lower()[:8],
                )
        except IntegrityError:
            duplicates.append(mask(last4(code)))
            continue
        # A code our app already redeemed starts out as "used" with its player.
        prior = (CodeEvent.objects.filter(fingerprint=fp, kind=EventKind.REDEEMED)
                 .order_by("-created_at").first())
        if prior:
            sc.status = CodeStatus.USED
            sc.product_name = prior.product_name
            sc.redeemed_player_id, sc.redeemed_username = prior.player_id, prior.username
            sc.redeemed_at, sc.redeemed_by_id = prior.created_at, prior.actor_id
            sc.save()
        CodeEvent.objects.create(fingerprint=fp, code_last4=sc.code_last4, stored_code=sc,
                                 kind=EventKind.ADDED, actor=actor, source=source)
        added.append(sc)
    return {"added": added, "duplicates": duplicates, "empty": empty}


def list_codes(viewer, *, status: str = "", search: str = "", client_ref: str = "",
               page: int = 1, page_size: int = 50) -> dict:
    qs = StoredCode.objects.filter(owner_scope_q(viewer)).select_related("owner")
    if status == "unused":
        qs = qs.filter(status__in=[CodeStatus.UNCHECKED, CodeStatus.VALID])
    elif status:
        qs = qs.filter(status=status)
    if search:
        search = search.strip()
        qs = qs.filter(Q(code_last4__icontains=search[-4:]) | Q(label__icontains=search)
                       | Q(redeemed_player_id__icontains=search))
    if client_ref:
        qs = qs.filter(owner__client_ref=client_ref)
    total = qs.count()
    page, page_size = max(page, 1), min(max(page_size, 1), 200)
    rows = list(qs.order_by("-created_at")[(page - 1) * page_size: page * page_size])
    counts = dict(StoredCode.objects.filter(owner_scope_q(viewer))
                  .values("status").annotate(n=Count("id")).values_list("status", "n"))
    return {"items": [code_to_dict(sc, viewer) for sc in rows], "total": total,
            "page": page, "page_size": page_size, "counts": counts}


def get_code(viewer, code_id: int) -> StoredCode | None:
    return (StoredCode.objects.filter(owner_scope_q(viewer), pk=code_id)
            .select_related("owner").first())


def history(viewer, fp: str, limit: int = 100) -> list[dict]:
    qs = (CodeEvent.objects.filter(_event_visible_q(viewer), fingerprint=fp)
          .select_related("actor").order_by("-created_at", "-id")[:limit])
    return [event_to_dict(e) for e in qs]


def code_detail(viewer, sc: StoredCode) -> dict:
    return {**code_to_dict(sc, viewer, include_code=True),
            "history": history(viewer, sc.fingerprint)}


def delete_code(viewer, sc: StoredCode) -> None:
    CodeEvent.objects.create(fingerprint=sc.fingerprint, code_last4=sc.code_last4,
                             stored_code=None, kind=EventKind.DELETED, actor=viewer,
                             source="vault")
    sc.delete()


def lookup(viewer, code) -> dict:
    """Everything the viewer may know about any code (stored or not)."""
    fp = fingerprint(code)
    stored = list(StoredCode.objects.filter(owner_scope_q(viewer), fingerprint=fp)
                  .select_related("owner"))
    return {
        "code": mask(last4(code)),
        "stored": [code_to_dict(sc, viewer) for sc in stored],
        "redeemed_to": redeemed_to_for(viewer, code),
        "history": history(viewer, fp),
    }


# ── Recording (called from every check / redeem path) ──────────────────────────
def record_check(code, status: str | None, *, actor=None, source: str = "",
                 product_name: str = "", player_id: str = "",
                 include_redeeming: bool = False) -> None:
    """Apply a DEFINITE check result to every stored copy of the code. Copies locked
    for redeeming are skipped unless the caller holds that lock (include_redeeming)."""
    if status not in DEFINITE_STATUSES:
        return
    fp = fingerprint(code)
    rows = StoredCode.objects.filter(fingerprint=fp)
    if not include_redeeming:
        rows = rows.exclude(status=CodeStatus.REDEEMING)
    if not rows.exists():
        return
    update = {"status": status, "checked_at": timezone.now(), "updated_at": timezone.now()}
    if product_name:
        update["product_name"] = product_name[:255]
    rows.update(**update)
    mine = rows.filter(owner_scope_q(actor)).first() if actor else None
    CodeEvent.objects.create(fingerprint=fp, code_last4=last4(code), stored_code=mine,
                             kind=EventKind.CHECKED, actor=actor, source=source,
                             player_id=player_id, product_name=product_name[:255],
                             result_status=status)


def record_redemption(code, *, actor=None, source: str = "", player_id: str = "",
                      username: str = "", product_name: str = "", order_no: str = "") -> None:
    """A redeem through our app succeeded: remember which player got the code."""
    fp, now = fingerprint(code), timezone.now()
    StoredCode.objects.filter(fingerprint=fp).update(
        status=CodeStatus.USED, product_name=product_name[:255], checked_at=now,
        redeemed_player_id=player_id[:64], redeemed_username=username[:128],
        redeemed_at=now, redeemed_by=actor, updated_at=now,
    )
    mine = (StoredCode.objects.filter(owner_scope_q(actor), fingerprint=fp).first()
            if actor else None)
    CodeEvent.objects.create(fingerprint=fp, code_last4=last4(code), stored_code=mine,
                             kind=EventKind.REDEEMED, actor=actor, source=source,
                             player_id=player_id[:64], username=username[:128],
                             product_name=product_name[:255], order_no=order_no[:128],
                             result_status="redeemed")


def record_redeem_failure(code, *, actor=None, source: str = "", player_id: str = "",
                          message: str = "") -> None:
    """Only stored codes get a failure entry (it's part of their history)."""
    fp = fingerprint(code)
    mine = (StoredCode.objects.filter(owner_scope_q(actor), fingerprint=fp).first()
            if actor else None)
    if mine is None:
        return
    CodeEvent.objects.create(fingerprint=fp, code_last4=last4(code), stored_code=mine,
                             kind=EventKind.REDEEM_FAILED, actor=actor, source=source,
                             player_id=player_id[:64], message=message[:1000],
                             result_status="failed")


def redeemed_to_for(viewer, code) -> dict | None:
    """Which player our app redeemed this code to — only if ``viewer`` may know:
    admins; whoever redeemed it (or their partner); anyone holding the code in
    their vault (or their partner)."""
    if viewer is None:
        return None
    fp = fingerprint(code)
    ev = (CodeEvent.objects.filter(fingerprint=fp, kind=EventKind.REDEEMED)
          .select_related("actor").order_by("-created_at").first())
    if ev is None:
        return None
    allowed = (
        viewer.is_admin
        or (ev.actor_id is not None and (ev.actor_id == viewer.id
                                         or ev.actor.partner_id == viewer.id))
        or StoredCode.objects.filter(owner_scope_q(viewer), fingerprint=fp).exists()
    )
    if not allowed:
        return None
    return {"player_id": ev.player_id, "username": ev.username,
            "product_name": ev.product_name, "redeemed_at": ev.created_at}


# ── Redeem lock for stored codes ───────────────────────────────────────────────
def begin_redeem(sc: StoredCode) -> bool:
    """Atomically mark a stored code as being redeemed. False if it's already used
    or another request is redeeming it right now."""
    n = (StoredCode.objects.filter(pk=sc.pk)
         .exclude(status__in=[CodeStatus.REDEEMING, CodeStatus.USED])
         .update(status=CodeStatus.REDEEMING, updated_at=timezone.now()))
    return n == 1


def end_redeem(sc: StoredCode, previous_status: str) -> None:
    """Release the lock if the redeem didn't mark the code used (failure path)."""
    StoredCode.objects.filter(pk=sc.pk, status=CodeStatus.REDEEMING).update(
        status=previous_status, updated_at=timezone.now())
