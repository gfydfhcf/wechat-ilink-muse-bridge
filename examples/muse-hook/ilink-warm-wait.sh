#!/usr/bin/env bash
# Warm wait: after sending a reply, the worker stays alive for a short
# idle window instead of exiting, so a follow-up message from the user
# is handled by the already-warm worker (context loaded) instead of
# paying a full cold start per message.
#
# Protocol: this script owns the marker file $STATE_DIR/ilink-warm.json
# ({since, activity, heartbeat, until}). While the marker's heartbeat
# is fresh, the hook script does NOT wake a second worker for newly
# landed batches — this waiter sees the pending file within seconds
# and the worker claims it. If the worker dies, the heartbeat goes
# stale and the hook resumes normal waking on its own (self-healing).
#
# Run it once after each reply. It blocks, refreshing the heartbeat,
# and prints exactly one line:
#   NEW_MESSAGES  a batch is waiting in the pending queue — claim it
#                 and handle it WITHOUT re-reading context files
#   IDLE_TIMEOUT  nobody spoke for ILINK_WARM_IDLE seconds — exit
#   TOTAL_CAP     this warm session hit ILINK_WARM_TOTAL seconds
#                 since it started — exit (next message wakes fresh)
#
# Env: ILINK_HOOK_STATE_DIR (legacy alias ILINK_HOOK_STATE),
#      ILINK_WARM_IDLE (default 180), ILINK_WARM_TOTAL (default 1800),
#      ILINK_WARM_POLL (default 5).
set -uo pipefail

STATE_DIR=${ILINK_HOOK_STATE_DIR:-${ILINK_HOOK_STATE:-$HOME/hooks/state}}
WARM=$STATE_DIR/ilink-warm.json
PENDING=$STATE_DIR/ilink-pending.json
IDLE=${ILINK_WARM_IDLE:-180}
TOTAL=${ILINK_WARM_TOTAL:-1800}
POLL=${ILINK_WARM_POLL:-5}

now_s=$(date +%s)
# Continue `since` from an existing marker so the total cap spans the
# whole warm session across successive waits; a marker left by a dead
# session has its cap in the past and ends this call immediately.
since=$now_s
if [ -f "$WARM" ]; then
  prev=$(jq -r '.since // empty' "$WARM" 2>/dev/null || true)
  [ -n "$prev" ] && since=$prev
fi
activity=$now_s
cap=$((since + TOTAL))
deadline=$((activity + IDLE))
[ "$deadline" -gt "$cap" ] && deadline=$cap

write_marker() {
  local tmp="$WARM.tmp.$$"
  jq -n --argjson s "$since" --argjson a "$activity" \
        --argjson h "$(date +%s)" --argjson u "$cap" \
        '{since: $s, activity: $a, heartbeat: $h, until: $u}' \
        > "$tmp" 2>/dev/null && mv "$tmp" "$WARM" 2>/dev/null || true
}
write_marker

while :; do
  n=$(date +%s)
  if [ -s "$PENDING" ]; then
    cnt=$(jq '[.msgs[]?] | length' "$PENDING" 2>/dev/null || echo 0)
    if [ "${cnt:-0}" -gt 0 ] 2>/dev/null; then
      # Keep the marker: its heartbeat going stale is what re-arms the
      # hook if this worker dies before claiming the batch.
      echo NEW_MESSAGES
      exit 0
    fi
  fi
  if [ "$n" -ge "$deadline" ]; then
    rm -f "$WARM"
    if [ "$n" -ge "$cap" ]; then echo TOTAL_CAP; else echo IDLE_TIMEOUT; fi
    exit 0
  fi
  write_marker
  sleep "$POLL"
done
