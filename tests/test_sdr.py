"""Radio demultiplexing, process ownership and real PHP station admission."""

import dataclasses
import json
import os
import sqlite3
import sys
import time
import uuid

import pytest
from configobj import ConfigObj

from weewx_php_ingest.config import ConfigError, load_config
from weewx_php_ingest.hardware import probe_packet
from weewx_php_ingest.protocol import ProtocolError, envelope
from weewx_php_ingest.sdr import (
    MODULE,
    Receiver,
    Router,
    command,
    decode,
    discover_spools,
    sensor_uuid,
    source_for,
)
from weewx_php_ingest.spool import SpoolFull
from weewx_php_ingest.supervisor import Supervisor, open_spools
from weewx_php_ingest.transport import HTTPSClient
from weewx_php_ingest.uploader import Uploader


@pytest.fixture
def radio_config(make_config):
    config = make_config(count=1)
    raw = ConfigObj(str(config.path), interpolation=False)
    station = raw["Stations"]["s0"]
    station["Station"]["station_type"] = "RTL433"
    station["RTL433"] = {"driver": MODULE, "device": "0"}
    raw.write()
    return load_config(config.path)


@pytest.fixture
def radio(radio_config):
    spools = open_spools(radio_config)
    router = Router(radio_config, spools[0])
    yield radio_config, spools, router
    router.close()
    for spool in spools:
        spool.close()


def reading(parent, sensor=1, channel=1, timestamp=None, model="Test-Sensor", **data):
    now = int(time.time())
    return decode(
        json.dumps(
            {
                "model": model,
                "id": sensor,
                "channel": channel,
                "time": now if timestamp is None else timestamp,
                "temperature_C": 10 + sensor,
                **data,
            }
        ),
        parent.station_id,
    )


def test_identity_normalization_and_unit_conversion(radio):
    _, spools, router = radio
    parent = spools[0]
    now = int(time.time())
    event = decode(
        json.dumps(
            {
                "model": 'A/B "sensor"',
                "id": 7,
                "channel": 0,
                "time": str(now) + ".100000",
                "temperature_F": 68,
                "humidity": 50,
                "wind_avg_km_h": 36,
                "battery_ok": True,
                "co2_ppm": 600,
                "mic": "CRC",
                "rssi": -20,
            }
        ),
        parent.station_id,
    )
    assert event["data"] == {
        "outTemp": 20,
        "outHumidity": 50,
        "windSpeed": 10,
        "batteryStatus": 0,
        "rtl_co2_ppm": 600,
    }
    assert event["source"]["sensor_id"] == "7" and event["source"]["channel"] == "0"
    assert (
        event["usUnits"] == 17 and json.loads(envelope(str(uuid.uuid4()), [event]))["version"] == 3
    )
    router.append(event)
    assert not router.append(event)
    assert parent.status()["events"] == 0


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        "[]",
        '{"model":"x","id":true,"temperature_C":1}',
        '{"model":"x","id":1,"id":2,"temperature_C":1}',
        '{"model":"x","id":1,"temperature_C":NaN}',
        '{"model":"x","id":1,"temperature_C":1e9999}',
        '{"model":"x","id":1,"temperature_C":2,"temperature_F":40}',
        '{"model":"x","id":1,"time":0,"temperature_C":2}',
        '{"model":"x\\n","id":1,"temperature_C":2}',
        '{"model":"x","id":1,"status":"OK"}',
        pytest.param("x" * 65537, id="oversized"),
    ],
)
def test_invalid_radio_output(raw):
    with pytest.raises(ProtocolError, match="invalid_rtl433_packet"):
        decode(raw, str(uuid.uuid4()))


def test_sensor_identity_scopes():
    rid, other = str(uuid.uuid4()), str(uuid.uuid4())
    ids = {
        sensor_uuid(source_for(r, m, s, c))
        for r, m, s, c in (
            (rid, "A", "1", ""),
            (rid, "A", "1", "1"),
            (rid, "B", "1", ""),
            (rid, "A", "2", ""),
            (other, "A", "1", ""),
        )
    }
    assert len(ids) == 5


