"""Notification policy and delivery.

Policy (NotificationPolicy)
    Decides *whether* and *how loudly* to notify for a TransitionEvent:
      - every confirmed zone change is notifiable;
      - arrivals in `high_priority_zones` (front, driveway = street-adjacent)
        are time-sensitive alerts;
      - the same (from -> to) transition repeated within `cooldown_seconds`
        is downgraded to a silent update (still refreshes the watch face);
      - during quiet hours only high-priority alerts make noise.
    Debouncing of repeated sightings inside one zone already happened in the
    state machine, so by the time an event reaches this layer it is a real
    zone change.

Delivery (Sender implementations)
    LogSender        prints; default for development
    IMessageSender   macOS Messages.app → iMessage/SMS to iPhone+Watch
    PushoverSender   quick prototype on iPhone/Watch via Pushover
    APNsSender       production, token-based APNs to the Watch app

Payload
    build_payload() produces one dict used by every backend. The `aps`
    section is what APNs needs; the `winston` section is what the SwiftUI
    watch app decodes.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .db import Database
from .observation import parse_timestamp, utcnow
from .state_machine import TransitionEvent

log = logging.getLogger(__name__)

NORMAL = "normal"
HIGH_PRIORITY = "high_priority"
SILENT = "silent"
SUPPRESSED = "suppressed"

APNS_CATEGORY = "WINSTON_MOVED"
APNS_THREAD_ID = "winston-location"


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #

@dataclass
class NotificationDecision:
    type: str
    title: str
    body: str
    reason: str = ""

    @property
    def should_send(self) -> bool:
        return self.type != SUPPRESSED


@dataclass
class PolicyConfig:
    cooldown_seconds: float = 300.0
    high_priority_zones: tuple[str, ...] = ("front", "driveway")
    quiet_hours_start: dtime | None = None
    quiet_hours_end: dtime | None = None
    min_confidence: float = 0.0
    """Transitions below this confidence are logged but not pushed."""
    zone_labels: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, d: dict[str, Any] | None) -> "PolicyConfig":
        d = d or {}
        qh = d.get("quiet_hours") or {}
        return cls(
            cooldown_seconds=float(d.get("cooldown_seconds", 300)),
            high_priority_zones=tuple(d.get("high_priority_zones", ("front", "driveway"))),
            quiet_hours_start=_parse_time(qh.get("start")),
            quiet_hours_end=_parse_time(qh.get("end")),
            min_confidence=float(d.get("min_confidence", 0.0)),
            zone_labels=dict(d.get("zone_labels") or {}),
        )


class NotificationPolicy:
    def __init__(self, config: PolicyConfig | None = None) -> None:
        self.config = config or PolicyConfig()
        self._last_sent: dict[tuple[str | None, str], datetime] = {}
        self.muted_until: datetime | None = None

    def decide(self, event: TransitionEvent, now: datetime | None = None,
               *, delivery_time: datetime | None = None) -> NotificationDecision:
        cfg = self.config
        now = parse_timestamp(now) if now else event.arrived_at
        title, body = self._describe(event)
        key = (event.from_zone, event.to_zone)

        if event.confidence < cfg.min_confidence:
            return NotificationDecision(SUPPRESSED, title, body,
                                        f"confidence {event.confidence:.2f} < {cfg.min_confidence:.2f}")

        # A session can review an old clip during a current mute. Compare with
        # delivery time, never the historical sighting timestamp.
        delivery_time = parse_timestamp(delivery_time) if delivery_time is not None else utcnow()
        if self.muted_until is not None and delivery_time < self.muted_until:
            return NotificationDecision(SILENT, title, body,
                                        f"manual mute until {self.muted_until.isoformat()}")

        high = event.to_zone in cfg.high_priority_zones
        last = self._last_sent.get(key)
        in_cooldown = last is not None and (now - last).total_seconds() < cfg.cooldown_seconds

        if in_cooldown:
            # Repeated pacing between the same two zones: keep the watch face
            # current but don't buzz again.
            return NotificationDecision(SILENT, title, body, "repeat transition within cooldown")

        if self._in_quiet_hours(now) and not high:
            self._last_sent[key] = now
            return NotificationDecision(SILENT, title, body, "quiet hours")

        self._last_sent[key] = now
        return NotificationDecision(HIGH_PRIORITY if high else NORMAL, title, body,
                                    "high-priority zone" if high else "zone change")

    def seed_last_sent(self, from_zone: str | None, to_zone: str, at: datetime) -> None:
        """Restore cooldown state from the DB after a restart."""
        self._last_sent[(from_zone, to_zone)] = parse_timestamp(at)

    # -- helpers -----------------------------------------------------------

    def _describe(self, event: TransitionEvent) -> tuple[str, str]:
        to = self.label(event.to_zone)
        if event.is_initial_sighting:
            return "Winston spotted", f"Winston is in the {to}."
        frm = self.label(event.from_zone or "")
        if event.to_zone in self.config.high_priority_zones:
            return f"Winston is in the {to}", f"Moved from the {frm} to the {to}. Street-adjacent."
        return f"Winston → {to}", f"Moved from the {frm} to the {to}."

    def label(self, zone: str) -> str:
        return self.config.zone_labels.get(zone, zone.replace("-", " "))

    def _in_quiet_hours(self, now: datetime) -> bool:
        s, e = self.config.quiet_hours_start, self.config.quiet_hours_end
        if s is None or e is None:
            return False
        t = now.astimezone().time()  # quiet hours are in local time
        if s <= e:
            return s <= t < e
        return t >= s or t < e  # wraps midnight


def _parse_time(v: str | None) -> dtime | None:
    if not v:
        return None
    h, m = str(v).split(":")[:2]
    return dtime(int(h), int(m))


# --------------------------------------------------------------------------- #
# Payload
# --------------------------------------------------------------------------- #

def build_payload(event: TransitionEvent, decision: NotificationDecision) -> dict[str, Any]:
    """One payload for all backends. Matches what the Watch app decodes."""
    aps: dict[str, Any] = {
        "category": APNS_CATEGORY,
        "thread-id": APNS_THREAD_ID,
    }
    if decision.type == SILENT:
        aps["content-available"] = 1
    else:
        aps["alert"] = {"title": decision.title, "body": decision.body}
        aps["sound"] = "default"
        aps["interruption-level"] = "time-sensitive" if decision.type == HIGH_PRIORITY else "active"
        aps["relevance-score"] = 1.0 if decision.type == HIGH_PRIORITY else 0.5
    return {
        "aps": aps,
        "winston": {
            "type": decision.type,
            "zone": event.to_zone,
            "from_zone": event.from_zone,
            "arrived_at": event.arrived_at.isoformat(),
            "departed_at": event.departed_at.isoformat() if event.departed_at else None,
            "confidence": round(event.confidence, 3),
            "transition_id": event.id,
        },
    }


# --------------------------------------------------------------------------- #
# Senders
# --------------------------------------------------------------------------- #

class Sender(Protocol):
    name: str

    def send(self, payload: dict[str, Any], decision: NotificationDecision) -> None: ...


class LogSender:
    name = "log"

    def send(self, payload: dict[str, Any], decision: NotificationDecision) -> None:
        log.info("[%s] %s — %s", decision.type, decision.title, decision.body)


class PushoverSender:
    """Prototype delivery via https://pushover.net (works on iPhone + Watch mirroring)."""

    name = "pushover"
    URL = "https://api.pushover.net/1/messages.json"

    def __init__(self, user_key: str | None = None, api_token: str | None = None,
                 user_key_env: str = "PUSHOVER_USER_KEY", api_token_env: str = "PUSHOVER_API_TOKEN") -> None:
        self.user_key = user_key or os.environ.get(user_key_env)
        self.api_token = api_token or os.environ.get(api_token_env)
        if not (self.user_key and self.api_token):
            raise ValueError("Pushover user key and API token are required")

    def send(self, payload: dict[str, Any], decision: NotificationDecision) -> None:
        if decision.type == SILENT:
            return  # Pushover has no silent/background delivery
        import httpx

        priority = 1 if decision.type == HIGH_PRIORITY else 0
        data = {
            "token": self.api_token,
            "user": self.user_key,
            "title": decision.title,
            "message": decision.body,
            "priority": priority,
            "sound": "siren" if priority == 1 else "pushover",
            "timestamp": int(parse_timestamp(payload["winston"]["arrived_at"]).timestamp()),
        }
        r = httpx.post(self.URL, data=data, timeout=10.0)
        r.raise_for_status()


