#!/bin/bash
# Weekly unattended run, invoked by launchd (see com.radiopharma.sourcing.plist).
#
# Run it by hand any time to test:  ./run_weekly.sh
#
# Deliberately does NOT email anything. Sending mail on your behalf is a
# different kind of action from writing a file, so the output is archived here
# and you read it when you choose.

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR" || exit 1

ARCHIVE="$PROJECT_DIR/archive"
LOG="$PROJECT_DIR/run.log"
STAMP="$(date +%Y-%m-%d)"

mkdir -p "$ARCHIVE"

# launchd starts with a minimal PATH; use an absolute interpreter.
PYTHON="/usr/bin/python3"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

{
  echo "======================================================================"
  echo "run started $(date '+%Y-%m-%d %H:%M:%S %Z')"
} >> "$LOG"

"$PYTHON" run.py >> "$LOG" 2>&1
STATUS=$?

# Archive dated copies. feed.txt and digest.txt are overwritten every run, so
# without this a missed week is simply gone.
[ -f feed.txt ]   && cp feed.txt   "$ARCHIVE/feed-$STAMP.txt"
[ -f digest.txt ] && cp digest.txt "$ARCHIVE/digest-$STAMP.txt"

if [ $STATUS -eq 0 ]; then
  echo "run finished OK $(date '+%H:%M:%S')" >> "$LOG"
else
  echo "run FAILED with status $STATUS $(date '+%H:%M:%S')" >> "$LOG"
fi

# Keep the log from growing without bound.
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 5000 ]; then
  tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

exit $STATUS
