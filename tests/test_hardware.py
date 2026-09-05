"""Hardware logger lifecycle on a real StdEngine, including durable restart recovery."""

import json
import time
import uuid

import pytest
import weewx
from weewx.drivers import AbstractDevice

from weewx_php_ingest.config import ConfigError
from weewx_php_ingest.protocol import (
    ProtocolError,
    decode_response,
    encode,
    envelope,
    event_from_archive,
)
from weewx_php_ingest.runtime import create_engine, driver_config
from weewx_php_ingest.spool import Spool, SpoolFull
from weewx_php_ingest.supervisor import open_spools


def record(timestamp=None, **values):
    return {
        "dateTime": timestamp or int(time.time()) - 600,
        "usUnits": 17,
        "interval": 5,
        "rain": 0.5,
        "outTemp": 12,
        **values,
    }


@pytest.mark.parametrize("units", [1, 16, 17])
def test_hardware_identity_and_version_are_stable(units):
    sid = str(uuid.uuid4())
    source = record(usUnits=units)
    event = event_from_archive(source, sid, "user.logger")
    assert event == event_from_archive(source, sid, "user.logger")
    assert event["interval"] == 5 and event["usUnits"] == units
    assert "interval" not in event["data"]
    assert event["kind"] == "archive"
    changed = event_from_archive({**source, "rain": 99}, sid, "user.logger")
    assert changed["event_id"] == event["event_id"]  # Receiver detects conflicting contents.
    assert (
        event_from_archive(source, str(uuid.uuid4()), "user.logger")["event_id"]
        != event["event_id"]
    )
    assert json.loads(envelope(str(uuid.uuid4()), [event]))["version"] == 2
    reply = {"version": 1, "status": "ok", "results": []}
    with pytest.raises(ProtocolError):
        decode_response(encode(reply), [event])
    reply["version"] = 2
    assert decode_response(encode(reply), [event])[0] == {}


@pytest.mark.parametrize(
    "interval", [None, True, "5", 0, -1, 1441, 1.001, float("nan"), float("inf")]
)
def test_invalid_hardware_intervals(interval):
    with pytest.raises(ProtocolError):
        event_from_archive(record(interval=interval), str(uuid.uuid4()), "user.logger")


def test_full_spool_does_not_advance_hardware_cursor(make_config):
    cfg = make_config(count=1, max_events=1)
    spool = open_spools(cfg)[0]
    try:
        first = event_from_archive(record(), spool.station_id, spool.get_meta("driver_module"))
        spool.append(first)
        second = event_from_archive(
            record(first["dateTime"] + 300), spool.station_id, spool.get_meta("driver_module")
        )
        with pytest.raises(SpoolFull):
            spool.append(second)
        assert spool.get_meta("hardware_cursor") == first["dateTime"]
        spool.acknowledge(first["event_id"], time.time())
        spool.append(second)
        path = spool.path
    finally:
        spool.close()
    reopened = Spool(path, cfg.collector_id, "weewx.drivers.simulator", cfg.stations[0])
    try:
        assert reopened.get_meta("hardware_cursor") == second["dateTime"]
        assert json.loads(reopened.candidates(time.time(), 1)[0]["payload"]) == second
    finally:
        reopened.close()


