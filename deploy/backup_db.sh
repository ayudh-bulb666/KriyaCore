#!/bin/bash
# Nightly Postgres backup for KriyaCore — no managed backups on a self-hosted
# VPS, so this replaces what Railway's Postgres add-on gives you for free.
#
# Install:
#   sudo cp deploy/backup_db.sh /home/deploy/backup_db.sh
#   sudo chmod +x /home/deploy/backup_db.sh
#   crontab -e   # as the 'deploy' user
#   # add this line to run nightly at 2:30 AM IST:
#   30 2 * * * /home/deploy/backup_db.sh >> /home/deploy/backup.log 2>&1

set -euo pipefail

DB_NAME="kriyacore"
DB_USER="kriyacore"
BACKUP_DIR="/home/deploy/backups"
KEEP_DAYS=14

mkdir -p "$BACKUP_DIR"

STAMP=$(date +%Y-%m-%d_%H%M)
FILE="$BACKUP_DIR/kriyacore_$STAMP.sql.gz"

pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$FILE"
echo "$(date): backed up to $FILE"

# Delete backups older than KEEP_DAYS
find "$BACKUP_DIR" -name "kriyacore_*.sql.gz" -mtime +"$KEEP_DAYS" -delete

# Optional but recommended: also copy $FILE off-box (e.g. rclone to
# Backblaze B2 / a free-tier S3 bucket) so a disk failure doesn't take
# your only backup copy with it. Left out here since it needs your own
# cloud storage credentials — ask me to wire this up once you've picked one.
