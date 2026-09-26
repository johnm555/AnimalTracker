"""Ring API integration.

Wraps the community `ring_doorbell` library (Ring has no official public API).
Authentication is a placeholder: the first run needs a username, password and
2FA code, after which a refresh token is cached on disk and reused.

Everything here is I/O plumbing; nothing in this module decides whether an
event contains Winston.

Usage sketch::

    client = RingClient.from_settings(settings["ring"])
    client.authenticate(otp_callback=input)      # first run only prompts for 2FA
    for event in client.poll_events(since=last_seen):
        clip = client.download_video(event)
        ...
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .observation import parse_timestamp
from .state_machine import normalize_camera_id

log = logging.getLogger(__name__)

USER_AGENT = "WinstonTracker/0.1"


@dataclass
class MotionEvent:
    """One Ring motion/ding event on a specific camera."""

    event_id: str
    camera_id: str
    """Normalized camera name matching cameras.yaml (e.g. 'back-door')."""
    device_id: str
    timestamp: datetime
    kind: str = "motion"
    """Ring event kind: motion | ding | on_demand."""
    ring_classification: str | None = None
    """Ring's coarse label if present (animal / person / other_motion / vehicle)."""
    recording_status: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.timestamp = parse_timestamp(self.timestamp)


@dataclass
class RingSettings:
    token_cache: Path = Path("./ring_token.cache")
    poll_interval_seconds: float = 15.0
    device_ids: tuple[str, ...] | None = None
    event_kinds: tuple[str, ...] = ("motion", "ding")
    ring_classifications: tuple[str, ...] | None = None
    download_dir: Path = Path("./ring_downloads")

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "RingSettings":
        d = d or {}
        return cls(
            device_ids=tuple(str(v) for v in d["device_ids"]) if "device_ids" in d else None,
            token_cache=Path(d.get("token_cache", "./ring_token.cache")),
            poll_interval_seconds=float(d.get("poll_interval_seconds", 15)),
            event_kinds=tuple(d.get("event_kinds", ("motion", "ding"))),
            ring_classifications=tuple(d["ring_classifications"]) if d.get("ring_classifications") else None,
            download_dir=Path(d.get("download_dir", "./ring_downloads")),
        )


class RingAuthError(RuntimeError):
    pass


