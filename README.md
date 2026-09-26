# Animal Tracker

A camera-based animal tracker that uses Ring motion clips, a Mac Mini backend,
and an Apple Watch app to tell you exactly where your animal is — and only what
cameras have actually seen. If no camera has seen them, it says so.

Ring tells you "motion detected". This system tells you "your animal just went
from the backyard to the driveway, 40 seconds ago, 92% sure."

## How it works

1. **Ring cameras** detect motion and record clips
2. **Mac Mini backend** downloads clips, extracts frames, runs local vision
   (Apple Vision + DINOv2 embeddings) to identify your enrolled animal
3. **Deterministic state machine** tracks zone transitions using a property
   topology you define — travel times, camera coverage, confidence thresholds
4. **Notifications** via iMessage, Pushover, or APNs when your animal moves
5. **Apple Watch app** shows current location, history, and Siri check-in

## Quick start

```bash
# 1. Clone and set up
git clone https://github.com/johnm555/AnimalTracker.git
cd AnimalTracker
scripts/setup.sh

# 2. Configure your property
DATA_DIR=~/Library/Application\ Support/AnimalTracker
mkdir -p "$DATA_DIR/config" "$DATA_DIR/reference_images"
cp backend/config/settings.example.yaml "$DATA_DIR/config/settings.yaml"
cp backend/config/cameras.example.yaml "$DATA_DIR/config/cameras.yaml"
# Edit both files for your property layout and Ring device IDs

# 3. Add reference photos of your animal
# Place 6+ photos in $DATA_DIR/reference_images/

# 4. Create .env with Ring credentials
cat > "$DATA_DIR/.env" << 'EOF'
RING_USERNAME=your@email.com
RING_PASSWORD=your-ring-password
WINSTON_API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
EOF

# 5. Ring login (one-time 2FA)
scripts/run.sh ring-login

# 6. Start the system
scripts/run.sh api
```

## Architecture

```
~/Library/Application Support/AnimalTracker/   # Site data (your property)
    config/settings.yaml                       # Thresholds, notification backend
    config/cameras.yaml                        # Zones, cameras, travel windows
    reference_images/                          # Enrolled animal photos
    secrets/                                   # FindMy keys, APNs certs
    tracker.db                                 # SQLite observations + transitions
    .env                                       # Ring credentials, API tokens

AnimalTracker/                                 # Framework code (this repo)
    backend/src/                               # Python backend
    backend/tests/                             # Hermetic test suite
    backend/config/*.example.yaml              # Config templates
    ios/WinstonWatch/                          # watchOS + WinstonCore SwiftPM
    scripts/                                   # run.sh, setup.sh, install-launchd.sh
    docs/                                      # Design decisions, dependencies
```

## Key design decisions

- **Observations vs. locations**: the vision layer answers "is this my animal?"
  and returns an Observation. Only the deterministic state machine turns
  observations into zone transitions.
- **Never guess**: if nothing has seen the animal, the answer is `unknown` or
  `last_seen(zone, N minutes ago)` — never a default, never "probably inside".
- **Local-first detection**: Apple Vision animal gate + DINOv2 embeddings run
  on-device (~80ms per event). A Claude session handles uncertain cases.
- **iMessage notifications**: zone changes send journey summaries via macOS
  Messages.app. Reply with `mute`, `unmute`, `status`, `on`, `off`.

## Requirements

- macOS (Apple Vision framework, Messages.app for iMessage notifications)
- Python 3.11+
- Ring cameras with an active subscription
- Xcode (for the watchOS app)

## License

MIT
