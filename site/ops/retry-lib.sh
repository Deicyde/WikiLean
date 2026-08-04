# Shared Max-window retry wrapper for the nightly jobs — sourced (not executed)
# by nightly-moderate.sh / newtags-nightly.sh / brain-nightly.sh, which each
# set $LOG before calling retry_on_ratelimit. Single source of truth: this
# replaced three drifting inline copies (2026-08-04).
#
# Contract: the wrapped command exits 3 BOTH on a consecutive-window-exhaustion
# abort AND on an intentional token-budget stop; retry ONLY when the fresh log
# tail carries the Max rate-limit signature — never on a budget stop.
#
# The Max limit error carries its own reset time ("resets 7:30am
# (America/New_York)"), and a 5-hour window is never back in a blind 900s nap —
# the 07-19/07-22/07-23/08-02 nights each burned 3×15 review articles proving
# it (see docs/ROADMAP.md status log). So: parse the newest "resets <time>"
# from the log tail and sleep until then (+120s buffer); blind RETRY_SLEEP is
# only the fallback when nothing parseable is present.
#
# Tunables (override in nightly.env or the environment):
#   WIKILEAN_RETRY_SLEEP  fallback seconds when no reset time parses  (900)
#   WIKILEAN_RETRY_MAX    total attempts incl. the first              (2)
#   WIKILEAN_RETRY_CAP_S  never sleep longer than this (>cap → stop)  (10800)

RETRY_SLEEP="${WIKILEAN_RETRY_SLEEP:-900}"
RETRY_MAX="${WIKILEAN_RETRY_MAX:-2}"
RETRY_CAP_S="${WIKILEAN_RETRY_CAP_S:-10800}"

# Emit seconds-until-reset (+120s buffer) parsed from the newest
# "resets 7:30am"-style fragment in the log tail; nonzero exit if absent or
# unparseable. BSD date (macOS): -j -f for parse-without-set.
reset_wait_seconds() {
  local m t ampm hh mm now target
  m=$(tail -n 80 "$LOG" 2>/dev/null \
      | grep -ioE "resets [0-9]{1,2}(:[0-9]{2})?(am|pm)" | tail -1) || true
  [ -z "$m" ] && return 1
  t=$(printf '%s' "$m" | tr 'A-Z' 'a-z'); t=${t#resets }
  ampm=${t##*[0-9]}; t=${t%"$ampm"}
  hh=${t%%:*}; mm=0; case "$t" in *:*) mm=${t#*:};; esac
  hh=$((10#$hh)); mm=$((10#$mm))
  [ "$ampm" = "pm" ] && [ "$hh" -ne 12 ] && hh=$((hh + 12))
  [ "$ampm" = "am" ] && [ "$hh" -eq 12 ] && hh=0
  now=$(date +%s)
  target=$(date -j -f "%Y-%m-%d %H:%M" \
           "$(date +%Y-%m-%d) $(printf '%02d:%02d' "$hh" "$mm")" +%s 2>/dev/null) || return 1
  [ "$target" -le "$now" ] && target=$((target + 86400))
  echo $((target - now + 120))
}

retry_on_ratelimit() {
  local n=0 rc wait_s
  while : ; do
    "$@"; rc=$?
    [ "$rc" -ne 3 ] && return "$rc"
    if ! tail -n 80 "$LOG" 2>/dev/null \
        | grep -qiE "hit your limit|usage limit|resets [0-9]|rate_limited_429"; then
      return "$rc"   # exit 3 without the Max signature = intended budget stop
    fi
    n=$((n + 1))
    if [ "$n" -ge "$RETRY_MAX" ]; then
      echo "  (rate-limited: exhausted $n retries across the Max reset — leaving the rest for tomorrow)"
      return "$rc"
    fi
    wait_s=$(reset_wait_seconds) || wait_s="$RETRY_SLEEP"
    if [ "$wait_s" -gt "$RETRY_CAP_S" ]; then
      echo "  (window resets in ${wait_s}s — beyond the ${RETRY_CAP_S}s cap; leaving the rest for tomorrow)"
      return "$rc"
    fi
    echo "  (Max window exhausted; sleeping ${wait_s}s until the parsed reset, then retry $n/$((RETRY_MAX - 1)))"
    sleep "$wait_s"
  done
}
