"""Optional real PHP endpoint behind the test TLS terminator; no PHP project edits."""

import dataclasses
import http.client
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest
from weewx.drivers.simulator import Simulator

from weewx_php_ingest.protocol import event_from_loop
from weewx_php_ingest.supervisor import open_spools
from weewx_php_ingest.transport import HTTPSClient
from weewx_php_ingest.uploader import Uploader


def test_real_php_admission_lost_ack_and_station_identity(make_config, tls_server, tmp_path):
    root_text = os.environ.get("WEEWX_PHP_ROOT")
    php = shutil.which("php")
    if not root_text or not php:
        pytest.skip("set WEEWX_PHP_ROOT and install PHP with SQLite3 for receiver integration")
    root = Path(root_text).resolve()
    php_config = tmp_path / "weather.conf"
    data_dir = tmp_path / "php-data"
    php_config.write_text(
        f"data_dir = {data_dir.as_posix()}\ntimezone = UTC\n"
        "[Ingest]\nenabled = true\ntick_mode = external\n"
        "trusted_proxies = 127.0.0.1\n"
    )

    def cli(*args):
        result = subprocess.run(
            [php, str(root / "bin/weewx-php"), "--config", str(php_config), "collector", *args],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, "PHP collector CLI failed"
        return result.stdout

    provisioned = dict(line.split(": ", 1) for line in cli("add", "Integration").splitlines())
    cfg = make_config(endpoint=tls_server["url"])
    cfg.token_file.write_text(provisioned["token"])
    cfg = dataclasses.replace(
        cfg, collector_id=provisioned["collector_id"], ca_file=tls_server["ca"]
    )
    spools = open_spools(cfg)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server_log = (tmp_path / "php-server.log").open("w+")
    env = {**os.environ, "WEEWX_PHP_CONF": str(php_config)}
    process = subprocess.Popen(
        [php, "-S", f"127.0.0.1:{port}", "-t", str(root / "public")],
        env=env,
        stdout=server_log,
        stderr=server_log,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )

    def backend(payload, headers):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request(
                "POST",
                "/ingest/weewx.php",
                json.dumps(payload),
                {
                    "Content-Type": "application/json",
                    "Authorization": headers["Authorization"],
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-For": "192.0.2.10",
                },
            )
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    tls_server["backend"]["send"] = backend
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("PHP server startup failed")
        now = int(time.time())
        simulator = Simulator(mode="generator", start_time=now - 100, loop_interval=2.5)
        source = next(simulator.genLoopPackets())
        expected = {}
        for spool in spools:
            event = event_from_loop(source, spool.station_id, "weewx.drivers.simulator")
            spool.append(event)
            expected[f"{cfg.collector_id}/{spool.station_id}"] = event
        uploader = Uploader(cfg, spools, HTTPSClient(cfg))
        assert uploader.tick(now=now) == 0
        assert all(s.status()["rejections"] == {"pending": 1} for s in spools)
        for spool in spools:
            cli("adopt", cfg.collector_id, spool.station_id, spool.station.key)
        tls_server["behaviors"].append("lost")
        assert uploader.tick(now=now + 61) == 0
        assert all(s.status()["events"] == 1 for s in spools)
        uploader = Uploader(cfg, spools, HTTPSClient(cfg))
        assert uploader.tick(now=now + 120) == 2
        assert all(s.status()["events"] == 0 for s in spools)
        db = sqlite3.connect(data_dir / "live.sdb")
        try:
            rows = db.execute("SELECT identity,dateTime,usUnits,data FROM packet").fetchall()
            assert len(rows) == 2
            for identity, timestamp, units, data in rows:
                event = expected[identity]
                assert (timestamp, units, json.loads(data)) == (
                    event["dateTime"],
                    event["usUnits"],
                    event["data"],
                )
            assert db.execute("SELECT COUNT(*) FROM weewx_receipt").fetchone()[0] == 2
        finally:
            db.close()
    finally:
        process.terminate()
        process.wait(timeout=10)
        server_log.close()
        for spool in spools:
            spool.close()
    assert provisioned["token"] not in (tmp_path / "php-server.log").read_text()
