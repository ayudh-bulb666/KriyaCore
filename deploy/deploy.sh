#!/bin/bash
# Run this ON THE VPS (not locally) to ship a new version after you've
# pushed to GitHub. Pulls latest code, installs any new dependencies,
# updates the DB schema, and restarts the app with zero manual steps.
#
# Usage (as the 'deploy' user, from anywhere):
#   /home/deploy/kriyacore/deploy/deploy.sh

set -euo pipefail

cd /home/deploy/kriyacore

echo "→ Pulling latest code..."
git pull origin main

echo "→ Installing dependencies..."
source venv/bin/activate
pip install -q -r requirements.txt

echo "→ Backing up the database first..."
# Migrations are the one deploy step that isn't simply re-runnable if it goes
# wrong, so take the snapshot before touching the schema, not after.
./deploy/backup_db.sh

echo "→ Applying database migrations..."
# Alembic, via Flask-Migrate. Applies only the migrations this database
# hasn't seen yet and records each one, so re-running is a no-op.
#
# This replaces the old db.create_all() call, whose comment above was
# wrong: create_all() adds new TABLES but never new COLUMNS. A model that
# gained a field would deploy "successfully" and then 500 on the first
# query that touched it.
export FLASK_APP=run.py
flask db upgrade

echo "→ Restarting service..."
sudo systemctl restart kriyacore

echo "→ Done. Tailing logs (Ctrl+C to stop watching, app keeps running):"
sudo journalctl -u kriyacore -f --since "10 seconds ago"
