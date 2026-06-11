"""
Debug capture for the redeem COMMIT step.

Opens a HEADFUL Midasbuy redeem page using a logged-in account's session, with
full network capture (every request + response body for non-asset traffic), and
keeps the browser open so you can perform the real redemption by hand while we
record exactly which request commits the code.

Usage:
    python manage.py capture_redeem --account-id 1 --country bd --minutes 30

Then in the opened browser: look up the player, enter the VALID code, click OK,
solve the captcha if prompted, and confirm the final popup. Everything is written
to  midasbuy_sessions/account_<id>/redeem_commit_capture.txt  — send that file.

NOTE: a valid code is consumed by a real redemption. This captures HOW it happens
so the commit can be automated afterwards.
"""
import json
import os
import time

from django.conf import settings
from django.core.management.base import BaseCommand


# Requests whose bodies we always capture in full (the interesting ones).
_FULL_BODY_HINTS = (
    "/interface/", "shelfproto", "redeem", "order", "secondary", "buy",
    "shelves_svr", "harvestsharp", "commoncheck", "pavalidate", "trade",
    "provide", "confirm", "/pay", "deliver", "commit",
)
# Resource types whose bodies we skip (too big / not useful).
_SKIP_BODY_TYPES = {"image", "font", "media", "stylesheet"}

# Appended to the served Chaos VM script (runs in the MAIN JS world, where
# window.xMidas actually lives). Patchright runs add_init_script in an ISOLATED
# world, so a setter hook registered there never sees the page's own
# Object.defineProperty assignment of window.xMidas — hence we must wrap it here,
# inside the same script that the page trusts. We poll until the VM has assigned
# window.xMidas, wrap it with a recorder that pushes each call's plaintext input
# (arguments[0]) into sessionStorage['__xmidasCalls'] (survives the confirm
# navigation to /result/pubgm), then lock it non-configurable.
_CHAOS_VM_RECORD = b"""
;(function(){
    var KEY = '__xmidasCalls';
    function record(arg){
        try {
            var arr = JSON.parse(sessionStorage.getItem(KEY) || '[]');
            var v; try { v = JSON.parse(JSON.stringify(arg)); } catch(e){ v = String(arg); }
            arr.push(v);
            sessionStorage.setItem(KEY, JSON.stringify(arr));
        } catch(e) {}
    }
    function wrap(fn){
        var w = function(){ record(arguments[0]); return fn.apply(this, arguments); };
        try { w.__wrapped = true; } catch(e) {}
        return w;
    }
    function lock(fn){
        try {
            Object.defineProperty(window, 'xMidas', {
                get: function(){ return fn; },
                set: function(){},
                configurable: false,
                enumerable: true,
            });
        } catch(e) {}
    }
    var _cur = window.xMidas;
    if (typeof _cur === 'function') { lock(wrap(_cur)); return; }
    var _t = setInterval(function(){
        var f = window.xMidas;
        if (typeof f === 'function') { clearInterval(_t); lock(wrap(f)); }
    }, 10);
    setTimeout(function(){ clearInterval(_t); }, 30000);
})();
"""


