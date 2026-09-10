#!/bin/bash
# Create the platform admin account on the live database.
#
#   ./deploy/create_admin.sh
#
# Render's free plan has no shell, so one-off commands run from here instead.
# The account lands in the same Supabase database the deployed app uses —
# nothing about it is specific to the machine that created it.

set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    echo "No .env file. Run: python3 deploy/set_database_url.py"
    exit 1
fi

set -a; source .env; set +a

if [ -z "${DATABASE_URL:-}" ] || [[ "$DATABASE_URL" == *localhost* ]]; then
    echo "DATABASE_URL is missing or still the template value."
    echo "Run: python3 deploy/set_database_url.py"
    exit 1
fi

# .env sets FLASK_ENV=production, which makes the app demand a real
# SECRET_KEY at startup. That guard exists to stop a placeholder key ever
# signing live session cookies — but this command only writes one database
# row and never touches a session, so we drop the production flag and let it
# use the local dev key instead of inventing a fake one.
unset FLASK_ENV
export FLASK_APP=run.py

# Quieten three warnings that always appear and never matter here:
#   - Flask offering to load .env (we already sourced it in bash)
#   - urllib3 noting macOS ships LibreSSL, not OpenSSL (psycopg2 doesn't use it)
#   - Flask-Limiter noting in-memory rate limits (correct for one worker)
# Scoped to this wrapper only — the app still shows its warnings normally.
# Six lines of noise around a password prompt makes a real error easy to miss.
export PYTHONWARNINGS=ignore

# Resolve the database host once, then pin the address for the actual
# command. Two reasons:
#
#   1. It proves the database is reachable before the user types a password.
#      Without it, a DNS hiccup produces sixty lines of SQLAlchemy traceback
#      *after* they have entered everything.
#   2. It takes DNS out of the connect path. The Supabase pooler sits behind
#      a load balancer with rotating addresses, and this machine's resolver
#      drops a lookup now and then — twice during setup, both times mid-
#      command. libpq's `hostaddr` lets us supply the IP while keeping the
#      hostname for TLS SNI and certificate verification, so the connection
#      is still fully verified.
PINNED=$(python3 - <<'PREFLIGHT'
import os, socket, sys, time
from urllib.parse import urlsplit
url = os.environ['DATABASE_URL']
host = urlsplit(url).hostname

ip = None
for attempt in range(1, 6):
    try:
        ip = socket.getaddrinfo(host, 5432, socket.AF_INET,
                                proto=socket.IPPROTO_TCP)[0][4][0]
        break
    except socket.gaierror:
        time.sleep(2)

if ip is None:
    print(f"ERROR|Cannot resolve {host} after 5 tries. Check your connection.")
    sys.exit(1)

pinned = url + ('&' if '?' in url else '?') + f'hostaddr={ip}'

try:
    import psycopg2
    psycopg2.connect(pinned, connect_timeout=20).close()
except Exception as exc:
    first = str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)
    hint = ''
    if 'password authentication' in first:
        hint = ' Re-run: python3 deploy/set_database_url.py'
    print(f"ERROR|Could not connect: {first}{hint}")
    sys.exit(1)

print(pinned)
PREFLIGHT
) || true

if [ -z "$PINNED" ] || [[ "$PINNED" == ERROR\|* ]]; then
    echo
    echo "  ${PINNED#ERROR|}"
    echo
    exit 1
fi
export DATABASE_URL="$PINNED"

echo
echo "Creating an admin account on:"
echo "  ${DATABASE_URL##*@}"          # host and database only — no credentials
echo
echo "Password must be at least 10 characters and cannot contain your name"
echo "or email address. A phrase like 'harbour-mango-lantern' works well."
echo

exec python3 -m flask create-admin 2> >(grep -v 'python-dotenv' >&2)
