"""Optional real PHP endpoint for collector integration tests."""

import http.client
import json
import os
import shutil
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import pytest


@contextmanager
def receiver(tmp_path, tls_server):
    php = shutil.which("php")
    root = os.environ.get("WEEWX_PHP_ROOT")
    if not php or not root:
        pytest.skip("set WEEWX_PHP_ROOT and install PHP with SQLite3")
    root = Path(root)
    config = tmp_path / "php.conf"
    data = tmp_path / "php-data"
    config.write_text(
        f"data_dir = {data.as_posix()}\ntimezone = UTC\n"
        "[Ingest]\nenabled = true\ntick_mode = external\ntrusted_proxies = 127.0.0.1\n"
    )

    def cli(*args):
        result = subprocess.run(
            [php, str(root / "bin/weewx-php"), "--config", str(config), "ingest", *args],
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

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

    with (tmp_path / "php-server.log").open("w") as log:
        process = subprocess.Popen(
            [php, "-S", f"127.0.0.1:{port}", "-t", str(root / "public")],
            env={**os.environ, "WEEWX_PHP_CONF": str(config)},
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            for _ in range(100):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                pytest.fail("PHP server startup failed")
            tls_server["backend"]["send"] = backend
            yield cli, data
        finally:
            tls_server["backend"].pop("send", None)
            process.terminate()
            process.wait(timeout=10)
