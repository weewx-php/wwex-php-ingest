"""Original air-quality services against simulated local device APIs."""

import base64
import dataclasses
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from configobj import ConfigObj
from Cryptodome.Cipher import AES
from test_virtual import virtual_config

from weewx_php_ingest.config import load_config
from weewx_php_ingest.hardware import probe_packet
from weewx_php_ingest.runtime import driver_config
from weewx_php_ingest.service_profiles import CLASSES, configure
from weewx_php_ingest.supervisor import open_spools
from weewx_php_ingest.transport import HTTPSClient
from weewx_php_ingest.uploader import Uploader

FIXTURES = {
    "AirGradient": (
        "airgradient",
        "airgradient.py",
        "42f30d96c51960effb249e33111134f05e1d95b0a111ec05602cfedddb569197",
    ),
    "AirLink": (
        "airlink",
        "airlink.py",
        "7b1f405334006d8b09502b3010b65a8aae4092e5886653e197efd595243e20ef",
    ),
    "air-Q": (
        "airq",
        "airQ_corant.py",
        "b82638ace1b6bc349339875685ecee1a593a9adf74703300dcabb7bd8fd24361",
    ),
}


def response(profile, value, path):
    if profile == "AirGradient":
        assert path == "/measures/current"
        return {
            "serialno": f"device-{value}",
            "pm01": value,
            "pm02Compensated": value + 1,
            "pm10": value + 2,
            "rco2": 500 + value,
            "tvocIndex": value + 3,
            "tvocRaw": 20000 + value,
            "noxIndex": value + 4,
            "noxRaw": 10000 + value,
        }
    if profile == "AirLink":
        assert path == "/v1/current_conditions"
        row = {
            "data_structure_type": 6,
            "lsid": value,
            "last_report_time": int(time.time()),
            "temp": 70.0,
            "hum": 40.0,
            "dew_point": 45.0,
            "wet_bulb": 50.0,
            "heat_index": 70.0,
        }
        for key in ("pm_1_last", "pm_2p5_last", "pm_10_last"):
            row[key] = value
        row["pm_1"] = float(value)
        for name in ("pm_2p5", "pm_10"):
            for suffix in ("", "_last_1_hour", "_last_3_hours", "_last_24_hours", "_nowcast"):
                row[name + suffix] = float(value)
        for suffix in ("last_1_hour", "last_3_hours", "nowcast", "last_24_hours"):
            row["pct_pm_data_" + suffix] = 100
        return {
            "error": None,
            "data": {"name": "airlink", "ts": int(time.time()), "conditions": [row]},
        }
    assert path in ("/config", "/data")
    if path == "/config":
        return {"id": f"device-{value}", "ppb&ppm": True, "RoomType": "indoor"}
    return {
        "timestamp": int(time.time() * 1000),
        "DeviceID": f"device-{value}",
        "Status": "OK",
        "bat": "external",
        "pm1": [value, 0.1],
        "pm2_5": [value + 1, 0.1],
        "pm10": [value + 2, 0.1],
        "co2": [500 + value, 1],
        "temperature": [21, 0.1],
        "humidity": [40, 0.1],
        "pressure": [1013, 0.1],
        "sound": [value + 30, 0.1],
        "tvoc": [1000 + value, 1],
        "no2": [5, 0.1],
        "health": 900,
    }


