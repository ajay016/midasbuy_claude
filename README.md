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
