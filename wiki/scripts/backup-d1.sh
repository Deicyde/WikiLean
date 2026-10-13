#!/usr/bin/env bash
# Dump the live WikiLean D1 to a timestamped file encrypted with age, the same
# way .github/workflows/backup-d1.yml does. Only the .age file is kept; the
# plaintext lives in a private temp dir and is deleted as soon as it is
# encrypted. Needs age (`brew install age`) and the backup public key(s):
#
#   export BACKUP_AGE_RECIPIENTS="$(gh variable get BACKUP_AGE_RECIPIENTS --repo Deicyde/WikiLean)"
#   ./scripts/backup-d1.sh                # → ./backups/wikilean-<ts>.sql.age
#   ./scripts/backup-d1.sh /path/to/dir   # → /path/to/dir/wikilean-<ts>.sql.age
#
# Decrypt and restore (local D1 first): see docs/BACKUP.md.
#
# Schedule daily via launchd / cron, e.g. add to your crontab:
#   30 7 * * *  cd /Users/jack/Desktop/LEAN/WikiLean/wiki && BACKUP_AGE_RECIPIENTS='age1…' ./scripts/backup-d1.sh
set -euo pipefail

DIR="${1:-./backups}"
RECIPIENTS="${BACKUP_AGE_RECIPIENTS:-}"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$DIR/wikilean-$STAMP.sql.age"

# Fail before exporting if age or the recipients are missing or invalid.
command -v age >/dev/null || { echo "age not found (brew install age)" >&2; exit 1; }
if [ -z "${RECIPIENTS//[[:space:]]/}" ]; then
  echo "BACKUP_AGE_RECIPIENTS is empty; refusing to write an unencrypted backup (see docs/BACKUP.md)" >&2
  exit 1
fi
umask 077
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
printf '%s\n' "$RECIPIENTS" > "$WORK/recipients.txt"
age -R "$WORK/recipients.txt" -o /dev/null < /dev/null

mkdir -p "$DIR"
npx wrangler d1 export wikilean --remote --output="$WORK/wikilean-$STAMP.sql"
age -R "$WORK/recipients.txt" -o "$OUT" "$WORK/wikilean-$STAMP.sql"
rm -f "$WORK/wikilean-$STAMP.sql"
echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"
