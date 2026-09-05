"""Virtual LOOP stations and the unmodified upstream PurpleAir service."""

import dataclasses
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import weewx
from configobj import ConfigObj

from weewx_php_ingest.config import ConfigError, load_config
from weewx_php_ingest.configure import setup
from weewx_php_ingest.hardware import drivers, probe_packet
from weewx_php_ingest.protocol import ProtocolError
from weewx_php_ingest.runtime import create_engine, driver_config
from weewx_php_ingest.supervisor import open_spools
from weewx_php_ingest.transport import HTTPSClient
from weewx_php_ingest.uploader import Uploader
from weewx_php_ingest.virtual import Virtual, options

UPSTREAM = Path(__file__).parent / "fixtures" / "purple_upstream" / "purple.py"


def virtual_config(cfg):
    raw = ConfigObj(str(cfg.path))
    for station in raw["Stations"].values():
        station["Station"]["station_type"] = "Virtual"
        station.pop("Simulator", None)
        station["Virtual"] = {
            "driver": "weewx_php_ingest.virtual",
            "loop_interval": "0.1",
            "unit_system": "METRICWX",
        }
    raw.write()
    return load_config(cfg.path)


def test_virtual_generates_fresh_metadata_only_and_closes():
    assert ("Virtual", "weewx_php_ingest.virtual") in drivers()
    device = Virtual(loop_interval=0.001)
    packets = device.genLoopPackets()
    before = int(time.time())
    first = next(packets)
    assert set(first) == {"dateTime", "usUnits"}
    assert before <= first["dateTime"] <= int(time.time())
    assert first["usUnits"] == 17
    first["rain"] = 123
    second = next(packets)
    assert "rain" not in second
    device.closePort()
    with pytest.raises(StopIteration):
        next(packets)


@pytest.mark.parametrize("value", ["nan", "inf", "bad", "0", "-1", "61"])
def test_virtual_rejects_invalid_cadence(value):
    with pytest.raises(ConfigError, match="Virtual"):
        options({"loop_interval": value})


def test_virtual_rejects_invalid_units_and_hardware_mode(make_config):
    with pytest.raises(ConfigError):
        options({"unit_system": "bad"})
    cfg = virtual_config(make_config(count=1))
    raw = ConfigObj(str(cfg.path))
    raw["Stations"]["s0"]["StdArchive"] = {"record_generation": "hardware"}
    raw.write()
    with pytest.raises(ConfigError, match="software"):
        driver_config(cfg.stations[0])


def test_virtual_skips_empty_but_validates_enriched_packets(make_config):
    cfg = virtual_config(make_config(count=1))
    config = ConfigObj(str(cfg.path))
    config["Stations"]["s0"]["Ingest"]["exclude_fields"] = "ignored"
    config.write()
    cfg = load_config(cfg.path)
    spool = open_spools(cfg)[0]
    raw, _ = driver_config(cfg.stations[0])
    engine = create_engine(raw, cfg.stations[0], spool)
    try:

        def send(**values):
            engine.dispatchEvent(
                weewx.Event(
                    weewx.NEW_LOOP_PACKET,
                    packet={"dateTime": int(time.time()), "usUnits": 17, **values},
                )
            )

        send()
        send(pm2_5=None, ignored=9)
        assert spool.status()["events"] == 0
        assert spool.get_meta("last_collected") is None
        assert spool.get_meta("collection_state") == "waiting_for_service"
        assert spool.get_meta("hardware_state") == "disabled"
        engine.bind(weewx.NEW_LOOP_PACKET, lambda e: e.packet.update(pm2_5=0, pm1_0=None))
        send()
        events = spool.candidates(time.time(), 10)
        assert json.loads(events[0]["payload"])["data"] == {"pm2_5": 0, "pm1_0": None}
        with pytest.raises(ProtocolError, match="invalid_value"):
            send(bad="text")
    finally:
        engine.shutDown()
        spool.close()


def test_empty_virtual_probe_times_out_without_spooling(make_config):
    cfg = virtual_config(make_config(count=1))
    with pytest.raises(ConfigError, match="timed out"):
        probe_packet(cfg.path, "s0", timeout=1)
    assert not cfg.state_dir.exists()