def test_restart_discovery_rain_and_full_spool(radio):
    cfg, spools, router = radio
    parent = spools[0]
    now = int(time.time()) - 20
    first = reading(parent, timestamp=now, rain_mm=100)
    router.append(first)
    router.append(reading(parent, sensor=2, timestamp=now, rain_mm=900))
    router.close()
    router.spools.clear()
    again = reading(parent, timestamp=now + 3, rain_mm=101)
    router.append(again)
    a, b = router.spools[first["station_id"]], first["station_id"]
    assert a.station_id == b
    rows = [json.loads(r["payload"]) for r in a.candidates(time.time(), 100)]
    assert "rain" not in rows[0]["data"]
    assert rows[1]["data"]["rain"] == 1
    limited = dataclasses.replace(a.station, max_events=2)
    original = a.station
    a.station = limited
    pending = reading(parent, timestamp=now + 6, rain_mm=103)
    with pytest.raises(SpoolFull):
        router.append(pending)
    assert a.get_meta("rain_counter")["value"] == 101
    assert "rain" not in pending["data"]
    a.station = original
    router.append(pending)
    router.append(reading(parent, timestamp=now + 9, rain_mm=0))
    rows = [
        json.loads(r["payload"]) for r in a.db.execute("SELECT payload FROM events ORDER BY seq")
    ]
    assert rows[2]["data"]["rain"] == 2 and "rain" not in rows[3]["data"]
    discover_spools(cfg, spools)
    assert len(spools) == 3
    assert spools[1].station_id != spools[2].station_id


def test_capacity_and_out_of_order(radio):
    _, spools, router = radio
    router.maximum = 1
    event = reading(spools[0])
    router.append(event)
    with pytest.raises(ProtocolError, match="sensor_capacity"):
        router.append(reading(spools[0], sensor=2))
    old = reading(spools[0], timestamp=event["dateTime"] - 1)
    with pytest.raises(ProtocolError, match="out_of_order"):
        router.append(old)


def test_command_is_fixed_argv():
    assert command({"device": ":000123"}) == [
        "rtl_433",
        "-c",
        "0",
        "-d",
        ":000123",
        "-f",
        "433920000",
        "-F",
        "json",
        "-M",
        "time:unix",
        "-C",
        "si",
    ]
    with pytest.raises(ConfigError):
        command({"device": "0; touch /tmp/test"})


def test_pipe_reader_bounds_and_cleanup(tmp_path):
    script = tmp_path / "radio.py"
    script.write_text(
        "import sys,time\nsys.stdout.write('x'*100000+'\\n{}\\n');"
        "sys.stdout.flush()\ntime.sleep(30)\n"
    )
    receiver = Receiver([sys.executable, str(script)])
    try:
        line = None
        deadline = time.monotonic() + 5
        while line is None and time.monotonic() < deadline:
            line = receiver.read()
        assert line.rstrip() == b"{}"
    finally:
        receiver.close()
    assert receiver.process.poll() is not None and not receiver.reader.is_alive()


@pytest.fixture
def fake_radio(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("Linux executable/POSIX process-group integration")
    executable = tmp_path / "rtl_433"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """import json,os,time
from pathlib import Path
Path(os.environ['RADIO_PID']).write_text(str(os.getpid()))
if os.environ.get('RADIO_SILENT'):
    time.sleep(60)
while True:
    for channel in (1,2):
        print(json.dumps({'model':'Test-Sensor','id':42,'channel':channel,
                          'time':str(int(time.time())),'temperature_C':10*channel}), flush=True)
    time.sleep(3)
"""
    )
    executable.chmod(0o755)
    pid = tmp_path / "radio.pid"
    monkeypatch.setenv("RADIO_PID", str(pid))
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    return pid


