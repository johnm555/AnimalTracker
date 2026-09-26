#!/usr/bin/env bash
# Run one unattended review session with your AI subscription (no API key).
#
#   scripts/review_session.sh [claude|codex]      # default: claude
#   scripts/install-launchd.sh review [claude|codex]   # every 30 min
#
# The session drains the review queue and spot-checks the local models (the
# review-frames skill). Every verdict it records is training data for the local
# models (scripts/run.sh train). It exits without starting an agent when there
# is nothing to do, so an idle schedule costs nothing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
AGENT="${1:-claude}"
export ANIMAL_TRACKER_DATA="${ANIMAL_TRACKER_DATA:-$HOME/Library/Application Support/AnimalTracker}"

status=$(.venv/bin/python scripts/train.py status --json 2>/dev/null) || {
  echo "$(date '+%F %T') train status failed — run scripts/run.sh doctor"; exit 1; }
read -r queue audits < <(printf '%s' "$status" | .venv/bin/python -c \
  'import json,sys; s=json.load(sys.stdin); print(s.get("review_queue", 0), (s.get("audits") or {}).get("n", 0))')
if [ "$queue" = "0" ] && [ "${AUDIT_EVERY_RUN:-0}" != "1" ]; then
  echo "$(date '+%F %T') review queue empty (audits so far: $audits) — nothing to do"
  exit 0
fi

PROMPT="You are the scheduled Animal Tracker review session. Follow .claude/skills/review-frames/SKILL.md exactly.
1. Drain the review queue: scripts/run.sh detect list --sheets, view the reference sheet and every event sheet, record all verdicts or skips with one scripts/run.sh detect record-batch call.
2. Then spot-check: scripts/run.sh detect audit --sheets --sample 6, record with scripts/run.sh detect audit-record.
3. Finish with scripts/run.sh detect audit-summary and a three-line summary: events reviewed, skips, audit disagreements.
Never record a verdict for frames you did not view. Never change settings, thresholds, code or the database directly.
Write scratch JSON files under ${TMPDIR:-/tmp}."

echo "$(date '+%F %T') starting $AGENT review session ($queue queued)"
case "$AGENT" in
  claude)
    exec claude -p "$PROMPT" \
      --allowedTools "Read" "Write" "Bash(scripts/run.sh detect:*)" "Bash(scripts/run.sh animals:*)" ;;
  codex)
    # Codex reads AGENTS.md automatically; sandbox flags vary by version — see `codex exec --help`.
    exec codex exec "$PROMPT" ;;
  *)
    echo "usage: $0 [claude|codex]" >&2; exit 2 ;;
esac