@pytest.fixture
def purple_sensors():
    sensors = []

    def make(pm, state="ok"):
        readings = {"pm": pm, "state": state, "requests": 0}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                readings["requests"] += 1
                assert self.path == "/json"
                when = datetime.now(UTC)
                if readings["state"] == "stale":
                    when -= timedelta(hours=1)
                value = readings["pm"]
                payload = {
                    "DateTime": when.strftime("%Y/%m/%dT%H:%M:%Sz"),
                    "current_temp_f": 70,
                    "current_humidity": 40,
                    "current_dewpoint_f": 45,
                    "pressure": 1013.0,
                    "pm2.5_aqi": 10,
                    "pm2.5_aqi_b": 10,
                }
                for suffix in ("", "_b"):
                    payload.update(
                        {
                            key + suffix: float(value)
                            for key in (
                                "pm1_0_cf_1",
                                "pm1_0_atm",
                                "p_0_3_um",
                                "pm2_5_cf_1",
                                "pm2_5_atm",
                                "p_0_5_um",
                                "pm10_0_cf_1",
                                "pm10_0_atm",
                            )
                        }
                    )
                body = json.dumps(payload).encode() if readings["state"] != "invalid" else b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        readings["port"] = server.server_port
        sensors.append((server, thread))
        return readings

    yield make
    for server, thread in sensors:
        server.shutdown()
        server.server_close()
        thread.join(3)


@pytest.fixture
def purple_config(make_config, tmp_path, purple_sensors):
    assert hashlib.sha256(UPSTREAM.read_bytes()).hexdigest() == (
        "e85c13f459aad83c146d4aa1e3bc52e8b214a8b8e9638e3fcd476b119fd7005f"
    )
    user = tmp_path / "bin" / "user"
    user.mkdir(parents=True)
    (user / "__init__.py").touch()
    shutil.copyfile(UPSTREAM, user / "purple.py")
    cfg = virtual_config(make_config())
    sensors = [purple_sensors(10), purple_sensors(50, "stale")]
    raw = ConfigObj(str(cfg.path))
    raw["WEEWX_ROOT"] = str(tmp_path)
    raw["USER_ROOT"] = "bin/user"
    for station, sensor in zip(raw["Stations"].values(), sensors, strict=True):
        station["Ingest"]["Services"] = {"data_services": "user.purple.Purple"}
        station["Purple"] = {
            "poll_secs": "1",
            "Sensor1": {"enable": "true", "hostname": "127.0.0.1", "port": sensor["port"]},
        }
    raw.write()
    return load_config(cfg.path), sensors


@pytest.fixture
def purple_collected(purple_config, tmp_path):
    cfg, sensors = purple_config
    spools = open_spools(cfg)
    children, logs = [], []
    try:
        for station in cfg.stations:
            log = (tmp_path / f"{station.key}.log").open("w+")
            logs.append(log)
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "weewx_php_ingest",
                        "--config",
                        str(cfg.path),
                        "_worker",
                        station.key,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    env=env,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            )

        def wait_for(predicate):
            until = time.monotonic() + 15
            while time.monotonic() < until:
                assert all(p.poll() is None for p in children), [
                    Path(log.name).read_text() for log in logs
                ]
                if predicate():
                    return
                time.sleep(0.05)
            pytest.fail(
                "PurpleAir workers timed out: " + repr([Path(log.name).read_text() for log in logs])
            )

        wait_for(lambda: spools[0].status()["events"] >= 2 and sensors[1]["requests"] >= 2)
        assert spools[1].status()["events"] == 0  # Stale service data is not invented.
        assert spools[1].get_meta("collection_state") == "waiting_for_service"
        sensors[1]["state"] = "ok"
        wait_for(lambda: spools[1].status()["events"] >= 2)
        # Graceful file-based stop also works on Windows, where terminate is abrupt.
        (cfg.state_dir / "stop").touch()
        for child in children:
            assert child.wait(timeout=10) == 0
        yield cfg, spools, sensors
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        for log in logs:
            log.close()
        for spool in spools:
            spool.close()


