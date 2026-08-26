#!/bin/bash
# Run this ON THE VPS (not locally) to ship a new version after you've
# pushed to GitHub. Pulls latest code, installs any new dependencies,
# updates the DB schema, and restarts the app with zero manual steps.
#
# Usage (as the 'deploy' user, from anywhere):
#   /home/deploy/gympro/deploy/deploy.sh

set -euo pipefail

cd /home/deploy/gympro

echo "→ Pulling latest code..."
git pull origin main

echo "→ Installing dependencies..."
source venv/bin/activate
pip install -q -r requirements.txt

echo "→ Applying any new DB tables/columns..."
# db.create_all() only adds NEW tables/columns, it does not alter existing
# ones — if a migration ever needs to change/rename an existing column,
# that needs a manual ALTER TABLE first. Fine for purely additive changes.
python -c "from app import create_app; create_app()"

echo "→ Restarting service..."
sudo systemctl restart gympro

echo "→ Done. Tailing logs (Ctrl+C to stop watching, app keeps running):"
sudo journalctl -u gympro -f --since "10 seconds ago"
