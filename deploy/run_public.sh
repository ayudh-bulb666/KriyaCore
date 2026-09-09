#!/bin/bash
# Run KriyaCore the way it has to be run when it's reachable from the
# internet — for testing on kriyacore.in through a Cloudflare tunnel,
# before there's a VPS.
#
#   ./deploy/run_public.sh
#
# This is NOT the dev server. `python3 run.py` starts Flask with debug on,
# which serves Werkzeug's interactive debugger — a Python console on a web
# page. Exposing that to the internet hands anyone who finds a stack trace
# remote code execution on this machine. Hence gunicorn and FLASK_ENV.

set -euo pipefail
cd "$(dirname "$0")/.."

# A secret that survives restarts. Generated once and kept out of git, so
# logins don't get invalidated every time the server restarts, and so the
# repo's placeholder key is never what signs a real session.
KEYFILE=".secret_key.local"
if [ ! -f "$KEYFILE" ]; then
    python3 -c "import secrets; print(secrets.token_hex(32))" > "$KEYFILE"
    chmod 600 "$KEYFILE"
    echo "→ Generated $KEYFILE (git-ignored, keep it)"
fi

export SECRET_KEY="$(cat "$KEYFILE")"
export FLASK_ENV=production      # debug off, secure cookies on
export FLASK_APP=run.py

# One worker on purpose. Flask-Limiter stores rate-limit counters in memory,
# so each extra worker gets its own counter and the login limit silently
# multiplies. One worker is plenty for testing and keeps the limit honest.
# `python3 -m gunicorn` rather than the bare `gunicorn` binary: pip installs
# console scripts into a user bin directory that often isn't on PATH, and the
# module form works regardless.
exec python3 -m gunicorn \
    --workers 1 \
    --bind 127.0.0.1:8000 \
    --access-logfile - \
    --error-logfile - \
    --forwarded-allow-ips='127.0.0.1' \
    run:app