def test_original_purple_two_processes_only_service_fields(purple_collected):
    cfg, spools, sensors = purple_collected
    assert spools[0].station_id != spools[1].station_id
    for spool, sensor in zip(spools, sensors, strict=True):
        for row in spool.candidates(time.time(), 128):
            event = json.loads(row["payload"])
            assert event["station_id"] == spool.station_id
            assert event["driver_module"] == "weewx_php_ingest.virtual"
            assert event["kind"] == "loop" and event["usUnits"] == 17
            assert set(event["data"]) == {
                "pm1_0",
                "pm2_5",
                "pm10_0",
                "pm2_5_aqi",
                "pm2_5_aqi_color",
            }
            assert event["data"]["pm1_0"] == sensor["pm"]
            assert event["data"]["pm10_0"] == sensor["pm"]
            assert event["data"]["pm2_5"] == pytest.approx(0.52 * sensor["pm"] - 0.086 * 40 + 5.75)
            assert "interval" not in event
    # Identity survives reopening the worker's spool.
    reopened = open_spools(cfg)
    try:
        assert [s.station_id for s in reopened] == [s.station_id for s in spools]
    finally:
        for spool in reopened:
            spool.close()


def test_original_purple_probe_reads_service_fields(purple_config):
    cfg, sensors = purple_config
    event = probe_packet(cfg.path, "s0", timeout=15)
    assert event["data"]["pm1_0"] == 10
    assert "outTemp" not in event["data"]
    assert sensors[0]["requests"] >= 1
    assert sensors[1]["requests"] == 0
    assert not cfg.state_dir.exists()


def test_guided_virtual_setup_preserves_instance_services(purple_config, monkeypatch):
    cfg, sensors = purple_config
    sensors[1]["state"] = "ok"
    replies = ["https://weather.example.org/ingest/weewx.php"]
    for sensor in sensors:
        replies.extend(
            [
                "Virtual",
                "0.1",
                "METRICWX",
                "n",
                "user.purple.Purple",
                "127.0.0.1",
                str(sensor["port"]),
            ]
        )
    replies.append("n")
    answers = iter(replies)
    monkeypatch.setattr("builtins.input", lambda: next(answers))
    monkeypatch.setattr("weewx_php_ingest.configure.scan", lambda: ([], []))
    monkeypatch.setattr("weewx_php_ingest.configure.preview_upload", lambda *_: None)
    setup(cfg.path)
    configured = load_config(cfg.path)
    assert configured.collector_id == cfg.collector_id
    for station, sensor in zip(configured.stations, sensors, strict=True):
        raw, module = driver_config(station)
        assert module == "weewx_php_ingest.virtual"
        assert raw["Purple"]["Sensor1"]["port"] == str(sensor["port"])
        assert raw["Engine"]["Services"]["data_services"] == ["user.purple.Purple"]
        assert raw["StdArchive"]["record_generation"] == "software"


def test_original_purple_to_php_adoption_and_lost_ack(purple_collected, tmp_path, tls_server):
    from php_receiver import receiver

    cfg, spools, _ = purple_collected
    cfg = dataclasses.replace(cfg, endpoint=tls_server["url"], ca_file=tls_server["ca"])
    expected = {
        json.loads(row["payload"])["event_id"]: json.loads(row["payload"])
        for spool in spools
        for row in spool.candidates(time.time(), 128)
    }
    assert expected and len({p["station_id"] for p in expected.values()}) == 2
    with receiver(tmp_path, tls_server) as (cli, data):
        uploader = Uploader(cfg, spools, HTTPSClient(cfg))
        now = time.time()
        assert uploader.tick(now) == 0
        assert all(s.status()["rejections"].get("pending", 0) > 0 for s in spools)
        senders = [line.split()[0] for line in cli("list").splitlines()]
        assert len(senders) == 2
        for sender in senders:
            cli("adopt", sender)
        tls_server["behaviors"].append("lost")
        assert uploader.tick(now + 61) == 0
        assert sum(s.status()["events"] for s in spools) == len(expected)
        assert uploader.tick(now + 120) == len(expected)
        assert all(s.status()["events"] == 0 for s in spools)
        with sqlite3.connect(data / "live.sdb") as db:
            rows = db.execute("SELECT identity,data,kind,usUnits FROM packet").fetchall()
            assert len(rows) == len(expected)
            assert len({r[0] for r in rows}) == 2
            for identity, readings, kind, units in rows:
                sid = identity.split("/")[1]
                matching = next(p for p in expected.values() if p["station_id"] == sid)
                assert json.loads(readings) == matching["data"]
                assert kind == "loop" and units == 17
            assert db.execute("SELECT COUNT(*) FROM weewx_receipt").fetchone()[0] == len(expected)
