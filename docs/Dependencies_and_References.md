# Dependencies and References

*GitHub survey done 2026-09-20 via the GitHub API (stars and last push are as
of that date). Purpose: decide what to depend on versus what to build.*

Legend — **Dependency**: in `requirements.txt` / `Package.swift`.
**Sidecar**: separate binary we run alongside. **Reference**: read it, borrow
ideas, don't depend on it. **Rejected**: looked at, not using, reason given.

## Summary of decisions

| need | decision |
|---|---|
| Ring auth, history, recordings | **Dependency:** `python-ring-doorbell` (already). Add the `[listen]` extra for FCM push. |
| Live/on-demand frames from Ring (WebRTC) | **Sidecar (optional, later):** `go2rtc` — it has native Ring support and a `/api/frame.jpeg` endpoint. Do **not** implement WebRTC/WHEP in Python. |
| Continuous camera streaming | **Rejected** on principle — Ring cameras stop sending motion events while streaming and battery units drain (ring-mqtt maintainer's warning). Event-clip pipeline stays. |
| Vision "is this Winston?" | **Build (done):** Claude with reference photos. LLM Vision is the closest prior art and validates the pattern. |
| Cheap similarity pre-filter | **Reference for later:** `open_clip` / DINOv2 embeddings vs reference set; not needed for v0. |
| Zone/topology state machine | **Build (done).** Nothing on GitHub does camera-zone dog tracking; Frigate's zones are the closest concept but need continuous RTSP. |
| APNs from Python | **Build (done, ~40 lines):** `httpx[http2]` + `PyJWT`. **Rejected:** PyAPNs2 (depends on abandoned `hyper`). Alternative if we go async: `aioapns`. |
| Pushover | **Build (done):** one `httpx.post`. No maintained Python client exists; Pushover's docs recommend raw HTTP. |
| Frame extraction from MP4 | **Dependency:** `opencv-python-headless` (already), ffmpeg fallback. PyAV is the alternative if cv2 misbehaves on Ring's H.264. |
| Generic FSM library | **Rejected:** `pytransitions` — our tracker's logic is topology math, not state-graph plumbing. |

---

## 1. Ring camera integrations

### python-ring-doorbell — **Dependency**
- https://github.com/python-ring-doorbell/python-ring-doorbell — 699 ★, pushed 2026-02-28, v0.9.14
- Python, sync + async, 2FA + refresh-token callback, history, recording download, snapshots, FCM push listener (`ring_doorbell[listen]`, built on `sdb9696/firebase-messaging`, 16 ★ but the same author co-maintains ring-doorbell and Home Assistant's Ring integration).
- Powers Home Assistant's `ring` integration, so it is exercised by thousands of installs even though the repo itself is quiet.
- Use for: everything in `backend/src/ring_client.py`. The module docstring documents auth, history, recordings and pitfalls.
- Caveat: no live-video/WebRTC support. Cadence is slow (last push Feb 2026); if Ring breaks auth, the Node ecosystem below usually fixes it first — watch their changelogs.

### dgreif/ring (`ring-client-api`) — **Reference** (most active Ring client)
- https://github.com/dgreif/ring — 1,521 ★, pushed 2026-09-09. TypeScript.
- Best-maintained reverse-engineering of the Ring API. Has live streaming over WebRTC (`werift`), `camera.recordToFile()`, `streamVideo()`, `getSnapshot()`, `getEvents()`, `getRecordingUrl(id, {transcoded})`, push via `@eneris/push-receiver`, `onMotionDetected` observables.
- Use for: reading how auth refresh, `hardware_id`, and push subscription work when python-ring-doorbell breaks; understanding the WebRTC signaling if we ever need it.
- Not a dependency: would mean a Node process next to Python. If we ever need live frames, go2rtc (below) already embeds this knowledge.

### tsightler/ring-mqtt — **Reference**
- https://github.com/tsightler/ring-mqtt — 789 ★, pushed 2026-09-03. Node, built on ring-client-api; exposes cameras over MQTT + an RTSP gateway (via go2rtc).
- Use for: its README's operational warnings — Ring cameras are event devices; continuous streaming suppresses motion alerts, drains batteries, overheats devices, and "attempts to use with Frigate/NVR tools will end in disappointment". This is the strongest argument for our clip-per-event architecture.
- Could be a sidecar if we ever wanted MQTT events instead of polling; not needed.

### AlexxIT/go2rtc — **Sidecar (optional, later)**
- https://github.com/AlexxIT/go2rtc — 14,226 ★, pushed 2026-09-06. Single Go binary.
- Native `ring:` source since v1.9.13 (`ring:?device_id=…&refresh_token=…`, plus `&snapshot` variant); outputs WebRTC/WHEP, RTSP, MJPEG, and `GET /api/frame.jpeg?src=<name>` for a single JPEG.
- Use for: an on-demand "look now" frame when the tracker is in TRANSITIONING and wants a confirming look without waiting for the next motion event. Python side is just `httpx.get(".../api/frame.jpeg?src=backyard")`.
- Trade-off: each pull starts a short live session on the camera (see ring-mqtt warning) — rate-limit to a few per hour per camera, battery cams especially.

### abracadabra50/open-ring — **Reference**
- https://github.com/abracadabra50/open-ring — 109 ★, pushed 2026-07-25. Unofficial API + macOS menu bar app.
- Use for: a second, recent reading of the current Ring endpoints (useful when debugging auth on macOS specifically). Small project; don't depend on it.

### Home Assistant `ring` integration — **Reference**
- https://github.com/home-assistant/core (`homeassistant/components/ring/`) — 90k ★.
- Use for: how a production consumer of python-ring-doorbell handles token refresh, polling intervals (~60 s per account for history), and event de-duplication.

## 2. Pet / dog tracking with home cameras

There is **no existing project** that does zone-based location tracking of a
specific pet across multiple home cameras. What exists:

### blakeblackshear/frigate — **Reference**
- https://github.com/blakeblackshear/frigate — 36,018 ★, pushed 2026-09-20. NVR with real-time object detection, per-camera **zones**, `dog`/`cat` labels out of the box, GenAI descriptions (OpenAI/Gemini/Ollama plugins), HA integration.
- Use for: the zone model (polygon zones per camera, "entered zone" events, `zones` + `current_zones` on tracked objects) and its object lifecycle (`new` → `update` → `end`) as vocabulary and design cross-check for our tracker.
- Not usable directly: needs continuous RTSP streams — incompatible with Ring (see ring-mqtt). If the house ever gets wired PoE cameras, Frigate becomes the perception layer and our tracker consumes its MQTT events instead of Ring clips.

### valentinfrlch/ha-llmvision — **Reference (closest prior art)**
- https://github.com/valentinfrlch/ha-llmvision — 1,474 ★, pushed 2026-09-17. Home Assistant integration sending camera images / Frigate events to multimodal LLMs (Anthropic, OpenAI, Gemini, Ollama, …).
- Has a **Memory** feature: user-uploaded reference images with labels are prepended to every request with the prompt *"The following images along with descriptions serve as reference. They are not to be mentioned in the response."* — the same reference-image pattern as `winston_detector.build_messages`, minus our verification-only framing and structured output.
- Use for: prompt ideas, provider abstractions, their `media_handlers.py` for frame sampling from clips, and evidence that per-event LLM vision on home cameras is viable and affordable.

### Small hobby repos (all < 5 ★, 2025–2026)
- `merceadlaon06/Chicken_Dog_Detection-discord-warning`, `gd-25/ubuntu` (Tapo camera dog monitor with YAMNet audio), `adea820616/DogCam`, `cobryan05/purrview`, `sanyabeast/tele_spotter` — YOLO/captioning → Telegram/Discord. None do identification or multi-camera state. **Reference only for sanity-checking notification UX.**

### Dog re-identification research code — **Reference for later**
- `eugeniodias5/BIFOR` (paper: background-invariant dog re-ID), `markoMedved/DogReID-1553` (video re-ID dataset), `ddyy-hash/dog-reid-…` (YOLOv8 + SAM + OSNet), `Grechka67/Biometric-Pet-Identification` (nose-print + face). All ≤ 2 ★, academic.
- Use for: if the LLM's `visual_similarity` proves noisy, a metric-learning embedding fine-tuned on Winston vs. neighborhood dogs is the next step; these show the recipe (YOLO crop → embedding → cosine vs. gallery).

## 3. Vision-model pet identification

### Our approach (Claude + reference images + JSON schema) — **Build (done)**
Nothing on GitHub packages "identify *this specific* pet with a VLM"; LLM
Vision's Memory is the nearest. Keep ours.

**2026-09-20: the Anthropic API dependency is removed for the MVP.** There
is no API key (subscription plan only). The same prompt, schema and fusion
run in *session mode*: a scheduled Claude Code session views staged frames
and records the verdict (ADR-013, `.claude/skills/review-frames/SKILL.md`). The
`anthropic` package stays in `requirements.txt` only for `detector.mode: api`
and its tests; nothing in the running system imports it.

### mlfoundations/open_clip — **Reference for later**
- https://github.com/mlfoundations/open_clip — 14,153 ★, pushed 2026-09-08.
- Use for: a local, free `visual_similarity` signal — embed reference photos once, embed each frame's animal crop, cosine similarity. Runs fine on Apple Silicon via PyTorch MPS. Would let us skip the Claude call entirely for obvious non-matches (cats, raccoons, people) and cut cost.

### facebookresearch/dinov2 — **Reference for later**
- https://github.com/facebookresearch/dinov2 — 13,349 ★, pushed 2026-06-03.
- Same role as CLIP; DINOv2 features are generally stronger for fine-grained same-species instance matching. Pick one after measuring on logged frames (ADR-010).

### ultralytics/ultralytics — **Reference for later**
- https://github.com/ultralytics/ultralytics — 61,819 ★. YOLO `dog` detector to crop the animal before embedding/LLM; also gives a size estimate (bbox vs. frame) for `size_appearance_compatible`. Only worth adding alongside CLIP/DINOv2.

## 4. WebRTC / WHEP frame extraction

**Decision: don't do WebRTC in Python.** Ring live view is WebRTC with
Ring-specific signaling (see `dgreif/ring/packages/ring-client-api/streaming/`);
reimplementing it on `aiortc` is a maintenance sink for a feature the event
pipeline doesn't need.

- **aiortc** — https://github.com/aiortc/aiortc — 5,104 ★, pushed 2026-07-17. The Python WebRTC stack. Reference only; would need Ring's signaling ported.
- **go2rtc** — see §1. **The answer**: it terminates Ring WebRTC and hands us JPEG over HTTP or RTSP for ffmpeg.
- **bluenviron/mediamtx** — 20,208 ★ — WHEP/RTSP server; no Ring source, so go2rtc wins for us.
- **pion/webrtc** — 16,786 ★ — what go2rtc is built on. Reference.
- **PyAV** — https://github.com/PyAV-Org/PyAV — 3,286 ★, pushed 2026-09-17. FFmpeg bindings; alternative to OpenCV for decoding clips (and for reading an RTSP feed from go2rtc if we ever do).

## 5. APNs from a Python backend

- **Pr0Ger/PyAPNs2** — 357 ★, last push 2024-04-19 — **Rejected.** Depends on `hyper`, an abandoned HTTP/2 library that breaks on Python ≥ 3.10.
- **Fatal1ty/aioapns** — https://github.com/Fatal1ty/aioapns — 164 ★, pushed 2025-04-14. asyncio, `h2`-based, token (`key_id`/`team_id`/`topic`) and cert auth, sandbox toggle. **Alternative** if the notifier becomes async; API maps 1:1 onto our `APNsSender` fields.
- **jazzband/django-push-notifications** — 2,387 ★ — Django-only. Reference for token storage schema (`APNSDevice`) when we add the device-registration endpoint.
- **sideshow/apns2** (Go, 3,189 ★) / **node-apn** (4,399 ★) — reference implementations of header handling (`apns-push-type`, `apns-collapse-id`, `apns-expiration`), which our sender already mirrors.
- **caronc/apprise** — 17,363 ★, pushed 2026-09-20 — 100+ notification services behind one URL scheme (Pushover, Telegram, ntfy, …; not APNs). **Optional dependency** if you want a second channel besides Pushover during the prototype phase without writing senders.
- **gotify/server** — 15,936 ★ — self-hosted push; Android-first, no watch story. Skip.

Current implementation (`httpx[http2]` + `PyJWT`, ~40 lines) stays. Both deps are first-tier maintained.

## 6. Multi-camera zone / presence state machines

Nothing camera-based. Adjacent:

- **agittins/bermuda** — https://github.com/agittins/bermuda — 2,037 ★, pushed 2026-09-15. BLE trilateration for room presence in HA.
- **ESPresense/ESPresense** — 1,477 ★, pushed 2026-09-18. ESP32 BLE nodes for room-level presence.
- Use for: a **complementary, non-visual signal**. A BLE tag on Winston's collar plus one ESP32 indoors would give a cheap, high-confidence "he is in the house" observation for the zone no camera covers well. It would enter the tracker as just another `Observation` (camera_id = `ble-indoor`), no design change. Park it in the backlog.
- **pytransitions/transitions** — 6,593 ★ — generic FSM. **Rejected**: our transitions are computed from topology + time, not a fixed graph; a library adds indirection without removing code.
- **Frigate zones** — see §2; best conceptual cross-check.

## Changes made to the repo from this survey

1. `backend/requirements.txt`: `ring-doorbell>=0.9` → `ring-doorbell[listen]>=0.9.14` so the FCM push path (`RingEventListener`) is installable.
2. `AGENTS.md` / `CLAUDE.md`: added a *Dependency policy* section pointing here.
3. No new Python dependencies otherwise; PyAPNs2 explicitly not adopted.

## Re-check cadence

Ring's API breaks a few times a year. When `ring_client` starts failing:
check `dgreif/ring` releases first (they patch fastest), then
`python-ring-doorbell` issues, then Home Assistant's `ring` component PRs.
