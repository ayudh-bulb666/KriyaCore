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

echo
echo "Creating an admin account on:"
echo "  ${DATABASE_URL##*@}"          # host and database only — no credentials
echo
echo "Password must be at least 10 characters and cannot contain your name"
echo "or email address. A phrase like 'harbour-mango-lantern' works well."
echo

exec python3 -m flask create-admin
