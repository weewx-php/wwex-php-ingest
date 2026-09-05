"""One durable SQLite journal per station; separate reader/writer connections."""

import json
import os
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from itertools import zip_longest
from pathlib import Path

from .protocol import encode


class SpoolError(RuntimeError):
    pass


class SpoolFull(SpoolError):
    pass


class Spool:
    def __init__(self, path, collector_id, driver_module, station, *, station_id=None):
        self.path, self.station = Path(path), station
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.execute("PRAGMA journal_size_limit=4194304")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    timestamp INTEGER NOT NULL,
                    payload BLOB NOT NULL,
                    size INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'queued',
                    reason TEXT
                );
                CREATE INDEX IF NOT EXISTS events_due ON events(state, next_attempt);
                CREATE INDEX IF NOT EXISTS events_time ON events(timestamp);
                CREATE TABLE IF NOT EXISTS usage (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    events INTEGER NOT NULL, bytes INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO usage VALUES (1, 0, 0);
                CREATE TRIGGER IF NOT EXISTS count_insert AFTER INSERT ON events BEGIN
                    UPDATE usage SET events=events+1, bytes=bytes+NEW.size WHERE id=1;
                END;
                CREATE TRIGGER IF NOT EXISTS count_delete AFTER DELETE ON events BEGIN
                    UPDATE usage SET events=events-1, bytes=bytes-OLD.size WHERE id=1;
                END;
            """)
            with self.transaction():
                identity = self.get_meta("collector_id")
                if identity is None:
                    self.set_meta("collector_id", collector_id)
                    self.set_meta("station_id", station_id or str(uuid.uuid4()))
                    self.set_meta("driver_module", driver_module)
                    self.set_meta("schema", 1)
                elif (
                    identity != collector_id
                    or self.get_meta("driver_module") != driver_module
                    or self.get_meta("schema") != 1
                    or (station_id is not None and self.get_meta("station_id") != station_id)
                ):
                    raise SpoolError("spool_identity_mismatch")
            self.station_id = self.get_meta("station_id")
            if os.name != "nt":
                os.chmod(self.path, 0o600)
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get_meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        self.db.execute(
            "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def can_fit(self, size=0):
        usage = self.db.execute("SELECT * FROM usage WHERE id=1").fetchone()
        return (
            usage["events"] < self.station.max_events
            and usage["bytes"] + size <= self.station.max_bytes
            and shutil.disk_usage(self.path.parent).free >= self.station.min_free_bytes + size
        )

    def append(self, event, now=None, *, metadata=None):
        payload = encode(event)
        if event["station_id"] != self.station_id or event["driver_module"] != self.get_meta(
            "driver_module"
        ):
            raise SpoolError("event_identity_mismatch")
        with self.transaction():
            if not self.can_fit(len(payload)):
                raise SpoolFull("spool_full")
            self.db.execute(
                "INSERT INTO events(event_id,timestamp,payload,size) VALUES (?,?,?,?)",
                (event["event_id"], event["dateTime"], payload, len(payload)),
            )
            if event["kind"] == "archive":
                # The record owns its duration; polling and upload cadence are independent.
                self.set_meta("hardware_last_interval", event["interval"] * 60)
                # Cursor and payload commit together, independently of upload acknowledgement.
                self.set_meta(
                    "hardware_cursor", max(self.get_meta("hardware_cursor", 0), event["dateTime"])
                )
            self.set_meta("last_collected", time.time() if now is None else now)
            self.set_meta("collection_state", "running")
            self.set_meta("collection_error", None)
            for key, value in (metadata or {}).items():
                self.set_meta(key, value)
        return event["event_id"]

    def candidates(self, now, limit, newest_first=False):
        # Alternate oldest and newest; neither a long backlog nor a busy live feed can starve.
        if now < self.get_meta("station_upload_after", 0):
            return []
        oldest = self.db.execute(
            "SELECT * FROM events WHERE state='queued' AND next_attempt<=? ORDER BY seq LIMIT ?",
            (now, limit),
        ).fetchall()
        newest = self.db.execute(
            "SELECT * FROM events WHERE state='queued' AND next_attempt<=? "
            "ORDER BY seq DESC LIMIT ?",
            (now, limit),
        ).fetchall()
        seen, result = set(), []
        if newest_first:
            oldest, newest = newest, oldest
        # A writer may commit between these reads. A shorter first result is harmless.
        for pair in zip_longest(oldest, newest):
            for row in pair:
                if row is not None and row["event_id"] not in seen:
                    seen.add(row["event_id"])
                    result.append(dict(row))
                    if len(result) == limit:
                        return result
        return result

    def acknowledge(self, event_id, now):
        with self.transaction():
            self.db.execute("DELETE FROM events WHERE event_id=?", (event_id,))
            self.set_meta("last_success", now)
            self.set_meta("upload_error", None)

    def defer(self, event_id, reason, until, quarantine=False):
        with self.transaction():
            self.db.execute(
                "UPDATE events SET attempts=attempts+1, next_attempt=?, state=?, reason=? "
                "WHERE event_id=?",
                (until, "quarantined" if quarantine else "queued", reason, event_id),
            )
            self.set_meta("upload_error", reason)

    def retry_quarantined(self):
        with self.transaction():
            count = self.db.execute(
                "UPDATE events SET state='queued',next_attempt=0,attempts=0,"
                "reason=NULL WHERE state='quarantined'"
            ).rowcount
            self.set_meta("station_upload_after", 0)
        return count

    def status(self, max_age_seconds=432000):
        usage = dict(self.db.execute("SELECT events,bytes FROM usage WHERE id=1").fetchone())
        usage.update(
            {
                "key": self.station.key,
                "label": self.station.label,
                "station_id": self.station_id,
                "driver_module": self.get_meta("driver_module"),
                "oldest_unconfirmed": self.db.execute(
                    "SELECT MIN(timestamp) FROM events"
                ).fetchone()[0],
                "quarantined": self.db.execute(
                    "SELECT COUNT(*) FROM events WHERE state='quarantined'"
                ).fetchone()[0],
                "rejections": {
                    r[0]: r[1]
                    for r in self.db.execute(
                        "SELECT reason,COUNT(*) FROM events WHERE reason IS NOT NULL "
                        "GROUP BY reason"
                    )
                },
                "max_events": self.station.max_events,
                "max_bytes": self.station.max_bytes,
                "full": not self.can_fit(self.get_meta("blocked_bytes", 0)),
            }
        )
        for key in (
            "last_collected",
            "last_success",
            "collection_state",
            "collection_error",
            "upload_error",
            "upload_next_attempt",
            "worker_pid",
            "worker_restarts",
            "record_generation",
            "hardware_state",
            "hardware_error",
            "hardware_cursor",
            "hardware_interval",
            "hardware_last_interval",
            "source",
        ):
            usage[key] = self.get_meta(key)
        age_limit = self.get_meta("receiver_max_age_seconds") or max_age_seconds
        usage["max_age_seconds"] = age_limit
        usage["beyond_receiver_age"] = self.db.execute(
            "SELECT COUNT(*) FROM events WHERE timestamp<?", (time.time() - age_limit,)
        ).fetchone()[0]
        usage["station_upload_after"] = self.get_meta("station_upload_after", 0)
        return usage


def spool_path(config, station):
    return config.state_dir / "stations" / f"{station.key}.sqlite3"