@pytest.fixture(params=list(FIXTURES))
def service_config(request, make_config, tmp_path, monkeypatch):
    profile = request.param
    folder, filename, digest = FIXTURES[profile]
    source = Path(__file__).parent / "fixtures" / (folder + "_upstream") / filename
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    user = tmp_path / "bin" / "user"
    user.mkdir(parents=True)
    (user / "__init__.py").touch()
    shutil.copyfile(source, user / filename)
    password = secrets.token_hex(8)
    monkeypatch.setattr("weewx_php_ingest.service_profiles.getpass.getpass", lambda _: password)
    sensors, servers = [], []

    def start_sensor(value):
        state = {"value": value, "calls": [], "password": password, "invalid": False}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                state["calls"].append(self.path)
                payload = response(profile, value, self.path)
                if state["invalid"]:
                    payload = (
                        {}
                        if profile != "air-Q"
                        else {"timestamp": int(time.time() * 1000), "Status": "OK"}
                    )
                if profile == "air-Q":
                    raw = json.dumps(payload).encode()
                    padding = 16 - len(raw) % 16
                    iv = secrets.token_bytes(16)
                    cipher = AES.new(password.encode().ljust(32, b"0"), AES.MODE_CBC, iv=iv)
                    payload = {
                        "content": base64.b64encode(
                            iv + cipher.encrypt(raw + bytes([padding]) * padding)
                        ).decode()
                    }
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        state["port"] = server.server_port
        sensors.append(state)
        return state

    cfg = virtual_config(make_config())
    raw = ConfigObj(str(cfg.path))
    raw["WEEWX_ROOT"] = str(tmp_path)
    raw["USER_ROOT"] = "bin/user"
    try:
        for index, station in enumerate(raw["Stations"].values()):
            sensor = start_sensor(10 + index * 40)
            answers = iter(
                [profile, f"127.0.0.1:{sensor['port']}"]
                if profile == "air-Q"
                else [profile, "127.0.0.1", str(sensor["port"])]
            )
            configure(station, lambda *_, answers=answers: next(answers))
            # Keep the physical service's polling cadence independent of fast test LOOPs.
            if profile == "air-Q":
                station["airQ"]["query_interval"] = "0.1"
            elif profile == "AirGradient":
                station["AirGradient"]["poll_secs"] = "1"
        raw.write()
        yield load_config(cfg.path), profile, sensors
    finally:
        for server, thread in servers:
            server.shutdown()
            server.server_close()
            thread.join(3)


@pytest.fixture
def service_collected(service_config, tmp_path):
    cfg, profile, sensors = service_config
    spools = open_spools(cfg)
    children, logs = [], []
    try:
        for station in cfg.stations:
            log = (tmp_path / f"{station.key}.log").open("w+")
            logs.append(log)
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
                    env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            )
        until = time.monotonic() + 15
        while time.monotonic() < until:
            assert all(child.poll() is None for child in children), [
                Path(log.name).read_text() for log in logs
            ]
            if all(spool.status()["events"] >= 2 for spool in spools):
                break
            time.sleep(0.05)
        else:
            pytest.fail(
                "No service readings: " + repr([Path(log.name).read_text() for log in logs])
            )
        (cfg.state_dir / "stop").touch()
        for child in children:
            assert child.wait(timeout=12) == 0
        yield cfg, profile, sensors, spools
        for log in logs:
            assert sensors[0]["password"] not in Path(log.name).read_text()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        for log in logs:
            log.close()
        for spool in spools:
            spool.close()


def test_two_original_services_only_their_sensor_values(service_collected):
    cfg, profile, sensors, spools = service_collected
    assert spools[0].station_id != spools[1].station_id
    for spool, sensor in zip(spools, sensors, strict=True):
        rows = spool.candidates(time.time(), 128)
        assert len(rows) >= 2
        for row in rows:
            event = json.loads(row["payload"])
            values = event["data"]
            assert event["station_id"] == spool.station_id
            assert event["kind"] == "loop" and event["usUnits"] == 17
            assert values["pm1_0"] == sensor["value"]
            assert "outTemp" not in values and "rain" not in values
            assert all(v is None or type(v) in (float, int) for v in values.values())
            if profile == "AirGradient":
                assert values["co2"] == 500 + sensor["value"]
                assert values["tvocRaw"] == 20000 + sensor["value"]
                assert values["noxIndex"] == sensor["value"] + 4
            elif profile == "AirLink":
                assert values["pm2_5_1m"] == sensor["value"]
                assert values["pm2_5_nowcast"] == sensor["value"]
            else:
                assert values["airqTemp"] == 21
                assert values["co2"] == 500 + sensor["value"]
                assert values["noise"] == sensor["value"] + 30
                assert values["TVOC"] == pytest.approx((1000 + sensor["value"]) / 1000)
                assert values["no2_m"] > 0
                assert not {"no2", "airqDeviceID", "airqStatus", "airqBattery"} & values.keys()
        raw, _ = driver_config(spool.station)
        assert raw["StdArchive"]["archive_interval"] == "2"
        assert raw["Engine"]["Services"]["data_services"] == [CLASSES[profile]]