class RingClient:
    """Thin adapter over `ring_doorbell`. Safe to construct without credentials."""

    def __init__(self, settings: RingSettings | None = None,
                 username: str | None = None, password: str | None = None) -> None:
        self.settings = settings or RingSettings()
        self.username = username or os.environ.get("RING_USERNAME")
        self.password = password or os.environ.get("RING_PASSWORD")
        self._ring: Any = None
        self._auth: Any = None
        self._devices_by_id: dict[str, Any] = {}

    @classmethod
    def from_settings(cls, d: dict[str, Any] | None) -> "RingClient":
        return cls(RingSettings.from_dict(d))

    # -- auth --------------------------------------------------------------

    def authenticate(self, otp_callback: Callable[[str], str] | None = None) -> None:
        """Log in to Ring, reusing the cached token when possible.

        `otp_callback(prompt) -> code` is invoked when Ring demands a 2FA code.
        Defaults to reading from stdin, which is fine for the one-time setup.
        """
        try:
            from ring_doorbell import Auth, Ring, Requires2FAError  # type: ignore
        except ImportError as e:  # pragma: no cover - depends on optional package
            raise RingAuthError("ring_doorbell is not installed; run scripts/setup.sh") from e

        cache = self.settings.token_cache
        cache.parent.mkdir(parents=True, exist_ok=True)

        def token_updated(token: dict[str, Any]) -> None:
            cache.write_text(json.dumps(token))
            try:
                os.chmod(cache, 0o600)
            except OSError:
                pass

        if cache.is_file():
            token = json.loads(cache.read_text())
            self._auth = Auth(USER_AGENT, token, token_updated)
        else:
            if not (self.username and self.password):
                raise RingAuthError(
                    "no cached Ring token and RING_USERNAME/RING_PASSWORD not set")
            self._auth = Auth(USER_AGENT, None, token_updated)
            try:
                self._auth.fetch_token(self.username, self.password)
            except Requires2FAError:
                if otp_callback is None:
                    import sys

                    if not sys.stdin.isatty():
                        raise RingAuthError(
                            "Ring requires a 2FA code and there is no terminal to ask on. Run "
                            "`scripts/run.sh ring-login` once interactively to create the token cache.")
                    otp_callback = input
                code = otp_callback("Ring 2FA code: ").strip()
                self._auth.fetch_token(self.username, self.password, code)

        self._ring = Ring(self._auth)
        self._ring.update_data()
        self._index_devices()
        log.info("Ring authenticated; %d camera(s) found", len(self._devices_by_id))

    @property
    def is_authenticated(self) -> bool:
        return self._ring is not None

    def _require(self) -> Any:
        if self._ring is None:
            raise RingAuthError("call authenticate() first")
        return self._ring

    # -- devices -----------------------------------------------------------

    def _index_devices(self) -> None:
        # RingDevices exposes `.video_devices` (doorbots + authorized_doorbots +
        # stickup_cams); chimes/intercoms/other are skipped. See docs/Ring_API_Research.md.
        devices = self._require().devices()
        video = getattr(devices, "video_devices", None)
        if video is None:  # very old library versions: dict-style access only
            video = [d for g in ("doorbots", "authorized_doorbots", "stickup_cams") for d in devices[g]]
        allowed = self.settings.device_ids
        self._devices_by_id = {
            str(dev.id): dev for dev in video
            if allowed is None or str(dev.id) in allowed
        }

    def get_cameras(self) -> list[dict[str, Any]]:
        """Return [{device_id, name, camera_id, kind}] for every Ring camera on the account."""
        self._require()
        out = []
        for dev_id, dev in self._devices_by_id.items():
            out.append({
                "device_id": dev_id,
                "name": dev.name,
                "camera_id": normalize_camera_id(dev.name),
                "kind": getattr(dev, "kind", None),
                "battery": getattr(dev, "battery_life", None),
            })
        return sorted(out, key=lambda c: c["camera_id"])

    # -- events ------------------------------------------------------------

    def poll_events(self, since: datetime | None = None, limit: int = 20) -> list[MotionEvent]:
        """Fetch recent events across all cameras, newest last, filtered by settings."""
        self._require()
        events: list[MotionEvent] = []
        for dev_id, dev in self._devices_by_id.items():
            try:
                history = dev.history(limit=limit)  # sync wrapper of async_history
            except Exception as e:  # network hiccups shouldn't kill the poller
                log.warning("history() failed for %s: %s", dev.name, e)
                continue
            for raw in history:
                ev = self._to_event(dev, raw)
                if ev is None:
                    continue
                if since is not None and ev.timestamp <= parse_timestamp(since):
                    continue
                if ev.kind not in self.settings.event_kinds:
                    continue
                if (self.settings.ring_classifications
                        and ev.ring_classification
                        and ev.ring_classification not in self.settings.ring_classifications):
                    continue
                events.append(ev)
        events.sort(key=lambda e: e.timestamp)
        return events

    def stream_events(self, since: datetime | None = None,
                      stop: Callable[[], bool] | None = None) -> Iterator[MotionEvent]:
        """Blocking generator that polls forever and yields new events as they appear."""
        seen: set[str] = set()
        cursor = since
        while not (stop and stop()):
            for ev in self.poll_events(since=cursor):
                if ev.event_id in seen:
                    continue
                seen.add(ev.event_id)
                cursor = max(cursor, ev.timestamp) if cursor else ev.timestamp
                yield ev
            if len(seen) > 5000:
                seen = set(list(seen)[-1000:])
            time.sleep(self.settings.poll_interval_seconds)

    def download_video(self, event: MotionEvent, dest: Path | None = None,
                       retries: int = 6, delay: float = 5.0) -> Path:
        """Download the event's MP4. Ring needs a few seconds to finish encoding."""
        self._require()
        dev = self._devices_by_id.get(event.device_id)
        if dev is None:
            raise KeyError(f"unknown Ring device {event.device_id}")
        dest = dest or (self.settings.download_dir / f"{event.camera_id}-{event.event_id}.mp4")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        # Publish only complete downloads. A failed HTTP transfer must not become
        # a nonempty cached clip on the next attempt.
        partial = dest.with_suffix(dest.suffix + ".part")
        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                try:
                    dev.recording_download(int(event.event_id), filename=str(partial), override=True)
                    if not partial.exists() or partial.stat().st_size == 0:
                        raise RuntimeError("direct recording download returned no data")
                except Exception:
                    # Ring's direct endpoint can return 404 while the same event's
                    # authenticated playback URL is available. Never log signed URLs.
                    import httpx

                    url = dev.recording_url(int(event.event_id))
                    if not url or not url.startswith("https://"):
                        raise RuntimeError("no secure playback URL available") from None
                    with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as response:
                        response.raise_for_status()
                        with partial.open("wb") as output:
                            for chunk in response.iter_bytes():
                                output.write(chunk)
                if not partial.exists() or partial.stat().st_size == 0:
                    raise RuntimeError("recording download returned no data")
                partial.replace(dest)
                return dest
            except Exception as e:
                last_err = e
                partial.unlink(missing_ok=True)
            if attempt + 1 < retries:
                time.sleep(delay)
        raise RuntimeError(
            f"could not download recording {event.event_id}: "
            f"{type(last_err).__name__ if last_err else 'no attempts'}"
        ) from None

    def download_snapshot(self, event: MotionEvent, dest: Path | None = None) -> Path | None:
        """Best-effort current still from the event's device, not event evidence.

        Ring's snapshot API requests a fresh image. Its capture time is not the
        triggering event timestamp; never use it to backfill that event's frames.
        Empty timestamp responses in ring-doorbell 0.9.14 can raise IndexError;
        treat those as unavailable without inventing an image or observation.
        """
        dev = self._devices_by_id.get(event.device_id)
        if dev is None or not hasattr(dev, "get_snapshot"):
            return None
        dest = dest or (self.settings.download_dir / f"{event.camera_id}-{event.event_id}.jpg")
        try:
            data = dev.get_snapshot()
        except Exception as e:
            log.debug("snapshot unavailable for %s (%s)", event.camera_id, type(e).__name__)
            return None
        if not data:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    # -- mapping -----------------------------------------------------------

    @staticmethod
    def _to_event(dev: Any, raw: dict[str, Any]) -> MotionEvent | None:
        try:
            ts = raw.get("created_at")
            if isinstance(ts, str):
                ts = parse_timestamp(ts)
            elif ts is None:
                return None
            cv = raw.get("cv_properties") or {}
            classification = cv.get("detection_type") or raw.get("detection_type")
            return MotionEvent(
                event_id=str(raw.get("id")),
                camera_id=normalize_camera_id(dev.name),
                device_id=str(dev.id),
                timestamp=ts,
                kind=str(raw.get("kind", "motion")),
                ring_classification=classification,
                recording_status=raw.get("recording", {}).get("status") if isinstance(raw.get("recording"), dict) else None,
                raw=raw,
            )
        except Exception as e:
            log.debug("could not parse Ring event %r: %s", raw, e)
            return None