def test_one_receiver_process_and_dynamic_sensors(radio_config, fake_radio):
    supervisor = Supervisor(radio_config)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            supervisor.poll()
            if len(supervisor.spools[0].get_meta("sensors", {})) == 2:
                break
            time.sleep(0.05)
        assert len(supervisor.children) == 2  # One receiver and one uploader.
        assert len(supervisor.spools[0].get_meta("sensors", {})) == 2
        assert supervisor.spools[0].status()["events"] == 0
        radio_pid = int(fake_radio.read_text())
        identities = set(supervisor.spools[0].get_meta("sensors"))
        worker = supervisor.children[0]
        worker.process.kill()
        worker.process.wait(timeout=3)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            supervisor.poll()
            if int(fake_radio.read_text()) != radio_pid:
                break
            time.sleep(0.05)
        assert int(fake_radio.read_text()) != radio_pid
        assert set(supervisor.spools[0].get_meta("sensors")) == identities
        radio_pid = int(fake_radio.read_text())
    finally:
        supervisor.shutdown()
    # Child is reaped during orderly shutdown.
    with pytest.raises(ProcessLookupError):
        os.kill(radio_pid, 0)


def test_probe_and_timeout_reap_receiver(radio_config, fake_radio, monkeypatch):
    event = probe_packet(radio_config.path, "s0", timeout=5)
    assert event["data"]["outTemp"] == 10 and event["source"]["channel"] == "1"
    assert not radio_config.state_dir.exists()
    monkeypatch.setenv("RADIO_SILENT", "1")
    with pytest.raises(ConfigError, match="timed out"):
        probe_packet(radio_config.path, "s0", timeout=1)
    pid = int(fake_radio.read_text())
    # SIGKILL group members may briefly remain zombies until PID 1 reaps them.
    stat = __import__("pathlib").Path(f"/proc/{pid}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"


def test_duplicate_receiver_configuration_rejected(radio_config):
    raw = ConfigObj(str(radio_config.path), interpolation=False)
    raw["Stations"]["s1"] = raw["Stations"]["s0"].dict()
    raw.write()
    with pytest.raises(ConfigError, match="only once"):
        open_spools(load_config(radio_config.path))


def test_real_php_discovery_adopt_retry_and_isolation(radio, tmp_path, tls_server):
    from php_receiver import receiver

    cfg, spools, router = radio
    cfg = dataclasses.replace(cfg, endpoint=tls_server["url"], ca_file=tls_server["ca"])
    client = HTTPSClient(cfg)
    uploader = Uploader(cfg, spools, client)
    now = time.time()
    events = [reading(spools[0], sensor=42, channel=c) for c in (1, 2)]
    for event in events:
        router.append(event)
    with receiver(tmp_path, tls_server) as (cli, data):
        assert uploader.tick(now) == 0
        lines = cli("list").splitlines()
        assert len(lines) == 2 and all("Test-Sensor 42 /" in line for line in lines)
        with sqlite3.connect(data / "live.sdb") as db:
            assert db.execute("SELECT count(*) FROM packet").fetchone()[0] == 0
            sources = db.execute("SELECT source FROM weewx_sensor").fetchall()
            assert {json.loads(row[0])["channel"] for row in sources} == {"1", "2"}
        cli("adopt", lines[0].split()[0])
        assert uploader.tick(now + 61) == 1
        cli("adopt", lines[1].split()[0])
        tls_server["behaviors"].append("lost")
        assert uploader.tick(now + 122) == 0
        assert uploader.tick(now + 184) == 1
        assert all(s.status()["events"] == 0 for s in spools)
        with sqlite3.connect(data / "live.sdb") as db:
            rows = db.execute("SELECT identity,data FROM packet").fetchall()
            assert len(rows) == 2 and len({r[0] for r in rows}) == 2
            assert {r[0].split("/")[1] for r in rows} == {e["station_id"] for e in events}
        # Tampered identity and metadata cannot be used to select another station.
        bad = {**events[0], "source": {**events[0]["source"], "channel": "3"}}
        assert client.send(envelope(cfg.collector_id, [bad])).status == 400
        bad = {**events[0], "source": {**events[0]["source"], "token": "unexpected"}}
        assert client.send(envelope(cfg.collector_id, [bad])).status == 400
        # New sensor discovered after adoption/uploader startup, without config edits.
        router.append(reading(spools[0], sensor=43))
        assert uploader.tick(now + 245) == 0
        assert len(cli("list").splitlines()) == 3
        assert len(spools) == 4
