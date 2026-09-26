---
name: troubleshoot
description: Diagnose a misbehaving Animal Tracker install — location stuck on unknown, no events, API down, Ring auth errors, disk full, wrong zones. Use when the user says something isn't working.
---

# Troubleshoot

1. **Always start with** `scripts/run.sh doctor`. It checks the data dir, configs, topology,
   reference photos, Ring token, database, notifications and the running service, and
   prints a fix for each problem. Work through FAILs first.
2. Logs: `tail -50 ~/Library/Logs/WinstonTracker/api.err.log`.
3. Live state: `curl -s localhost:8420/healthz | python3 -m json.tool` and
   `curl -s localhost:8420/tracker/location`.

## Symptoms

- **Location `unknown` right after a change** — check the API is reading the database you
  think it is: `doctor` prints the path and observation count. Relative paths in
  `settings.yaml` resolve against the data dir, not the repo.
- **`unknown` / `last_seen` for a long time** — that's the system being honest when no
  camera has seen the animal. Check the review queue (`scripts/run.sh detect status`): events
  waiting there haven't produced observations yet.
- **Ring errors / 401** — the refresh token expired. The user runs
  `! scripts/run.sh ring-login` (interactive 2FA).
- **healthz `degraded` with `over_limit`** — `scripts/run.sh cleanup` (dry run), then
  `--apply`; install the nightly job with `scripts/install-launchd.sh cleanup`.
- **Impossible or missing transitions** — travel windows. A `min_seconds` that's too large
  rejects real moves; set it to 0 unless measured. Zones that aren't declared neighbors
  are treated as "via skipped cameras" with lower confidence.
- **Port 8420 in use** — an old instance: `lsof -ti:8420`, stop it, then
  `launchctl kickstart -k gui/$(id -u)/com.winstontracker.api`.

Don't "fix" a correct `unknown` by adding defaults. The one rule: never hallucinate a location.