class FixtureRingClient:
    """Offline stand-in that replays events from a directory of clips.

    Expects files named `<camera-id>__<ISO timestamp>.mp4` (or .jpg), e.g.
    `backyard__2026-09-20T14-03-11Z.mp4`. Handy for developing without Ring.
    """

    def __init__(self, fixtures_dir: str | Path) -> None:
        self.dir = Path(fixtures_dir)

    def authenticate(self, otp_callback: Any = None) -> None:  # noqa: ARG002
        return None

    @property
    def is_authenticated(self) -> bool:
        return True

    def get_cameras(self) -> list[dict[str, Any]]:
        cams = sorted({e.camera_id for e in self.poll_events()})
        return [{"device_id": c, "name": c, "camera_id": c, "kind": "fixture"} for c in cams]

    def poll_events(self, since: datetime | None = None, limit: int = 1000) -> list[MotionEvent]:  # noqa: ARG002
        events = []
        for p in sorted(self.dir.glob("*__*.*")):
            cam, _, stamp = p.stem.partition("__")
            stamp = stamp.replace("Z", "").replace("_", ":")
            # allow filesystem-safe HH-MM-SS
            if len(stamp) >= 19 and stamp[13] == "-" and stamp[16] == "-":
                stamp = stamp[:13] + ":" + stamp[14:16] + ":" + stamp[17:]
            try:
                ts = parse_timestamp(stamp).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if since and ts <= parse_timestamp(since):
                continue
            events.append(MotionEvent(event_id=p.name, camera_id=normalize_camera_id(cam),
                                      device_id=cam, timestamp=ts, raw={"path": str(p)}))
        return sorted(events, key=lambda e: e.timestamp)

    def stream_events(self, since: datetime | None = None, stop: Any = None) -> Iterable[MotionEvent]:  # noqa: ARG002
        yield from self.poll_events(since)

    def download_video(self, event: MotionEvent, dest: Path | None = None, **_: Any) -> Path:  # noqa: ARG002
        return Path(event.raw["path"])

    def download_snapshot(self, event: MotionEvent, dest: Path | None = None) -> Path | None:  # noqa: ARG002
        p = Path(event.raw["path"])
        return p if p.suffix.lower() in {".jpg", ".jpeg", ".png"} else None
