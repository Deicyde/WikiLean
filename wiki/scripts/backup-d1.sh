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
# Schedule daily via launchd / cron, e.g. add to your crontab (keep the key
# file in sync with the repo variable when keys rotate; PATH must include the
# directories holding age and node — Homebrew uses /opt/homebrew/bin on Apple
# silicon and /usr/local/bin on Intel):
#   30 7 * * *  cd /path/to/WikiLean/wiki && PATH=/opt/homebrew/bin:/usr/local/bin:$PATH BACKUP_AGE_RECIPIENTS="$(cat ~/.config/wikilean/backup-recipients.txt)" ./scripts/backup-d1.sh
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
WORK=$(mktemp -d "${TMPDIR:-/tmp}/wikilean-backup.XXXXXX")
trap 'rm -rf "$WORK"; rm -f "$OUT.partial"' EXIT
printf '%s\n' "$RECIPIENTS" > "$WORK/recipients.txt"
# Like the workflow, any output from age fails the check: age only warns about
# SSH key types it can't use and skips them, which would drop a spare key. The
# output goes to a file in $WORK (not a command substitution), so a signal
# during the check still runs the EXIT trap.
if ! age -R "$WORK/recipients.txt" -o /dev/null < /dev/null > "$WORK/check.out" 2>&1; then
  cat "$WORK/check.out" >&2
  echo "BACKUP_AGE_RECIPIENTS is not a valid age recipient list; refusing to export (see docs/BACKUP.md)" >&2
  exit 1
fi
if [ -s "$WORK/check.out" ]; then
  cat "$WORK/check.out" >&2
  echo "age printed output for BACKUP_AGE_RECIPIENTS (for example a line it would skip, such as an unsupported SSH key type); refusing to export (see docs/BACKUP.md)" >&2
  exit 1
fi

mkdir -p "$DIR"
npx wrangler d1 export wikilean --remote --output="$WORK/wikilean-$STAMP.sql"
test -s "$WORK/wikilean-$STAMP.sql" || { echo "wrangler produced an empty export" >&2; exit 1; }
# Encrypt next to the destination and rename, so an interrupted run never
# leaves a truncated .age that looks like a real backup.
age -R "$WORK/recipients.txt" -o "$OUT.partial" "$WORK/wikilean-$STAMP.sql"
rm -f "$WORK/wikilean-$STAMP.sql"
mv "$OUT.partial" "$OUT"
echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"