class APNsSender:
    """Token-based (p8) APNs delivery over HTTP/2."""

    name = "apns"

    def __init__(
        self,
        team_id: str,
        key_id: str,
        private_key_path: str | Path,
        bundle_id: str,
        device_tokens: list[str],
        use_sandbox: bool = True,
        db: Database | None = None,
    ) -> None:
        self.team_id = team_id
        self.key_id = key_id
        self.private_key = Path(private_key_path).read_text()
        self.bundle_id = bundle_id
        self.static_tokens = [t for t in device_tokens if t]
        self.db = db
        self.host = "https://api.sandbox.push.apple.com" if use_sandbox else "https://api.push.apple.com"
        self._jwt: str | None = None
        self._jwt_issued: float = 0.0

    @classmethod
    def from_settings(cls, d: dict[str, Any], db: Database | None = None) -> "APNsSender":
        tokens = os.environ.get(d.get("device_tokens_env", "APNS_DEVICE_TOKENS"), "")
        missing = [k for k in (d.get("team_id_env", "APNS_TEAM_ID"), d.get("key_id_env", "APNS_KEY_ID"),
                               d.get("private_key_path_env", "APNS_PRIVATE_KEY_PATH")) if not os.environ.get(k)]
        if missing:
            raise ValueError(f"APNs backend selected but {', '.join(missing)} not set in the environment")
        return cls(
            team_id=os.environ[d.get("team_id_env", "APNS_TEAM_ID")],
            key_id=os.environ[d.get("key_id_env", "APNS_KEY_ID")],
            private_key_path=os.environ[d.get("private_key_path_env", "APNS_PRIVATE_KEY_PATH")],
            bundle_id=d["bundle_id"],
            device_tokens=[t.strip() for t in tokens.split(",")],
            use_sandbox=bool(d.get("use_sandbox", True)),
            db=db,
        )

    @property
    def device_tokens(self) -> list[str]:
        """Tokens registered by the watch app (DB) plus any pinned in APNS_DEVICE_TOKENS."""
        tokens = list(self.static_tokens)
        if self.db is not None:
            tokens += [row["token"] for row in self.db.list_device_tokens()]
        return list(dict.fromkeys(t.lower() for t in tokens))

    def _token(self) -> str:
        # APNs rejects provider tokens older than an hour; refresh every 50 min.
        if self._jwt is None or time.time() - self._jwt_issued > 50 * 60:
            import jwt

            self._jwt_issued = time.time()
            self._jwt = jwt.encode(
                {"iss": self.team_id, "iat": int(self._jwt_issued)},
                self.private_key, algorithm="ES256", headers={"kid": self.key_id},
            )
        return self._jwt

    def send(self, payload: dict[str, Any], decision: NotificationDecision) -> None:
        import httpx

        headers = {
            "authorization": f"bearer {self._token()}",
            "apns-topic": self.bundle_id,
            "apns-push-type": "background" if decision.type == SILENT else "alert",
            "apns-priority": "5" if decision.type == SILENT else "10",
            "apns-collapse-id": APNS_THREAD_ID,
            "apns-expiration": str(int(time.time()) + 3600),
        }
        body = json.dumps(payload)
        tokens = self.device_tokens
        if not tokens:
            raise RuntimeError("no APNs device tokens registered (open the watch app once, or set APNS_DEVICE_TOKENS)")
        errors = []
        with httpx.Client(http2=True, timeout=10.0) as client:
            for token in tokens:
                r = client.post(f"{self.host}/3/device/{token}", content=body, headers=headers)
                if r.status_code == 200:
                    continue
                reason = ""
                try:
                    reason = r.json().get("reason", "")
                except ValueError:
                    pass
                if r.status_code == 410 or reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"):
                    # The device uninstalled the app or the token belongs to another build: stop
                    # sending to it, keep the row so the app can re-register.
                    if self.db is not None:
                        self.db.disable_device_token(token)
                    log.warning("APNs token %s… disabled: %s %s", token[:8], r.status_code, reason)
                    continue
                errors.append(f"{token[:8]}…: {r.status_code} {reason or r.text}")
        if errors:
            raise RuntimeError("APNs errors: " + "; ".join(errors))


