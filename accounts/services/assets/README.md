# Vendored JavaScript assets

These are **verbatim copies** of third-party Midasbuy scripts that the redeem
flow needs at runtime. They live inside the project so the FastAPI/Django app is
self-contained and never depends on the inspection-only `*.js` files kept in the
repository root.

| File | Used by | Purpose |
|------|---------|---------|
| `kEc9hjFh5DQJbz_…AesIRU~.js` | `playwright_crypto.py` (`_CHAOS_VM_LOCAL_PATH`) | The Tencent "Chaos VM" that defines `window.xMidas`. Served via `page.route` to avoid CDN latency and to lock `window.xMidas`. |
| `midas-oversea-h5page.js` | `playwright_crypto.py` (`_MIDAS_SDK_LOCAL_PATH`) | Midasbuy oversea H5 SDK, read when the redeem commit needs the `window.midas` SDK source. |

If a newer version is captured, replace the file here (keep the same filename) —
do not point the code back at the root copies.