def test_original_service_probe(service_config):
    cfg, profile, sensors = service_config
    event = probe_packet(cfg.path, "s0", timeout=15)
    assert event["data"]["pm1_0"] == sensors[0]["value"]
    assert not sensors[1]["calls"]
    assert not cfg.state_dir.exists()


def test_invalid_sensor_data_does_not_pass_probe(service_config):
    from weewx_php_ingest.config import ConfigError

    cfg, _, sensors = service_config
    sensors[0]["invalid"] = True
    with pytest.raises(ConfigError, match="timed out"):
        probe_packet(cfg.path, "s0", timeout=2)
    assert not cfg.state_dir.exists()


def test_original_service_to_php(service_collected, tmp_path, tls_server):
    from php_receiver import receiver

    cfg, _, _, spools = service_collected
    cfg = dataclasses.replace(cfg, endpoint=tls_server["url"], ca_file=tls_server["ca"])
    expected = [
        json.loads(row["payload"]) for spool in spools for row in spool.candidates(time.time(), 128)
    ]
    with receiver(tmp_path, tls_server) as (cli, data):
        uploader = Uploader(cfg, spools, HTTPSClient(cfg))
        now = time.time()
        assert uploader.tick(now) == 0
        senders = [line.split()[0] for line in cli("list").splitlines()]
        assert len(senders) == 2
        for sender in senders:
            cli("adopt", sender)
        assert uploader.tick(now + 61) == len(expected)
        with sqlite3.connect(data / "live.sdb") as db:
            rows = db.execute("SELECT identity,data,usUnits FROM packet").fetchall()
            assert len(rows) == len(expected)
            for identity, readings, units in rows:
                assert units == 17
                sid = identity.split("/")[1]
                assert any(
                    p["station_id"] == sid and p["data"] == json.loads(readings) for p in expected
                )


def test_profile_preserves_custom_airgradient_mapping():
    data = {"AirGradient": {"LoopFields": {"rco2": "customCO2"}}}
    answers = iter(["AirGradient", "sensor.local", "80"])
    configure(data, lambda *_: next(answers))
    assert data["AirGradient"]["LoopFields"] == {"rco2": "customCO2"}


def test_airq_password_is_hidden_retained_and_prefixed_exclusions(monkeypatch, capsys):
    password = secrets.token_hex(8)
    data = {"airQ": {"bedroom": {"host": "airq.local", "password": password, "prefix": "bed"}}}
    answers = iter(["air-Q", "airq.local"])
    monkeypatch.setattr("weewx_php_ingest.service_profiles.getpass.getpass", lambda _: "")
    configure(data, lambda *_: next(answers))
    assert data["airQ"]["bedroom"]["password"] == password
    assert "bed_Status" in data["Ingest"]["exclude_fields"]
    assert "bed_no2" in data["Ingest"]["exclude_fields"]
    assert data["Ingest"]["Services"]["prep_services"] == ["user.airQ_corant.AirqUnits"]
    assert password not in capsys.readouterr().out


@pytest.mark.parametrize(
    "bad",
    ["user:pass@host", "https://host", "host/path", "host?x", "host\0", "host:0", "host:65536"],
)
def test_profile_retries_invalid_host(bad):
    answers = iter(["AirLink", bad, "valid.local", "80"])
    data = {}
    configure(data, lambda *_: next(answers))
    assert data["AirLink"]["Sensor1"]["hostname"] == "valid.local"


def test_airq_retries_invalid_password_without_printing_it(monkeypatch, capsys):
    passwords = iter(["x" * 33, "bad\npassword", "valid-secret"])
    answers = iter(["air-Q", "host.local"])
    monkeypatch.setattr(
        "weewx_php_ingest.service_profiles.getpass.getpass", lambda _: next(passwords)
    )
    data = {}
    configure(data, lambda *_: next(answers))
    assert data["airQ"]["Sensor1"]["password"] == "valid-secret"
    assert "valid-secret" not in capsys.readouterr().out