@pytest.fixture
def logger_engine(make_config, monkeypatch):
    cfg = make_config(count=1)
    spool = open_spools(cfg)[0]
    raw, _ = driver_config(cfg.stations[0])
    raw["StdArchive"] = {"record_generation": "hardware"}
    base = int(time.time() // 300) * 300 - 900

    class Logger(AbstractDevice):
        hardware_name = "Logger test fixture"
        archive_interval = 300
        closed = False
        error = None

        def __init__(self, engine):
            self.calls = []
            self.records = [record(base + 300)]
            engine.bind(weewx.NEW_ARCHIVE_RECORD, self.normalize)

        def normalize(self, event):
            event.record["extraTemp1"] = 23

        def genStartupRecords(self, since):
            self.calls.append(("startup", since))
            yield from self.records

        def genArchiveRecords(self, since):
            self.calls.append(("archive", since))
            if self.error:
                raise self.error
            yield from self.records  # Re-delivery intentionally includes the boundary.

        def getTime(self):
            return base + 900

        def closePort(self):
            self.closed = True

    monkeypatch.setattr("weewx.drivers.simulator.loader", lambda config, engine: Logger(engine))
    engine = create_engine(raw, cfg.stations[0], spool)
    yield cfg, spool, raw, engine, base
    engine.shutDown()
    spool.close()


def test_startup_period_poll_callbacks_loop_and_restart(logger_engine):
    cfg, spool, raw, engine, base = logger_engine
    engine.dispatchEvent(weewx.Event(weewx.STARTUP))
    engine.dispatchEvent(weewx.Event(weewx.PRE_LOOP))
    engine.console.records.append(record(base + 600, rain=0.7))
    engine.dispatchEvent(weewx.Event(weewx.POST_LOOP))
    engine.dispatchEvent(
        weewx.Event(
            weewx.NEW_LOOP_PACKET, packet={"dateTime": base + 950, "usUnits": 17, "rain": 0.2}
        )
    )
    events = [json.loads(r["payload"]) for r in spool.candidates(time.time(), 10)]
    assert len(events) == 3
    assert sum(p["kind"] == "archive" for p in events) == 2
    assert all(p["data"]["extraTemp1"] == 23 for p in events if p["kind"] == "archive")
    assert engine.console.calls[-1] == ("archive", base + 300)
    assert spool.get_meta("hardware_cursor") == base + 600
    for event in events:
        spool.acknowledge(event["event_id"], time.time())
    engine.shutDown()
    assert engine.console.closed
    restarted = create_engine(raw, cfg.stations[0], spool)
    try:
        restarted.console.records = [record(base + 300), record(base + 600), record(base + 900)]
        restarted.dispatchEvent(weewx.Event(weewx.STARTUP))
        assert restarted.console.calls == [("startup", base + 600)]
        assert spool.status()["events"] == 1
        assert spool.get_meta("hardware_cursor") == base + 900
        assert restarted.service_obj == []
    finally:
        restarted.shutDown()


def test_hardware_error_retries_and_not_implemented_falls_back(logger_engine):
    _, spool, _, engine, _ = logger_engine
    engine.dispatchEvent(weewx.Event(weewx.PRE_LOOP))
    engine.console.error = weewx.HardwareError("no console")
    engine.dispatchEvent(weewx.Event(weewx.POST_LOOP))
    assert spool.get_meta("hardware_state") == "retrying"
    assert spool.get_meta("hardware_cursor") is None
    engine.console.error = None
    engine.dispatchEvent(weewx.Event(weewx.POST_LOOP))
    assert spool.get_meta("hardware_state") == "active"
    engine.console.error = NotImplementedError()
    engine.dispatchEvent(weewx.Event(weewx.POST_LOOP))
    assert spool.get_meta("hardware_state") == "software_fallback"
    count = len(engine.console.calls)
    engine.dispatchEvent(weewx.Event(weewx.POST_LOOP))
    assert len(engine.console.calls) == count


@pytest.mark.parametrize("mode", ["software", "hardware"])
def test_software_and_no_catchup_skip_startup(logger_engine, mode):
    cfg, spool, raw, engine, _ = logger_engine
    engine.shutDown()
    raw["StdArchive"] = {"record_generation": mode, "no_catchup": True}
    replacement = create_engine(raw, cfg.stations[0], spool)
    try:
        replacement.dispatchEvent(weewx.Event(weewx.STARTUP))
        assert replacement.console.calls == []
        assert spool.status()["events"] == 0
        assert (spool.get_meta("hardware_cursor") is not None) == (mode == "hardware")
    finally:
        replacement.shutDown()


def test_real_simulator_falls_back_and_invalid_mode_is_rejected(make_config):
    cfg = make_config(count=1)
    spool = open_spools(cfg)[0]
    raw, _ = driver_config(cfg.stations[0])
    engine = create_engine(raw, cfg.stations[0], spool)
    try:
        engine.dispatchEvent(weewx.Event(weewx.STARTUP))
        assert spool.get_meta("hardware_state") == "software_fallback"
        engine.dispatchEvent(
            weewx.Event(
                weewx.NEW_LOOP_PACKET,
                packet={"dateTime": int(time.time()), "usUnits": 17, "rain": 0.2},
            )
        )
        assert spool.status()["events"] == 1
        with pytest.raises(ProtocolError):
            engine.dispatchEvent(weewx.Event(weewx.NEW_LOOP_PACKET, packet=record()))
    finally:
        engine.shutDown()
        spool.close()
    raw["StdArchive"] = {"record_generation": "anything"}
    with pytest.raises(ConfigError):
        create_engine(raw, cfg.stations[0], spool)


def test_historical_record_intervals_are_independent_of_poll_and_upload(logger_engine):
    cfg, spool, _, engine, base = logger_engine
    assert cfg.send_interval == 0.1
    assert engine.console.archive_interval == 300
    engine.console.records = [
        record(base + 300, interval=1),
        record(base + 600, interval=5),
        record(base + 900, interval=5),
    ]
    engine.dispatchEvent(weewx.Event(weewx.STARTUP))
    events = [json.loads(r["payload"]) for r in spool.candidates(time.time(), 10)]
    assert sorted(p["interval"] for p in events) == [1, 5, 5]
    assert spool.status()["hardware_interval"] == 300
    assert spool.status()["hardware_last_interval"] == 300
    assert spool.get_meta("hardware_cursor") == base + 900


def test_console_interval_overrides_unused_software_interval(logger_engine):
    cfg, spool, raw, engine, _ = logger_engine
    engine.shutDown()
    raw["StdArchive"] = {
        "record_generation": "hardware",
        "archive_interval": 2,
        "archive_delay": 15,
    }
    replacement = create_engine(raw, cfg.stations[0], spool)
    try:
        assert spool.status()["hardware_interval"] == 300
    finally:
        replacement.shutDown()