class IMessageSender:
    """Send notifications via macOS Messages.app (iMessage / SMS).

    Journey mode (default): buffers zone changes and sends a summary when
    Winston settles (no new transition for ``settle_seconds``).  High-priority
    zone arrivals still fire immediately.  The owner can reply with simple
    commands (mute/unmute/status/on/off) to control notifications.
    """

    name = "imessage"

    def __init__(self, recipient: str, *, settle_seconds: float = 300.0,
                 notifier: "NotificationService | None" = None) -> None:
        self.recipient = recipient
        if not recipient:
            raise ValueError("iMessage recipient (phone number or Apple ID email) is required")
        self.settle_seconds = settle_seconds
        self._journey: list[tuple[TransitionEvent, NotificationDecision]] = []
        self._settle_timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._notifier: NotificationService | None = notifier
        self._enabled = True
        # iMessage command listener
        self._cmd_last_rowid: int | None = None
        self._cmd_thread: threading.Thread | None = None

    @classmethod
    def from_settings(cls, d: dict[str, Any]) -> "IMessageSender":
        env_var = d.get("recipient_env", "IMESSAGE_RECIPIENT")
        recipient = os.environ.get(env_var, d.get("recipient", ""))
        settle = float(d.get("settle_seconds", 300))
        return cls(recipient, settle_seconds=settle)

    def set_notifier(self, notifier: "NotificationService") -> None:
        """Called after NotificationService is constructed (circular ref)."""
        self._notifier = notifier

    def start_command_listener(self, poll_interval: float = 15.0) -> None:
        """Start a background thread that reads iMessage replies."""
        if self._cmd_thread is not None:
            return
        self._cmd_thread = threading.Thread(
            target=self._poll_commands, args=(poll_interval,),
            daemon=True, name="imessage-cmd",
        )
        self._cmd_thread.start()
        log.info("iMessage command listener started (poll every %.0fs)", poll_interval)

    def send(self, payload: dict[str, Any], decision: NotificationDecision) -> None:
        if decision.type == SILENT:
            return
        if not self._enabled:
            log.info("iMessage disabled by owner — skipping")
            return

        # Reconstruct the TransitionEvent from payload for journey buffering.
        w = payload.get("winston", {})
        event = TransitionEvent(
            id=w.get("transition_id", 0),
            from_zone=w.get("from_zone"),
            to_zone=w.get("zone", ""),
            arrived_at=parse_timestamp(w["arrived_at"]) if w.get("arrived_at") else utcnow(),
            confidence=w.get("confidence", 0.0),
        )

        # High-priority: send immediately AND include in journey summary.
        if decision.type == HIGH_PRIORITY:
            self._send_text(f"⚠️ {decision.title}\n{decision.body}")
            # Don't buffer high-priority — it's already sent.
            return

        # Buffer normal transitions for journey summarization.
        with self._lock:
            self._journey.append((event, decision))
            # Reset the settle timer.
            if self._settle_timer is not None:
                self._settle_timer.cancel()
            self._settle_timer = threading.Timer(self.settle_seconds, self._flush_journey)
            self._settle_timer.daemon = True
            self._settle_timer.start()

    def _flush_journey(self) -> None:
        """Called when the settle timer fires — movement has stopped."""
        with self._lock:
            if not self._journey:
                return
            steps = list(self._journey)
            self._journey.clear()
            self._settle_timer = None

        # Build journey summary.
        zones = [steps[0][0].from_zone] if steps[0][0].from_zone else []
        for event, _ in steps:
            zones.append(event.to_zone)

        settled_zone = zones[-1]
        confidence = steps[-1][0].confidence

        if len(zones) <= 2:
            # Simple A → B move.
            route = " → ".join(_zone_label(z) for z in zones if z)
            text = f"🐾 Winston settled: {route}"
        else:
            # Multi-step journey.
            route = " → ".join(_zone_label(z) for z in zones if z)
            text = f"🐾 Winston settled in the {_zone_label(settled_zone)}\nRoute: {route}"

        text += f"\n({confidence:.0%} confidence)"

        try:
            self._send_text(text)
        except Exception as e:
            log.error("iMessage journey summary failed: %s", e)

    def _send_text(self, text: str) -> None:
        import subprocess

        # Keep all user-controlled values out of AppleScript source.
        script = (
            'on run argv\n'
            '  tell application "Messages"\n'
            '    set targetService to 1st account whose service type = iMessage\n'
            '    set targetBuddy to participant (item 1 of argv) of targetService\n'
            '    send (item 2 of argv) to targetBuddy\n'
            '  end tell\n'
            'end run'
        )
        result = subprocess.run(
            ["osascript", "-e", script, "--", self.recipient, text],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            raise RuntimeError(f"osascript failed: {result.stderr.strip()}")

    # ---- iMessage command listener ----------------------------------------

    def _poll_commands(self, interval: float) -> None:
        """Poll ~/Library/Messages/chat.db for incoming replies."""
        import sqlite3

        db_path = Path.home() / "Library" / "Messages" / "chat.db"
        if not db_path.exists():
            log.warning("Messages chat.db not found — command listener disabled")
            return

        # Seed the last rowid so we only process NEW messages.
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT MAX(ROWID) as mx FROM message").fetchone()
            self._cmd_last_rowid = row["mx"] or 0
            conn.close()
        except Exception as e:
            log.warning("Cannot read chat.db: %s — command listener disabled", e)
            return

        while True:
            time.sleep(interval)
            try:
                self._check_new_messages(db_path)
            except Exception as e:
                log.debug("iMessage poll error: %s", e)

    def _check_new_messages(self, db_path: Path) -> None:
        import sqlite3

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("""
                SELECT m.ROWID, m.text, m.is_from_me, h.id as handle_id
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                WHERE m.ROWID > ? AND m.is_from_me = 1
                ORDER BY m.ROWID
            """, (self._cmd_last_rowid,)).fetchall()
        finally:
            conn.close()

        for row in rows:
            self._cmd_last_rowid = row["ROWID"]
            text = (row["text"] or "").strip().lower()
            if not text:
                continue
            self._handle_command(text)

    def _handle_command(self, text: str) -> None:
        if text in ("mute", "quiet", "shh", "stop"):
            if self._notifier:
                self._notifier.set_mute(60)
            self._send_text("🔇 Muted for 1 hour.\nReply \"unmute\" to resume.")
        elif text.startswith("mute ") and text.split()[-1].isdigit():
            minutes = min(int(text.split()[-1]), 1440)
            if self._notifier:
                self._notifier.set_mute(minutes)
            self._send_text(f"🔇 Muted for {minutes} min.\nReply \"unmute\" to resume.")
        elif text in ("unmute", "resume", "start"):
            if self._notifier:
                self._notifier.set_mute(0)
            self._send_text("🔔 Notifications resumed.")
        elif text in ("off", "disable"):
            self._enabled = False
            self._send_text("📴 iMessage notifications off.\nReply \"on\" to re-enable.")
        elif text in ("on", "enable"):
            self._enabled = True
            self._send_text("📱 iMessage notifications on.")
        elif text in ("status", "where", "winston", "?"):
            self._send_status()
        else:
            return  # ignore unrecognized messages

    def _send_status(self) -> None:
        parts = ["🐾 Animal Tracker status:"]
        parts.append(f"  Notifications: {'on' if self._enabled else 'off'}")
        if self._notifier:
            ms = self._notifier.mute_status()
            if ms["muted"]:
                parts.append(f"  Muted until: {ms['muted_until']}")
            else:
                parts.append("  Not muted")
        with self._lock:
            if self._journey:
                zones = [self._journey[0][0].from_zone or "?"]
                zones += [e.to_zone for e, _ in self._journey]
                parts.append(f"  Moving: {' → '.join(_zone_label(z) for z in zones)}")
            else:
                parts.append("  Settled (no active journey)")
        parts.append("\nCommands: mute | unmute | mute <min> | on | off | status")
        self._send_text("\n".join(parts))


def _zone_label(zone: str | None) -> str:
    return (zone or "unknown").replace("-", " ")


def sender_from_settings(d: dict[str, Any] | None, db: Database | None = None) -> Sender:
    d = d or {}
    backend = d.get("backend", "log")
    if backend == "pushover":
        p = d.get("pushover") or {}
        return PushoverSender(user_key_env=p.get("user_key_env", "PUSHOVER_USER_KEY"),
                              api_token_env=p.get("api_token_env", "PUSHOVER_API_TOKEN"))
    if backend == "apns":
        return APNsSender.from_settings(d.get("apns") or {}, db)
    if backend == "imessage":
        return IMessageSender.from_settings(d.get("imessage") or {})
    if backend != "log":
        raise ValueError(f"unknown notification backend '{backend}' (log | pushover | apns | imessage)")
    return LogSender()


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #

class NotificationService:
    """Glue: policy -> payload -> sender -> DB record."""

    def __init__(self, policy: NotificationPolicy, sender: Sender, db: Database | None = None) -> None:
        self.policy = policy
        self.sender = sender
        self.db = db
        if db is not None:
            self._restore_cooldowns()
            self.policy.muted_until = db.get_muted_until()
        # Give the iMessage sender a back-reference for mute commands.
        if isinstance(sender, IMessageSender):
            sender.set_notifier(self)
            sender.start_command_listener()

    def set_mute(self, minutes: int, now: datetime | None = None) -> dict[str, Any]:
        if not 0 <= minutes <= 1440:
            raise ValueError("minutes must be between 0 and 1440")
        now = parse_timestamp(now) if now is not None else utcnow()
        until = now + timedelta(minutes=minutes) if minutes else None
        # Persist first: a failed write must not claim the mute succeeded.
        if self.db is not None:
            self.db.set_muted_until(until)
        self.policy.muted_until = until
        return self.mute_status(now)

    def mute_status(self, now: datetime | None = None) -> dict[str, Any]:
        now = parse_timestamp(now) if now is not None else utcnow()
        until = self.policy.muted_until
        active = until is not None and now < until
        return {"muted": active, "muted_until": until.isoformat() if active else None,
                "scope": "all_devices", "as_of": now.isoformat()}

    def _restore_cooldowns(self) -> None:
        assert self.db is not None
        since = utcnow() - timedelta(seconds=self.policy.config.cooldown_seconds)
        for n in self.db.list_notifications(since=since):
            if n["type"] in (SUPPRESSED, SILENT):
                continue
            w = n["payload"].get("winston", {})
            if w.get("zone"):
                self.policy.seed_last_sent(w.get("from_zone"), w["zone"], n["sent_at"])

    def handle_transition(self, event: TransitionEvent, now: datetime | None = None) -> NotificationDecision:
        decision = self.policy.decide(event, now)
        payload = build_payload(event, decision)
        success, error = True, None
        if decision.should_send:
            try:
                self.sender.send(payload, decision)
            except Exception as e:  # never let a push failure break tracking
                success, error = False, str(e)
                log.error("notification failed (%s): %s", self.sender.name, e)
        if self.db is not None:
            self.db.insert_notification(
                transition_id=event.id, type=decision.type, payload=payload,
                backend=self.sender.name, success=success, error=error, sent_at=now,
            )
        return decision
