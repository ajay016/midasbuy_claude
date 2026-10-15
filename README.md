# midas_b

## Celery command

    docker compose exec worker bash
    xvfb-run -a celery -A midasbuy_project worker -l info --concurrency=2

    ## without bash
    docker compose exec worker xvfb-run -a celery -A midasbuy_project worker -l info --concurrency=2

## Watching the login browser (Docker)

The panel's **Login / Refresh** button runs the login browser in the **web**
container (not the worker), on an invisible virtual display by default. To see
it on Windows:

1. Start **XLaunch** (VcXsrv): Multiple windows -> Display number `0` ->
   Start no client -> tick **Disable access control** -> Finish.
2. In `.env` set `LOGIN_DISPLAY=host.docker.internal:0.0`
3. `docker compose up -d` (recreates web/worker with the new display)

Clear `LOGIN_DISPLAY` again to go back to invisible logins. No need to start an
extra Celery worker for this — `docker compose up` already runs one.

## Celery beat (scheduled bot-account re-login)

`beat` fires the periodic schedule; the `worker` runs the tasks. In Docker it runs
as its own `beat` service. To run it locally:

    celery -A midasbuy_project beat -l info

Every `MIDASBUY_RELOGIN_INTERVAL_HOURS` (default 12) each logged-in account is
re-logged-in one at a time, so the others keep serving while one refreshes.
