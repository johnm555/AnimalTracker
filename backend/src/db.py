"""SQLite persistence layer.

Tables
------
cameras           one row per Ring camera, mapped to a zone
zones             one row per zone in the topology
observations      every analyzed camera event (see observation.py)
transitions       zone changes emitted by the state machine
notifications     every push notification decision (sent or suppressed)
captures          bounded evidence captures (clip + frames) keyed by Ring event
processed_events  every Ring event the poller has handled, so restarts never
                  re-analyze (and re-bill) the same clip
device_tokens     APNs device tokens registered by the watch app
reference_images  enrolled photos of Winston

All timestamps are stored as ISO-8601 UTC strings so they sort correctly.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .observation import Observation, parse_timestamp, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS zones (
    id          TEXT PRIMARY KEY,
    description TEXT
);

CREATE TABLE IF NOT EXISTS cameras (
    id       TEXT PRIMARY KEY,
    zone_id  TEXT NOT NULL REFERENCES zones(id),
    ring_device_id TEXT,
    enabled  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS observations (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp                   TEXT NOT NULL,
    camera_id                   TEXT NOT NULL,
    ring_classification         TEXT,
    winston_probability         REAL NOT NULL,
    vision_similarity           REAL,
    vision_confidence           REAL,
    size_appearance_compatible  INTEGER,
    temporal_likelihood         REAL,
    animal_present              INTEGER,
    frames_analyzed             INTEGER NOT NULL DEFAULT 0,
    raw_response                TEXT,
    extra                       TEXT
);
CREATE INDEX IF NOT EXISTS idx_observations_ts ON observations(timestamp);
CREATE INDEX IF NOT EXISTS idx_observations_camera ON observations(camera_id, timestamp);

CREATE TABLE IF NOT EXISTS detection_runs (
    run_id TEXT PRIMARY KEY,
    reviewer TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'partial', 'failed')),
    events_reviewed INTEGER NOT NULL CHECK (events_reviewed >= 0),
    sheets_viewed INTEGER NOT NULL CHECK (sheets_viewed >= 0),
    wall_seconds REAL NOT NULL CHECK (wall_seconds >= 0),
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_detection_runs_finished ON detection_runs(finished_at);

CREATE TABLE IF NOT EXISTS animal_sightings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id       TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    species         TEXT NOT NULL,
    source          TEXT NOT NULL CHECK (source IN ('session', 'owner', 'backfill')),
    confidence      REAL,
    notes           TEXT NOT NULL DEFAULT '',
    observation_id  INTEGER REFERENCES observations(id),
    staging_key     TEXT,
    extra           TEXT NOT NULL DEFAULT '{}',
    recorded_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_animal_at ON animal_sightings(timestamp);
CREATE INDEX IF NOT EXISTS idx_animal_species ON animal_sightings(species);
CREATE UNIQUE INDEX IF NOT EXISTS idx_animal_obs ON animal_sightings(observation_id)
    WHERE observation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS local_audits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    staging_key     TEXT NOT NULL,
    device_id       TEXT NOT NULL,
    event_id        TEXT NOT NULL,
    camera_id       TEXT NOT NULL,
    event_at        TEXT NOT NULL,
    local_outcome   TEXT NOT NULL,
    local_score     REAL,
    observation_id  INTEGER REFERENCES observations(id),
    reviewer        TEXT NOT NULL,
    reviewer_label  TEXT NOT NULL CHECK (reviewer_label IN ('winston', 'not_winston', 'no_animal', 'uncertain')),
    agrees          INTEGER NOT NULL,
    notes           TEXT NOT NULL,
    requeued        INTEGER NOT NULL DEFAULT 0,
    audited_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_local_audits_at ON local_audits(audited_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_local_audits_key ON local_audits(staging_key, reviewer);

CREATE TABLE IF NOT EXISTS observation_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL REFERENCES observations(id),
    label TEXT NOT NULL CHECK (label IN ('winston', 'not_winston', 'uncertain')),
    reviewer TEXT NOT NULL,
    notes TEXT NOT NULL,
    reviewed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_observation ON observation_reviews(observation_id, id);

CREATE TABLE IF NOT EXISTS transitions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    from_zone       TEXT,
    to_zone         TEXT NOT NULL,
    departed_at     TEXT,
    arrived_at      TEXT NOT NULL,
    confidence      REAL NOT NULL,
    observation_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_transitions_arrived ON transitions(arrived_at);

CREATE TABLE IF NOT EXISTS notifications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    transition_id INTEGER REFERENCES transitions(id),
    type          TEXT NOT NULL CHECK (type IN ('normal', 'high_priority', 'silent', 'suppressed')),
    sent_at       TEXT NOT NULL,
    payload       TEXT NOT NULL,
    backend       TEXT,
    success       INTEGER NOT NULL DEFAULT 1,
    error         TEXT
);

CREATE TABLE IF NOT EXISTS captures (
    device_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    camera_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'captured', 'failed')),
    clip_path TEXT,
    frame_paths TEXT NOT NULL DEFAULT '[]',
    error TEXT,
    PRIMARY KEY(device_id, event_id)
);

CREATE TABLE IF NOT EXISTS processed_events (
    device_id      TEXT NOT NULL,
    event_id       TEXT NOT NULL,
    camera_id      TEXT NOT NULL,
    timestamp      TEXT NOT NULL,
    status         TEXT NOT NULL CHECK(status IN ('staged', 'analyzed', 'skipped', 'failed')),
    observation_id INTEGER REFERENCES observations(id),
    error          TEXT,
    attempts       INTEGER NOT NULL DEFAULT 1,
    processed_at   TEXT NOT NULL,
    PRIMARY KEY(device_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_processed_events_ts ON processed_events(timestamp);

CREATE TABLE IF NOT EXISTS device_tokens (
    token         TEXT PRIMARY KEY,
    platform      TEXT NOT NULL DEFAULT 'watchos',
    name          TEXT,
    registered_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS notification_preferences (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    muted_until TEXT
);

CREATE TABLE IF NOT EXISTS reference_images (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT NOT NULL UNIQUE,
    label       TEXT,
    added_at    TEXT NOT NULL,
    sha256      TEXT
);
"""


