import dataclasses
import json
import sqlite3

import pytest
from conftest import packet

from weewx_php_ingest.spool import Spool, SpoolError, SpoolFull


def test_restart_immutability_and_independent_identity(spools):
    cfg, (a, b) = spools
    original = packet(a)
    a.append(original)
    a.defer(original["event_id"], "pending", 999)
    assert a.station_id != b.station_id
    reopened = Spool(a.path, cfg.collector_id, a.get_meta("driver_module"), a.station)
    try:
        row = reopened.candidates(1000, 5)[0]
        assert json.loads(row["payload"]) == original
        assert row["attempts"] == 1
        assert reopened.station_id == a.station_id
        assert reopened.db.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        reopened.close()
    with pytest.raises(SpoolError):
        Spool(a.path, "new-collector", a.get_meta("driver_module"), a.station)


def test_quota_never_discards_unconfirmed(spools):
    _, (a, _) = spools
    a.station = dataclasses.replace(a.station, max_events=1)
    first, second = packet(a), packet(a)
    a.append(first)
    with pytest.raises(SpoolFull):
        a.append(second)
    assert a.status()["events"] == 1
    assert a.status()["full"]
    a.acknowledge(first["event_id"], 100)
    a.append(second)
    assert a.candidates(101, 1)[0]["event_id"] == second["event_id"]


def test_transaction_failure_rolls_back_usage_and_packet(spools):
    _, (a, _) = spools
    a.db.execute(
        "CREATE TRIGGER fail BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'fail'); END"
    )
    with pytest.raises(sqlite3.Error):
        a.append(packet(a))
    assert a.status()["events"] == a.status()["bytes"] == 0
    assert a.get_meta("last_collected") is None


def test_quarantine_is_retained_and_explicit_retry_preserves_ids(spools):
    _, (a, _) = spools
    p = packet(a)
    a.append(p)
    a.defer(p["event_id"], "too_old", 0, True)
    assert a.candidates(100, 1) == []
    assert a.status()["quarantined"] == 1
    assert a.retry_quarantined() == 1
    assert json.loads(a.candidates(100, 1)[0]["payload"]) == p
