import dataclasses
import json

import pytest
from conftest import packet

from weewx_php_ingest.protocol import encode
from weewx_php_ingest.transport import HTTPSClient, Response, TransportError, retry_after_seconds
from weewx_php_ingest.uploader import Uploader


class Client:
    def __init__(self, statuses=None, code=200):
        self.statuses = statuses or ["stored"]
        self.code = code
        self.requests = []

    def send(self, body):
        request = json.loads(body)
        self.requests.append(request)
        results = []
        for index, p in enumerate(request["packets"]):
            status = self.statuses[index % len(self.statuses)]
            results.append(
                {
                    "station_id": p["station_id"],
                    "event_id": p["event_id"],
                    "status": status,
                    "reason": "too_old" if status == "rejected" else None,
                }
            )
        return Response(self.code, encode({"version": 1, "status": "ok", "results": results}))


def test_mixed_acks_release_only_confirmed_events(spools):
    cfg, rows = spools
    for _ in range(4):
        rows[0].append(packet(rows[0]))
    client = Client(["stored", "duplicate", "pending", "rejected"])
    uploader = Uploader(cfg, rows, client)
    assert uploader.tick(now=100) == 2
    assert rows[0].status()["events"] == 2
    assert rows[0].status()["quarantined"] == 1
    assert rows[0].status()["rejections"] == {"pending": 1, "too_old": 1}
    uploader.tick(now=101)
    assert len(client.requests) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 415, 429, 500, 503, 307])
def test_http_errors_retain_and_backoff(spools, status):
    cfg, rows = spools
    rows[0].append(packet(rows[0]))
    client = Client(code=status)
    uploader = Uploader(cfg, rows, client)
    assert uploader.tick(now=100) == 0
    assert rows[0].status()["events"] == 1
    uploader.tick(now=100)
    assert len(client.requests) == 1
    assert rows[0].status()["upload_error"] == f"http_{status}"


def test_fair_stations_and_old_new_backlog(spools):
    cfg, rows = spools
    cfg = dataclasses.replace(cfg, max_packets=4)
    ids = []
    for _ in range(20):
        p = packet(rows[0])
        ids.append(p["event_id"])
        rows[0].append(p)
    rows[1].append(packet(rows[1]))
    client = Client()
    uploader = Uploader(cfg, rows, client)
    uploader.tick(now=100)
    batch = client.requests[0]["packets"]
    assert {x["station_id"] for x in batch} == {s.station_id for s in rows}
    assert {ids[0], ids[-1]} <= {x["event_id"] for x in batch}


def test_413_splits_without_changing_ids(spools):
    cfg, rows = spools
    for _ in range(4):
        rows[0].append(packet(rows[0]))
    client = Client(code=413)
    uploader = Uploader(cfg, rows, client)
    uploader.tick(now=100)
    assert uploader.packet_limit == 2
    client.code = 200
    uploader.tick(now=200)
    original = {p["event_id"]: p for p in client.requests[0]["packets"]}
    assert len(client.requests[1]["packets"]) == 2
    assert all(p == original[p["event_id"]] for p in client.requests[1]["packets"])


def test_https_commit_lost_ack_then_duplicate(spools, tls_server):
    cfg, rows = spools
    cfg = dataclasses.replace(cfg, endpoint=tls_server["url"], ca_file=tls_server["ca"])
    original = packet(rows[0])
    rows[0].append(original)
    tls_server["behaviors"].append("lost")
    uploader = Uploader(cfg, rows, HTTPSClient(cfg))
    assert uploader.tick(now=100) == 0
    assert rows[0].status()["events"] == 1
    # Recreate the uploader to exercise durable scheduling and resend after restart.
    uploader = Uploader(cfg, rows, HTTPSClient(cfg))
    assert uploader.tick(now=200) == 1
    assert rows[0].status()["events"] == 0
    assert list(tls_server["receipts"].values()) == [original]
    assert tls_server["requests"][0][2] == tls_server["requests"][1][2]
    assert tls_server["requests"][0][0] == "/weather/ingest/weewx.php"
    assert "X-WeeWX-Token" not in tls_server["requests"][0][1]


def test_https_refuses_untrusted_cert_and_does_not_follow_redirect(spools, tls_server):
    cfg, rows = spools
    cfg = dataclasses.replace(cfg, endpoint=tls_server["url"])
    body = encode({"packets": [packet(rows[0])]})
    with pytest.raises(TransportError):
        HTTPSClient(cfg).send(body)
    cfg = dataclasses.replace(cfg, ca_file=tls_server["ca"], token_header="X-WeeWX-Token")
    tls_server["behaviors"].append("redirect")
    assert HTTPSClient(cfg).send(body).status == 307
    assert len(tls_server["requests"]) == 1
    headers = tls_server["requests"][0][1]
    assert "X-WeeWX-Token" in headers and "Authorization" not in headers


def test_retry_after_supports_date_and_seconds():
    assert retry_after_seconds("60", 0) == 60
    assert retry_after_seconds("Thu, 01 Jan 1970 00:02:00 GMT", 60) == 60
    assert retry_after_seconds("invalid", 0) == 0


def test_pending_admission_also_backs_off_new_readings(spools):
    cfg, rows = spools
    rows[0].append(packet(rows[0]))
    client = Client(["pending"])
    uploader = Uploader(cfg, rows, client)
    uploader.tick(now=100)
    rows[0].append(packet(rows[0]))
    uploader.tick(now=110)
    assert len(client.requests) == 1
    # A pending station does not prevent an admitted station from sending.
    rows[1].append(packet(rows[1]))
    client.statuses = ["stored"]
    assert uploader.tick(now=111) == 1


def test_single_packet_batches_alternate_backlog_and_fresh(spools):
    cfg, rows = spools
    cfg = dataclasses.replace(cfg, max_packets=1)
    ids = [rows[0].append(packet(rows[0])) for _ in range(20)]
    client = Client()
    uploader = Uploader(cfg, rows, client)
    uploader.tick(now=100)
    uploader.tick(now=101)
    assert [r["packets"][0]["event_id"] for r in client.requests] == [ids[0], ids[-1]]


def test_malformed_duplicate_ack_never_deletes_data(spools):
    cfg, rows = spools
    p = packet(rows[0])
    rows[0].append(p)
    result = {"event_id": p["event_id"], "station_id": p["station_id"], "status": "stored"}

    class BadClient:
        def send(self, body):
            return Response(
                200, encode({"version": 1, "status": "ok", "results": [result, result]})
            )

    assert Uploader(cfg, rows, BadClient()).tick(now=100) == 0
    assert rows[0].status()["events"] == 1


def test_byte_bound_and_oversized_single_event_retained(spools):
    cfg, rows = spools
    cfg = dataclasses.replace(cfg, max_bytes=1024)
    rows[0].append(packet(rows[0], **{f"custom{i}": i for i in range(200)}))
    for _ in range(10):
        rows[0].append(packet(rows[0]))
    client = Client()
    Uploader(cfg, rows, client).tick(now=100)
    assert len(encode(client.requests[0])) <= 1024
    assert rows[0].status()["rejections"] == {"payload_too_large": 1}
