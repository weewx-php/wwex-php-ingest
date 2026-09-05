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

from weewx_php_ingest.config import read_token
from weewx_php_ingest.protocol import event_from_archive, event_from_loop
from weewx_php_ingest.supervisor import open_spools
from weewx_php_ingest.transport import HTTPSClient
from weewx_php_ingest.uploader import Uploader


@pytest.mark.parametrize("hardware", [False, True])
def test_real_php_admission_lost_ack_and_station_identity(
    make_config, tls_server, tmp_path, hardware
):
    root_text = os.environ.get("WEEWX_PHP_ROOT")
    php = shutil.which("php")
    if not root_text or not php:
        pytest.skip("set WEEWX_PHP_ROOT and install PHP with SQLite3 for receiver integration")
    root = Path(root_text).resolve()
    php_config = tmp_path / "weather.conf"
    data_dir = tmp_path / "php-data"
    php_config.write_text(
        f"data_dir = {data_dir.as_posix()}\ntimezone = UTC\n"
        "archive_interval = 1m\narchive_delay = 0\nmax_intervals_per_run = 2000\n"
        "[Ingest]\nenabled = true\ntick_mode = external\n"
        "trusted_proxies = 127.0.0.1\n"
    )

    def cli(*args):
        result = subprocess.run(
            [php, str(root / "bin/weewx-php"), "--config", str(php_config), "ingest", *args],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, "PHP collector CLI failed"
        return result.stdout

    cfg = make_config(endpoint=tls_server["url"])
    cfg = dataclasses.replace(cfg, ca_file=tls_server["ca"])
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
            serialize = event_from_archive if hardware else event_from_loop
            if hardware:
                source = {
                    **source,
                    "dateTime": (now // 300) * 300 - 300,
                    "interval": 5,
                    "usUnits": 17,
                    "rain": 0.6,
                }
            event = serialize(source, spool.station_id, "weewx.drivers.simulator")
            spool.append(event)
            expected[f"{cfg.collector_id}/{spool.station_id}"] = event
        uploader = Uploader(cfg, spools, HTTPSClient(cfg))
        assert uploader.tick(now=now) == 0
        assert all(s.status()["rejections"] == {"pending": 1} for s in spools)
        senders = [line.split()[0] for line in cli("list").splitlines()]
        assert len(senders) == 2
        for sender in senders:
            cli("adopt", sender)
        if hardware:
            with php_config.open("a") as config_file:
                config_file.write("[Archives]\n")
                for index, sender in enumerate(senders):
                    config_file.write(
                        f"[[a{index}]]\nprimary = {sender}\nsenders = {sender}\n"
                        "auto_mapping = true\nunit_system = METRICWX\n"
                        f"database = {(data_dir / f'a{index}.sdb').as_posix()}\n"
                    )
        tls_server["behaviors"].append("lost")
        assert uploader.tick(now=now + 61) == 0
        assert all(s.status()["events"] == 1 for s in spools)
        uploader = Uploader(cfg, spools, HTTPSClient(cfg))
        assert uploader.tick(now=now + 120) == 2
        assert all(s.status()["events"] == 0 for s in spools)
        db = sqlite3.connect(data_dir / "live.sdb")
        try:
            rows = db.execute(
                "SELECT identity,dateTime,usUnits,data,kind,interval FROM packet"
            ).fetchall()
            assert len(rows) == 2
            for identity, timestamp, units, data, kind, interval in rows:
                event = expected[identity]
                assert kind == ("archive" if hardware else "loop")
                assert interval == (5.0 if hardware else None)
                assert (timestamp, units, json.loads(data)) == (
                    event["dateTime"],
                    event["usUnits"],
                    event["data"],
                )
            assert db.execute("SELECT COUNT(*) FROM weewx_receipt").fetchone()[0] == 2
        finally:
            db.close()
        if hardware:
            tick = subprocess.run(
                [php, str(root / "bin/weewx-php"), "--config", str(php_config), "tick"],
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert tick.returncode == 0, tick.stdout + tick.stderr
            script = tmp_path / "history.php"
            script.write_text(r"""<?php
require $argv[1] . '/src/autoload.php';
$wx = \WeewxPhp\Frontend\Weather::open($argv[2], $argv[3]);
$period = $wx->between((int) $argv[4] - 300, (int) $argv[4]);
echo json_encode(['total' => $period->sum('rain')->raw(),
    'series' => $period->series('rain', 60, 'sum')->series()], JSON_THROW_ON_ERROR);
$wx->close();
""")
            for index in range(len(senders)):
                with sqlite3.connect(data_dir / f"a{index}.sdb") as archive:
                    assert archive.execute("SELECT COUNT(*) FROM archive").fetchone()[0] == 0
                    assert archive.execute(
                        "SELECT stop-start,value FROM weewx_hardware WHERE field='rain'"
                    ).fetchall() == [(300, 0.6)]
                history = subprocess.run(
                    [
                        php,
                        str(script),
                        str(root),
                        str(php_config),
                        f"a{index}",
                        str(source["dateTime"]),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                assert history.returncode == 0, history.stderr
                result = json.loads(history.stdout)
                assert result["total"] == 0.6
                assert [point["value"] for point in result["series"]["points"]] == [None] * 5
                assert result["series"]["fallback"] == [
                    {
                        "start": source["dateTime"] - 300,
                        "end": source["dateTime"],
                        "value": 0.6,
                        "coverage": 1,
                    }
                ]
    finally:
        process.terminate()
        process.wait(timeout=10)
        server_log.close()
        for spool in spools:
            spool.close()
    assert read_token(cfg) not in (tmp_path / "php-server.log").read_text()