class Command(BaseCommand):
    help = "Open a headful Midasbuy redeem page with full network capture to debug the redemption commit."

    def add_arguments(self, parser):
        parser.add_argument("--account-id", type=int, default=1)
        parser.add_argument("--country", default="bd")
        parser.add_argument("--minutes", type=int, default=30)
        parser.add_argument(
            "--block-commit", action="store_true",
            help="Abort the /result/pubgm request so the code is NOT consumed, while still "
                 "capturing the xMidas plaintext built just before it.",
        )

    def handle(self, *args, **opts):
        from accounts.models import MidasbuyAccount
        from accounts.services.playwright_crypto import (
            _CHAOS_VM_LOCAL_PATH,
            _get_playwright,
            _launch_context,
            _wait_for_xmidas,
        )

        acct = MidasbuyAccount.objects.get(pk=opts["account_id"])
        ssp = acct.storage_state_path or os.path.join(
            acct.get_session_dir(str(settings.BASE_DIR)), "storage_state.json"
        )
        if not ssp or not os.path.exists(ssp):
            self.stderr.write(f"storage_state not found for account {opts['account_id']}: {ssp}")
            return

        country = opts["country"]
        out_dir = os.path.dirname(ssp)
        cap_path = os.path.join(out_dir, "redeem_commit_capture.txt")
        xmidas_path = os.path.join(out_dir, "xmidas_inputs.txt")
        # Same URL the working crypto flow uses, so window.xMidas initialises.
        redeem_url = f"https://www.midasbuy.com/midasbuy/{country}/redeem/pubgm?from=self.midasbuy_saas"

        with open(cap_path, "w", encoding="utf-8") as f:
            f.write(f"# redeem commit capture  account={opts['account_id']}  {time.ctime()}\n")
        # Fresh dedicated file for the xMidas plaintext inputs (one JSON per line).
        with open(xmidas_path, "w", encoding="utf-8") as f:
            f.write("")

        def dump(line: str):
            try:
                with open(cap_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass

        sync_playwright, _ = _get_playwright()

        self.stdout.write(self.style.WARNING(
            "Opening headful browser. Do the FULL redemption by hand (look up player, "
            "enter the VALID code, OK, solve captcha, confirm). Capturing to:\n  " + cap_path
        ))

        with sync_playwright() as p:
            # Reuse the working flow's launch (stealth + storage_state + SERVER_DATA
            # snapshot) but headful, so the real page's player lookup / encryption works.
            browser, context = _launch_context(p, ssp, country, headless=False)
            page = context.new_page()

            # Serve the Chaos VM script with a RECORDING wrapper appended so we
            # capture the plaintext window.xMidas encrypts. This must run in the
            # MAIN world (patchright init scripts run isolated and never see the
            # page's own Object.defineProperty assignment of window.xMidas), so we
            # append it to the trusted VM script itself — mirroring the working
            # flow's _setup_chaos_vm_protection, but recording instead of just
            # locking.
            def _serve_recording_vm(route):
                try:
                    with open(_CHAOS_VM_LOCAL_PATH, "rb") as vf:
                        original = vf.read()
                    route.fulfill(
                        status=200,
                        headers={
                            "content-type": "application/javascript; charset=utf-8",
                            "cache-control": "no-cache",
                        },
                        body=original + _CHAOS_VM_RECORD,
                    )
                    self.stdout.write("  chaos VM served with xMidas recorder")
                    return
                except Exception as e:
                    self.stdout.write(self.style.WARNING(f"  local chaos VM unavailable ({e}) — fetching from CDN"))
                try:
                    response = route.fetch()
                    hdrs = dict(response.headers)
                    hdrs.pop("content-length", None)
                    route.fulfill(status=response.status, headers=hdrs, body=response.body() + _CHAOS_VM_RECORD)
                except Exception:
                    try:
                        route.continue_()
                    except Exception:
                        pass

            page.route("**cdn.midasbuy.com/js/x-midas/**", _serve_recording_vm)

            def on_request(req):
                try:
                    u = req.url
                    interesting = any(h in u.lower() for h in _FULL_BODY_HINTS)
                    if interesting or req.method != "GET":
                        dump(f"\n>> {req.method} {u}")
                        pd = None
                        try:
                            pd = req.post_data
                        except Exception:
                            pd = None
                        if pd:
                            dump("POST_DATA: " + pd[:20000])
                except Exception:
                    pass

            def on_response(resp):
                try:
                    u = resp.url
                    rtype = resp.request.resource_type
                    interesting = any(h in u.lower() for h in _FULL_BODY_HINTS)
                    if interesting:
                        dump(f"\n<< {resp.status} {u}")
                        if rtype not in _SKIP_BODY_TYPES:
                            body = None
                            try:
                                body = resp.text()
                            except Exception:
                                body = None
                            if body:
                                dump(body[:40000])
                        self.stdout.write(f"  captured {resp.status} {u[:110]}")
                except Exception:
                    pass

            page.on("request", on_request)
            page.on("response", on_response)

            if opts["block_commit"]:
                # Abort the commit so the code is preserved; the xMidas plaintext
                # is built client-side before this request fires, so we still get it.
                def _block(route):
                    try:
                        if "/result/" in route.request.url:
                            self.stdout.write(self.style.WARNING("  BLOCKED commit: " + route.request.url[:110]))
                            route.abort()
                            return
                    except Exception:
                        pass
                    try:
                        route.continue_()
                    except Exception:
                        pass
                page.route("**/result/**", _block)
                self.stdout.write(self.style.WARNING(
                    "--block-commit ON: the redemption will be aborted (code preserved); "
                    "we only capture the xMidas plaintext."
                ))

            page.goto(redeem_url, wait_until="load")
            try:
                _wait_for_xmidas(page, out_dir, 30_000)
            except Exception:
                pass
            self.stdout.write(self.style.SUCCESS(
                f"Browser open at {redeem_url}. Complete the redemption now."
            ))

            deadline = time.time() + opts["minutes"] * 60
            sentinel = os.path.join(out_dir, "stop_capture.txt")
            self.stdout.write(f"Capturing for up to {opts['minutes']} min. "
                              f"Create {sentinel} (or close the window) to stop early.")
            dumped_xmidas = 0
            try:
                while time.time() < deadline:
                    if page.is_closed() or os.path.exists(sentinel):
                        break
                    # Drain newly captured window.xMidas plaintext inputs
                    # (sessionStorage survives the confirm navigation).
                    try:
                        calls = page.evaluate(
                            "() => { try { return JSON.parse(sessionStorage.getItem('__xmidasCalls') || '[]'); } catch(e) { return []; } }"
                        )
                        for c in calls[dumped_xmidas:]:
                            payload = json.dumps(c)
                            dump("\n[XMIDAS-INPUT] " + payload[:8000])
                            # Also write to a dedicated file that is easy to find/push.
                            try:
                                with open(xmidas_path, "a", encoding="utf-8") as xf:
                                    xf.write(payload + "\n")
                            except Exception:
                                pass
                            self.stdout.write(self.style.SUCCESS("  captured xMidas input -> xmidas_inputs.txt"))
                        dumped_xmidas = len(calls)
                    except Exception:
                        pass
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
            finally:
                if os.path.exists(sentinel):
                    try:
                        os.remove(sentinel)
                    except Exception:
                        pass
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

        self.stdout.write(self.style.SUCCESS(
            f"Done. Full capture: {cap_path}\n"
            f"      xMidas plaintext inputs: {xmidas_path}  <-- push this one"
        ))
