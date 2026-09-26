#!/usr/bin/env bash
# Install (or remove) WinstonTracker as a per-user launchd agent on the Mac Mini.
#
#   scripts/install-launchd.sh            install + start; restarts on crash and at login
#   scripts/install-launchd.sh uninstall  stop + remove
#   scripts/install-launchd.sh status
#   scripts/install-launchd.sh cleanup    install the daily 03:00 retention job (run.sh cleanup --apply)
#   scripts/install-launchd.sh uninstall-cleanup
#
# Runs `scripts/run.sh api` (API server + in-process Ring poller). Logs go to
# ~/Library/Logs/WinstonTracker/. The cleanup job is a second, independent
# agent so the disk stays protected even if the API agent is stopped. The agent needs the user to be logged in
# (LaunchAgent, not LaunchDaemon) because the Ring token cache and .env live
# in the user's home directory; enable auto-login on the Mac Mini for
# unattended reboots.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.winstontracker.api"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
CLEAN_LABEL="com.winstontracker.cleanup"
CLEAN_PLIST="$HOME/Library/LaunchAgents/$CLEAN_LABEL.plist"
LOG_DIR="$HOME/Library/Logs/WinstonTracker"
UID_NUM="$(id -u)"

case "${1:-install}" in
  install)
    [ -x "$ROOT/.venv/bin/python" ] || { echo "Run scripts/setup.sh first." >&2; exit 1; }
    [ -f "$ROOT/.env" ] || { echo "Create $ROOT/.env first (see .env.example)." >&2; exit 1; }
    mkdir -p "$LOG_DIR" "$(dirname "$PLIST")"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT/scripts/run.sh</string>
    <string>api</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key><false/>
  </dict>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/api.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/api.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>ANIMAL_TRACKER_DATA</key><string>$HOME/Library/Application Support/AnimalTracker</string>
  </dict>
</dict>
</plist>
EOF
    launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$UID_NUM" "$PLIST"
    launchctl kickstart -k "gui/$UID_NUM/$LABEL"
    echo "Installed $LABEL. Logs: $LOG_DIR"
    sleep 3
    curl -fsS "http://127.0.0.1:8420/healthz" 2>/dev/null | python3 -m json.tool || echo "(API not answering yet; check $LOG_DIR/api.err.log)"
    ;;
  uninstall)
    launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed $LABEL."
    ;;
  status)
    launchctl print "gui/$UID_NUM/$LABEL" 2>/dev/null | grep -E "state|pid|last exit" || echo "$LABEL is not loaded"
    launchctl print "gui/$UID_NUM/$CLEAN_LABEL" 2>/dev/null | grep -E "state|last exit" | sed "s/^/  cleanup: /" || echo "$CLEAN_LABEL is not loaded"
    curl -fsS "http://127.0.0.1:8420/healthz" 2>/dev/null | python3 -m json.tool || echo "API not answering on 127.0.0.1:8420"
    ;;
  cleanup)
    [ -x "$ROOT/.venv/bin/python" ] || { echo "Run scripts/setup.sh first." >&2; exit 1; }
    mkdir -p "$LOG_DIR" "$(dirname "$CLEAN_PLIST")"
    cat > "$CLEAN_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$CLEAN_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT/scripts/run.sh</string>
    <string>cleanup</string>
    <string>--apply</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>3</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$LOG_DIR/cleanup.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/cleanup.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>ANIMAL_TRACKER_DATA</key><string>$HOME/Library/Application Support/AnimalTracker</string>
  </dict>
</dict>
</plist>
EOF
    launchctl bootout "gui/$UID_NUM/$CLEAN_LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$UID_NUM" "$CLEAN_PLIST"
    echo "Installed $CLEAN_LABEL: runs 'run.sh cleanup --apply' daily at 03:00 (launchd runs it at next wake if the Mini was asleep). Log: $LOG_DIR/cleanup.log"
    ;;
  uninstall-cleanup)
    launchctl bootout "gui/$UID_NUM/$CLEAN_LABEL" 2>/dev/null || true
    rm -f "$CLEAN_PLIST"
    echo "Removed $CLEAN_LABEL."
    ;;
  *)
    echo "usage: $0 [install|uninstall|status|cleanup|uninstall-cleanup]" >&2; exit 2 ;;
esac
