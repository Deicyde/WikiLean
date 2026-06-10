#!/usr/bin/env bash
# Dump the live WikiLean D1 to a timestamped .sql file.
#
#   ./scripts/backup-d1.sh                # → ./backups/wikilean-<ts>.sql
#   ./scripts/backup-d1.sh /path/to/dir   # → /path/to/dir/wikilean-<ts>.sql
#
# Restore (into a fresh empty D1 of the same name):
#   npx wrangler d1 execute wikilean --remote --file=./backups/wikilean-<ts>.sql
#
# Schedule daily via launchd / cron, e.g. add to your crontab:
#   30 7 * * *  cd /Users/jack/Desktop/LEAN/WikiLean/wiki && ./scripts/backup-d1.sh
set -euo pipefail

DIR="${1:-./backups}"
mkdir -p "$DIR"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$DIR/wikilean-$STAMP.sql"

npx wrangler d1 export wikilean --remote --output="$OUT"
echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"
