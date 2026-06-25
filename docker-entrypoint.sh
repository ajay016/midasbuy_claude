#!/bin/sh
# Start a virtual X display so the headful Chromium used for account login can
# launch inside the container, then hand off to the real command (uvicorn /
# celery). Headless browser paths ignore DISPLAY, so this is harmless when a
# given call doesn't need it.
#
# This replaces wrapping the server in `xvfb-run`, which could hang before the
# app started (no logs at all). Here Xvfb runs in the background and the real
# process is exec'd as PID 1, so its stdout/stderr stream normally.
set -e

if [ -z "$DISPLAY" ]; then
    rm -f /tmp/.X99-lock 2>/dev/null || true
    Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
    export DISPLAY=:99
    # Give Xvfb a moment to create the socket before the browser needs it.
    sleep 1
fi

exec "$@"
