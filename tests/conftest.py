import json
import ssl
import threading
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from weewx_php_ingest.config import load_config
from weewx_php_ingest.protocol import event_from_loop
from weewx_php_ingest.supervisor import open_spools


@pytest.fixture
def make_config(tmp_path):
    def make(count=2, endpoint="https://localhost/ingest/weewx.php", **overrides):
        token = tmp_path / "collector.token"
        token.write_text("a" * 64)
        token.chmod(0o600)
        content = [
            "[collector]",
            f'id = "{uuid.uuid4()}"',
            f'endpoint = "{endpoint}"',
            'token_file = "collector.token"',
            'state_dir = "state"',
            "send_interval = 0.1",
            "timeout = 2",
            "shutdown_timeout = 2",
        ]
        for i in range(count):
            path = tmp_path / f"station{i}.conf"
            path.write_text(
                "[Station]\nstation_type=Simulator\nlatitude=0\nlongitude=0\n"
                "altitude=0,meter\n[Simulator]\ndriver=weewx.drivers.simulator\n"
                "loop_interval=0.1\nobservations=outTemp,rain,windSpeed,windDir\n"
            )
            content += [
                f"[stations.s{i}]",
                f'config = "station{i}.conf"',
                "min_free_bytes=0",
                "startup_timeout=10",
                "silence_timeout=2",
                "lifecycle_interval=2",
                "lifecycle_delay=1",
            ]
            content += [f"{key} = {json.dumps(value)}" for key, value in overrides.items()]
        config_path = tmp_path / "collector.toml"
        config_path.write_text("\n".join(content))
        return load_config(config_path)

    return make


@pytest.fixture
def spools(make_config):
    cfg = make_config()
    rows = open_spools(cfg)
    yield cfg, rows
    for row in rows:
        row.close()


def packet(spool, timestamp=1788600000, **data):
    return event_from_loop(
        {"dateTime": timestamp, "usUnits": 17, "rain": 0.2, "outTemp": None, **data},
        spool.station_id,
        spool.get_meta("driver_module"),
    )


@pytest.fixture
def tls_server(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "ca.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    receipts, requests, behaviors = {}, [], []
    backend = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, dict(self.headers), payload))
            behavior = behaviors.pop(0) if behaviors else "ok"
            if behavior == "redirect":
                self.send_response(307)
                self.send_header("Location", "https://localhost/other")
                self.end_headers()
                return
            results = []
            for p in payload["packets"]:
                status = "duplicate" if p["event_id"] in receipts else "stored"
                receipts.setdefault(p["event_id"], p)
                results.append(
                    {"event_id": p["event_id"], "station_id": p["station_id"], "status": status}
                )
            status_code = 200
            body = json.dumps({"version": 1, "status": "ok", "results": results}).encode()
            if "send" in backend:
                status_code, body = backend["send"](payload, dict(self.headers))
            if behavior == "lost":
                self.close_connection = True
                return
            self.send_response(status_code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {
        "url": f"https://localhost:{server.server_port}/weather/ingest/weewx.php",
        "ca": cert_path,
        "receipts": receipts,
        "requests": requests,
        "behaviors": behaviors,
        "backend": backend,
    }
    server.shutdown()
    server.server_close()
    thread.join(3)