def _iso(dt: datetime | None) -> str | None:
    return parse_timestamp(dt).isoformat() if dt is not None else None


class Database:
    """Thin wrapper around sqlite3 with typed helpers for each table."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        # check_same_thread=False so FastAPI's threadpool can share the handle;
        # we serialize access with a per-connection lock via isolation_level=None
        # + explicit transactions in `transaction()`.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self.init_schema()

    # -- lifecycle ---------------------------------------------------------

    def init_schema(self) -> None:
        with self.transaction():
            self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Small forward-only migrations for DBs created by earlier versions."""
        row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='processed_events'").fetchone()
        if row and "'staged'" not in row["sql"]:
            # v1 ledger lacked the 'staged' status used by session-mode detection.
            with self.transaction() as c:
                c.execute("ALTER TABLE processed_events RENAME TO processed_events_v1")
                c.executescript(SCHEMA)
                c.execute("INSERT INTO processed_events SELECT * FROM processed_events_v1")
                c.execute("DROP TABLE processed_events_v1")

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # -- zones & cameras ---------------------------------------------------

    def upsert_zone(self, zone_id: str, description: str | None = None) -> None:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO zones(id, description) VALUES(?, ?) "
                "ON CONFLICT(id) DO UPDATE SET description=excluded.description",
                (zone_id, description),
            )

    def upsert_camera(self, camera_id: str, zone_id: str, ring_device_id: str | None = None) -> None:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO cameras(id, zone_id, ring_device_id) VALUES(?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET zone_id=excluded.zone_id, "
                "ring_device_id=COALESCE(excluded.ring_device_id, cameras.ring_device_id)",
                (camera_id, zone_id, ring_device_id),
            )

    def sync_topology(self, topology: Any) -> None:
        """Mirror a Topology (state_machine.Topology) into the zones/cameras tables."""
        for zone in topology.zones.values():
            self.upsert_zone(zone.id, zone.description)
            for cam in zone.cameras:
                self.upsert_camera(cam, zone.id)

    def list_cameras(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM cameras ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def list_zones(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM zones ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def get_capture(self, device_id: str, event_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM captures WHERE device_id=? AND event_id=?",
            (device_id, event_id),
        ).fetchone()
        return dict(row) if row else None

    def save_capture(self, event: Any, status: str, clip_path: str | None = None,
                     frame_paths: list[str] | None = None, error: str | None = None) -> None:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO captures(device_id,event_id,camera_id,timestamp,status,clip_path,frame_paths,error) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(device_id,event_id) DO UPDATE SET "
                "status=excluded.status,clip_path=excluded.clip_path,frame_paths=excluded.frame_paths,error=excluded.error",
                (event.device_id, event.event_id, event.camera_id, event.timestamp.isoformat(),
                 status, clip_path, json.dumps(frame_paths or []), error),
            )

    # -- observations ------------------------------------------------------

    def insert_observation(self, obs: Observation) -> int:
        with self.transaction() as c:
            cur = c.execute(
                """INSERT INTO observations(
                       timestamp, camera_id, ring_classification, winston_probability,
                       vision_similarity, vision_confidence, size_appearance_compatible,
                       temporal_likelihood, animal_present, frames_analyzed, raw_response, extra)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    obs.timestamp.isoformat(),
                    obs.camera_id,
                    obs.ring_classification,
                    obs.winston_probability,
                    obs.vision_similarity,
                    obs.vision_confidence,
                    _bool(obs.size_appearance_compatible),
                    obs.temporal_likelihood,
                    _bool(obs.animal_present),
                    obs.frames_analyzed,
                    obs.raw_response,
                    json.dumps(obs.extra) if obs.extra else None,
                ),
            )
            obs.id = int(cur.lastrowid)
            return obs.id

    def iter_observations_in_ingestion_order(self) -> Iterator[Observation]:
        """Stream evidence in its original processing order, including rejections."""
        cursor = self._conn.execute("SELECT * FROM observations ORDER BY id")
        try:
            for row in cursor:
                yield _row_to_observation(row)
        finally:
            cursor.close()

    def iter_calibration_observations(self) -> Iterator[Observation]:
        """Latest committed verdict per Ring device/event, in capture-time order.

        Audit/restart history remains append-only. A requeue or skip without a
        replacement observation cannot hide the previous verdict. Missing Ring
        identity means an independent observation, not one shared NULL group.
        """
        cursor = self._conn.execute("""
            WITH identified AS (
                SELECT *,
                    NULLIF(CAST(json_extract(extra, '$.ring_device_id') AS TEXT), '') AS device_key,
                    NULLIF(CAST(json_extract(extra, '$.ring_event_id') AS TEXT), '') AS event_key
                FROM observations
            ), ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY device_key, event_key,
                        CASE WHEN device_key IS NULL OR event_key IS NULL THEN id END
                    ORDER BY id DESC
                ) AS revision_rank FROM identified
            )
            SELECT observations.* FROM observations JOIN ranked USING (id)
            WHERE revision_rank = 1 ORDER BY observations.timestamp, observations.id
        """)
        try:
            for row in cursor:
                yield _row_to_observation(row)
        finally:
            cursor.close()

    def get_observation(self, obs_id: int) -> Observation | None:
        row = self._conn.execute("SELECT * FROM observations WHERE id=?", (obs_id,)).fetchone()
        return _row_to_observation(row) if row else None

    def list_observations(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        camera_id: str | None = None,
        min_probability: float | None = None,
        limit: int = 1000,
    ) -> list[Observation]:
        clauses, params = [], []
        if since is not None:
            clauses.append("timestamp >= ?"); params.append(_iso(since))
        if until is not None:
            clauses.append("timestamp < ?"); params.append(_iso(until))
        if camera_id is not None:
            clauses.append("camera_id = ?"); params.append(camera_id)
        if min_probability is not None:
            clauses.append("winston_probability >= ?"); params.append(min_probability)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM observations {where} ORDER BY timestamp ASC LIMIT ?", (*params, limit)
        ).fetchall()
        return [_row_to_observation(r) for r in rows]

    def record_detection_run(self, data: dict[str, Any]) -> dict[str, Any]:
        existing = self._conn.execute("SELECT * FROM detection_runs WHERE run_id=?",
                                      (data["run_id"],)).fetchone()
        if existing is not None:
            row = dict(existing)
            if any(row[key] != value for key, value in data.items()):
                raise ValueError("run_id already recorded with different metrics")
            return row
        row = {**data, "recorded_at": utcnow().isoformat()}
        with self.transaction() as c:
            c.execute("""INSERT INTO detection_runs
                (run_id, reviewer, started_at, finished_at, status, events_reviewed,
                 sheets_viewed, wall_seconds, recorded_at)
                VALUES (:run_id, :reviewer, :started_at, :finished_at, :status,
                        :events_reviewed, :sheets_viewed, :wall_seconds, :recorded_at)""", row)
        return row

    def detection_runs_report(self, since: datetime, until: datetime, limit: int = 100) -> dict[str, Any]:
        params = (_iso(since), _iso(until))
        rows = self._conn.execute("""SELECT * FROM detection_runs
            WHERE finished_at >= ? AND finished_at < ? ORDER BY finished_at DESC, run_id LIMIT ?""",
            (*params, limit)).fetchall()
        summary = dict(self._conn.execute("""SELECT COUNT(*) AS runs,
            COALESCE(SUM(events_reviewed), 0) AS events_reviewed,
            COALESCE(SUM(sheets_viewed), 0) AS sheets_viewed,
            COALESCE(SUM(wall_seconds), 0) AS wall_seconds,
            COALESCE(SUM(status = 'failed'), 0) AS failed_runs,
            COALESCE(SUM(status = 'partial'), 0) AS partial_runs
            FROM detection_runs WHERE finished_at >= ? AND finished_at < ?""", params).fetchone())
        return {"since": params[0], "until": params[1], "summary": summary,
                "runs": [dict(row) for row in rows], "truncated": summary["runs"] > len(rows),
                "provenance": "self-reported session metrics; not token usage or subscription billing"}

    def insert_observation_review(self, observation_id: int, label: str,
                                  reviewer: str, notes: str) -> dict[str, Any]:
        if label not in {"winston", "not_winston", "uncertain"}:
            raise ValueError("invalid review label")
        if not reviewer.strip() or not notes.strip():
            raise ValueError("reviewer and evidence notes are required")
        at = utcnow().isoformat()
        with self.transaction() as c:
            cur = c.execute("""INSERT INTO observation_reviews
                (observation_id, label, reviewer, notes, reviewed_at) VALUES (?, ?, ?, ?, ?)""",
                (observation_id, label, reviewer.strip(), notes.strip(), at))
            return {"id": cur.lastrowid, "observation_id": observation_id, "label": label,
                    "reviewer": reviewer.strip(), "notes": notes.strip(), "reviewed_at": at}

    # -- visiting animals (P4-30) -----------------------------------------

    def record_animal_sighting(self, sighting: Any) -> dict[str, Any]:
        """Store one visiting-animal sighting. Never touches the tracker."""
        d = sighting.to_dict()
        at = utcnow().isoformat()
        with self.transaction() as c:
            cur = c.execute(
                """INSERT INTO animal_sightings(camera_id,timestamp,species,source,confidence,
                       notes,observation_id,staging_key,extra,recorded_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(observation_id) WHERE observation_id IS NOT NULL DO UPDATE SET
                       species=excluded.species, source=excluded.source,
                       confidence=excluded.confidence, notes=excluded.notes,
                       extra=excluded.extra, recorded_at=excluded.recorded_at""",
                (d["camera_id"], d["timestamp"], d["species"], d["source"],
                 d["confidence"], d["notes"], d["observation_id"], d["staging_key"],
                 json.dumps(d["extra"]), at))
            return {"id": cur.lastrowid, **d, "recorded_at": at}

    def list_animal_sightings(self, since: datetime | None = None, species: str | None = None,
                              limit: int = 500) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM animal_sightings", []
        where = []
        if since is not None:
            where.append("timestamp >= ?"); args.append(_iso(since))
        if species:
            where.append("species = ?"); args.append(species)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY timestamp DESC LIMIT ?"; args.append(limit)
        return [dict(r) for r in self._conn.execute(q, args)]

    def unlabelled_animal_observations(self, threshold: float = 0.70,
                                       limit: int = 200) -> list[dict[str, Any]]:
        """Observations that saw *an* animal that was not Winston, with no species yet.

        These are the backfill candidates. The species is genuinely unknown —
        the reviewer said what it was not, not what it was — so this only
        surfaces them for labelling and never guesses.
        """
        rows = self._conn.execute(
            """SELECT o.id, o.camera_id, o.timestamp, o.winston_probability, o.extra
                 FROM observations o
                 LEFT JOIN animal_sightings a ON a.observation_id = o.id
                WHERE o.animal_present = 1 AND o.winston_probability < ?
                  AND a.id IS NULL
                ORDER BY o.id DESC LIMIT ?""", (threshold, limit)).fetchall()
        out = []
        for r in rows:
            extra = json.loads(r["extra"] or "{}")
            out.append({"observation_id": r["id"], "camera_id": r["camera_id"],
                        "timestamp": r["timestamp"],
                        "winston_probability": r["winston_probability"],
                        "mismatched_features": extra.get("mismatched_features") or [],
                        "reasoning": (extra.get("reasoning") or "")[:300]})
        return out

    CONFIRMED_PROBABILITY = 0.95
    """What an owner-confirmed sighting is worth. Above `strong_threshold`, so it
    confirms a move on its own rather than waiting for a second sighting."""

    def apply_owner_confirmation(self, observation_id: int, label: str, reviewer: str,
                                 notes: str) -> dict[str, Any]:
        """Correct one observation from ground truth the owner supplied.

        This is the only path that overwrites a stored `winston_probability`, and
        it is not a model output, so it records what it replaced. The original
        values are kept in `extra.owner_confirmation` and a matching
        `observation_reviews` row is written, so the amendment stays visible to
        calibration and can never be mistaken for the vision layer's own answer.

        Re-confirming an already-confirmed observation keeps the *first*
        originals — the pre-confirmation values, not the confirmed ones.
        """
        if label not in ("winston", "not_winston"):
            raise ValueError("label must be 'winston' or 'not_winston'")
        if not reviewer.strip() or not notes.strip():
            raise ValueError("reviewer and notes are required")
        row = self._conn.execute(
            "SELECT winston_probability, vision_confidence, extra FROM observations WHERE id=?",
            (observation_id,)).fetchone()
        if row is None:
            raise KeyError(f"no observation {observation_id}")
        extra = json.loads(row["extra"] or "{}")
        prior = extra.get("owner_confirmation") or {}
        prob = self.CONFIRMED_PROBABILITY if label == "winston" else 0.0
        at = utcnow().isoformat()
        extra["owner_confirmation"] = {
            "label": label, "reviewer": reviewer.strip(), "notes": notes.strip(), "at": at,
            "original_winston_probability": prior.get("original_winston_probability",
                                                      row["winston_probability"]),
            "original_vision_confidence": prior.get("original_vision_confidence",
                                                    row["vision_confidence"]),
        }
        with self.transaction() as c:
            c.execute("UPDATE observations SET winston_probability=?, vision_confidence=?, extra=? "
                      "WHERE id=?", (prob, prob, json.dumps(extra), observation_id))
        self.insert_observation_review(observation_id=observation_id, label=label,
                                       reviewer=reviewer, notes=notes)
        return {"observation_id": observation_id, "label": label,
                "winston_probability": prob,
                "was": extra["owner_confirmation"]["original_winston_probability"],
                "confirmed_at": at}

    # -- local-model audits (P4-29) ---------------------------------------

    AUDIT_AGREEMENT = {
        # local outcome -> the reviewer labels that agree with it
        "winston": {"winston"},
        "no_animal": {"no_animal"},
        "not_winston": {"not_winston"},
        "review": {"winston", "not_winston", "no_animal", "uncertain"},
    }

    def insert_local_audit(self, *, staging_key: str, device_id: str, event_id: str,
                           camera_id: str, event_at: str, local_outcome: str,
                           local_score: float | None, reviewer_label: str, reviewer: str,
                           notes: str, observation_id: int | None = None,
                           requeued: bool = False) -> dict[str, Any]:
        """Record one spot-check of a local verdict.

        `agrees` is derived, not supplied: the reviewer states what they saw and
        the comparison is made here, so a disagreement cannot be recorded as
        agreement by accident.
        """
        if not reviewer.strip() or not notes.strip():
            raise ValueError("reviewer and notes are required")
        if local_outcome not in self.AUDIT_AGREEMENT:
            raise ValueError(f"unknown local outcome {local_outcome!r}")
        agrees = reviewer_label in self.AUDIT_AGREEMENT[local_outcome]
        at = utcnow().isoformat()
        with self.transaction() as c:
            cur = c.execute(
                """INSERT INTO local_audits(staging_key, device_id, event_id, camera_id, event_at,
                       local_outcome, local_score, observation_id, reviewer, reviewer_label,
                       agrees, notes, requeued, audited_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(staging_key, reviewer) DO UPDATE SET
                       reviewer_label=excluded.reviewer_label, agrees=excluded.agrees,
                       notes=excluded.notes, requeued=excluded.requeued, audited_at=excluded.audited_at""",
                (staging_key, device_id, event_id, camera_id, event_at, local_outcome, local_score,
                 observation_id, reviewer.strip(), reviewer_label, 1 if agrees else 0,
                 notes.strip(), 1 if requeued else 0, at))
            return {"id": cur.lastrowid, "staging_key": staging_key, "agrees": agrees,
                    "local_outcome": local_outcome, "reviewer_label": reviewer_label,
                    "audited_at": at}

    def audited_keys(self, since: datetime | None = None) -> set[str]:
        q, args = "SELECT staging_key FROM local_audits", []
        if since is not None:
            q += " WHERE audited_at >= ?"; args.append(_iso(since))
        return {r["staging_key"] for r in self._conn.execute(q, args)}

    def list_local_audits(self, since: datetime | None = None, disagreements_only: bool = False,
                          limit: int = 200) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM local_audits", []
        where = []
        if since is not None:
            where.append("audited_at >= ?"); args.append(_iso(since))
        if disagreements_only:
            where.append("agrees = 0")
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY audited_at DESC LIMIT ?"; args.append(limit)
        return [dict(r) for r in self._conn.execute(q, args)]

    def local_audit_summary(self, since: datetime | None = None) -> dict[str, Any]:
        """Agreement rate overall and per local outcome — the drift signal."""
        rows = self.list_local_audits(since=since, limit=100000)
        out: dict[str, Any] = {"audited": len(rows), "agreed": sum(r["agrees"] for r in rows),
                               "by_outcome": {}}
        for r in rows:
            b = out["by_outcome"].setdefault(r["local_outcome"], {"n": 0, "agreed": 0, "disagreed": []})
            b["n"] += 1
            if r["agrees"]:
                b["agreed"] += 1
            else:
                b["disagreed"].append({"key": r["staging_key"], "camera_id": r["camera_id"],
                                       "score": r["local_score"], "said": r["reviewer_label"]})
        out["agreement_rate"] = round(out["agreed"] / len(rows), 4) if rows else None
        for b in out["by_outcome"].values():
            b["agreement_rate"] = round(b["agreed"] / b["n"], 4) if b["n"] else None
        return out

    def list_observation_reviews(self, observation_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self._conn.execute(
            "SELECT * FROM observation_reviews WHERE observation_id = ? ORDER BY id", (observation_id,))]

    def latest_observation_reviews(self) -> dict[int, dict[str, Any]]:
        rows = self._conn.execute("""SELECT r.* FROM observation_reviews r JOIN
            (SELECT observation_id, MAX(id) AS id FROM observation_reviews GROUP BY observation_id) latest
            ON r.id = latest.id""")
        return {row["observation_id"]: dict(row) for row in rows}

    def latest_observation(self, min_probability: float = 0.0) -> Observation | None:
        row = self._conn.execute(
            "SELECT * FROM observations WHERE winston_probability >= ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (min_probability,),
        ).fetchone()
        return _row_to_observation(row) if row else None

    # -- transitions -------------------------------------------------------

    def insert_transition(
        self,
        to_zone: str,
        arrived_at: datetime,
        confidence: float,
        from_zone: str | None = None,
        departed_at: datetime | None = None,
        observation_ids: list[int] | None = None,
    ) -> int:
        with self.transaction() as c:
            cur = c.execute(
                """INSERT INTO transitions(from_zone, to_zone, departed_at, arrived_at,
                                           confidence, observation_ids)
                   VALUES(?,?,?,?,?,?)""",
                (
                    from_zone,
                    to_zone,
                    _iso(departed_at),
                    _iso(arrived_at),
                    float(confidence),
                    json.dumps(observation_ids or []),
                ),
            )
            return int(cur.lastrowid)

    def get_transition(self, transition_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM transitions WHERE id=?", (transition_id,)).fetchone()
        return _row_to_transition(row) if row else None

    def latest_transition(self) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM transitions ORDER BY arrived_at DESC, id DESC LIMIT 1"
        ).fetchone()
        return _row_to_transition(row) if row else None

    def list_transitions(
        self,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if since is not None:
            clauses.append("arrived_at >= ?"); params.append(_iso(since))
        if until is not None:
            clauses.append("arrived_at < ?"); params.append(_iso(until))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM transitions {where} ORDER BY arrived_at ASC, id ASC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [_row_to_transition(r) for r in rows]

    def transitions_for_day(self, day: datetime) -> list[dict[str, Any]]:
        start = parse_timestamp(day).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.list_transitions(since=start, until=start + timedelta(days=1))

    def get_muted_until(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT muted_until FROM notification_preferences WHERE id = 1").fetchone()
        return parse_timestamp(row["muted_until"]) if row and row["muted_until"] else None

    def set_muted_until(self, until: datetime | None) -> None:
        with self.transaction() as c:
            c.execute("""INSERT INTO notification_preferences(id, muted_until) VALUES(1, ?)
                         ON CONFLICT(id) DO UPDATE SET muted_until = excluded.muted_until""",
                      (_iso(until),))

    # -- notifications -----------------------------------------------------

    def insert_notification(
        self,
        transition_id: int | None,
        type: str,
        payload: dict[str, Any],
        backend: str | None = None,
        success: bool = True,
        error: str | None = None,
        sent_at: datetime | None = None,
    ) -> int:
        with self.transaction() as c:
            cur = c.execute(
                """INSERT INTO notifications(transition_id, type, sent_at, payload, backend, success, error)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    transition_id,
                    type,
                    _iso(sent_at or utcnow()),
                    json.dumps(payload),
                    backend,
                    1 if success else 0,
                    error,
                ),
            )
            return int(cur.lastrowid)

    def list_notifications(self, since: datetime | None = None, limit: int = 500) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if since is not None:
            where = "WHERE sent_at >= ?"
            params.append(_iso(since))
        rows = self._conn.execute(
            f"SELECT * FROM notifications {where} ORDER BY sent_at ASC LIMIT ?", (*params, limit)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            d["success"] = bool(d["success"])
            out.append(d)
        return out

    def last_notification_for(self, from_zone: str | None, to_zone: str) -> dict[str, Any] | None:
        """Most recent *sent* notification for a given (from -> to) pair, for cooldown checks."""
        row = self._conn.execute(
            """SELECT n.* FROM notifications n
               JOIN transitions t ON t.id = n.transition_id
               WHERE t.to_zone = ? AND (t.from_zone IS ? OR t.from_zone = ?)
                 AND n.type != 'suppressed'
               ORDER BY n.sent_at DESC LIMIT 1""",
            (to_zone, from_zone, from_zone),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        return d

    # -- processed events --------------------------------------------------

    def event_processed(self, device_id: str, event_id: str) -> bool:
        """True once the poller is done with an event: analyzed, skipped, or handed to a session (staged)."""
        row = self._conn.execute(
            "SELECT 1 FROM processed_events WHERE device_id=? AND event_id=? AND status IN ('staged','analyzed','skipped')",
            (device_id, event_id),
        ).fetchone()
        return row is not None

    def mark_event_by_id(self, device_id: str, event_id: str, status: str,
                         observation_id: int | None = None, error: str | None = None) -> bool:
        """Update an existing ledger row (session recorded a verdict for a staged event).

        Returns True if a row was updated. When another process (the API)
        inserted the observation moments ago, this connection's read snapshot
        can lag behind and the FOREIGN KEY check fails; a rollback refreshes
        the snapshot, so retry briefly before giving up.
        """
        import time as _time

        for attempt in range(5):
            try:
                with self.transaction() as c:
                    cur = c.execute(
                        "UPDATE processed_events SET status=?, observation_id=?, error=?, processed_at=? "
                        "WHERE device_id=? AND event_id=?",
                        (status, observation_id, error, utcnow().isoformat(), device_id, event_id),
                    )
                    return cur.rowcount > 0
            except sqlite3.IntegrityError:
                if observation_id is None or attempt == 4:
                    raise
                _time.sleep(0.1 * (attempt + 1))
        return False

    def get_event(self, device_id: str, event_id: str) -> dict[str, Any] | None:
        """One ledger row, or None if the poller has never seen this event."""
        row = self._conn.execute(
            "SELECT * FROM processed_events WHERE device_id=? AND event_id=?",
            (device_id, event_id),
        ).fetchone()
        return dict(row) if row else None

    def requeue_event(self, device_id: str, event_id: str) -> int | None:
        """Reset a ledger row to 'staged' so a session can re-record its verdict.

        Returns the observation_id the row carried (None if it was skipped or
        never analyzed). The observation itself is left in place: re-recording
        inserts a new row, and deciding what to do with the superseded one is a
        calibration question, not a bookkeeping one.
        """
        row = self.get_event(device_id, event_id)
        if row is None:
            return None
        with self.transaction() as c:
            c.execute(
                "UPDATE processed_events SET status='staged', observation_id=NULL, error=NULL, processed_at=? "
                "WHERE device_id=? AND event_id=?",
                (utcnow().isoformat(), device_id, event_id),
            )
        return row["observation_id"]

    def count_events_by_status(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM processed_events GROUP BY status").fetchall()
        return {r["status"]: int(r["n"]) for r in rows}

    def mark_event(self, event: Any, status: str, observation_id: int | None = None,
                   error: str | None = None) -> None:
        """Record the outcome of one Ring event. 'failed' rows are retried next poll."""
        with self.transaction() as c:
            c.execute(
                "INSERT INTO processed_events(device_id,event_id,camera_id,timestamp,status,observation_id,error,attempts,processed_at) "
                "VALUES(?,?,?,?,?,?,?,1,?) ON CONFLICT(device_id,event_id) DO UPDATE SET "
                "status=excluded.status,observation_id=excluded.observation_id,error=excluded.error,"
                "attempts=processed_events.attempts+1,processed_at=excluded.processed_at",
                (event.device_id, event.event_id, event.camera_id, event.timestamp.isoformat(),
                 status, observation_id, error, utcnow().isoformat()),
            )

    def event_attempts(self, device_id: str, event_id: str) -> int:
        """How many times the poller has tried this event (0 if never seen)."""
        row = self._conn.execute(
            "SELECT attempts FROM processed_events WHERE device_id=? AND event_id=?",
            (device_id, event_id),
        ).fetchone()
        return int(row["attempts"]) if row else 0

    def latest_processed_timestamp(self) -> datetime | None:
        row = self._conn.execute("SELECT MAX(timestamp) AS ts FROM processed_events").fetchone()
        return parse_timestamp(row["ts"]) if row and row["ts"] else None

    def list_processed_events(self, since: datetime | None = None, limit: int = 200) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM processed_events", []
        if since is not None:
            q += " WHERE timestamp >= ?"; args.append(_iso(since))
        q += " ORDER BY timestamp DESC LIMIT ?"; args.append(limit)
        return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    # -- device tokens -----------------------------------------------------

    def register_device_token(self, token: str, platform: str = "watchos", name: str | None = None) -> None:
        now = utcnow().isoformat()
        with self.transaction() as c:
            c.execute(
                "INSERT INTO device_tokens(token,platform,name,registered_at,last_seen_at,enabled) "
                "VALUES(?,?,?,?,?,1) ON CONFLICT(token) DO UPDATE SET platform=excluded.platform,"
                "name=COALESCE(excluded.name, device_tokens.name),last_seen_at=excluded.last_seen_at,enabled=1",
                (token, platform, name, now, now),
            )

    def remove_device_token(self, token: str) -> bool:
        with self.transaction() as c:
            cur = c.execute("DELETE FROM device_tokens WHERE token=?", (token,))
            return cur.rowcount > 0

    def disable_device_token(self, token: str) -> None:
        """APNs said the token is gone (410) — stop sending without losing the record."""
        with self.transaction() as c:
            c.execute("UPDATE device_tokens SET enabled=0 WHERE token=?", (token,))

    def list_device_tokens(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        q = "SELECT * FROM device_tokens" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY registered_at"
        return [dict(r) for r in self._conn.execute(q).fetchall()]

    # -- reference images --------------------------------------------------

    def add_reference_image(self, path: str | Path, label: str | None = None, sha256: str | None = None) -> int:
        with self.transaction() as c:
            cur = c.execute(
                "INSERT INTO reference_images(path, label, added_at, sha256) VALUES(?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET label=excluded.label, sha256=excluded.sha256",
                (str(path), label, utcnow().isoformat(), sha256),
            )
            if cur.lastrowid:
                return int(cur.lastrowid)
            row = c.execute("SELECT id FROM reference_images WHERE path=?", (str(path),)).fetchone()
            return int(row["id"])

    def list_reference_images(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM reference_images ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def remove_reference_image(self, path: str | Path) -> None:
        with self.transaction() as c:
            c.execute("DELETE FROM reference_images WHERE path=?", (str(path),))


# -- row mappers -------------------------------------------------------------

def _bool(v: bool | None) -> int | None:
    return None if v is None else (1 if v else 0)


def _row_to_observation(row: sqlite3.Row) -> Observation:
    d = dict(row)
    extra = d.pop("extra", None)
    d["extra"] = json.loads(extra) if extra else {}
    for key in ("size_appearance_compatible", "animal_present"):
        if d.get(key) is not None:
            d[key] = bool(d[key])
    return Observation(**d)


def _row_to_transition(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["observation_ids"] = json.loads(d["observation_ids"] or "[]")
    return d
