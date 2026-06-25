# midas_b

## Celery command

    docker compose exec worker bash
    xvfb-run -a celery -A midasbuy_project worker -l info --concurrency=2

    ## without bash
    docker compose exec worker xvfb-run -a celery -A midasbuy_project worker -l info --concurrency=2

## for viewing the browser

    docker compose exec worker bash
    export DISPLAY=host.docker.internal:0.0
    celery -A midasbuy_project worker -l info --concurrency=5

## Celery beat (scheduled bot-account re-login)

`beat` fires the periodic schedule; the `worker` runs the tasks. In Docker it runs
as its own `beat` service. To run it locally:

    celery -A midasbuy_project beat -l info

Every `MIDASBUY_RELOGIN_INTERVAL_HOURS` (default 12) each logged-in account is
re-logged-in one at a time, so the others keep serving while one refreshes.
