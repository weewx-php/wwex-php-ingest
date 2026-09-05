import json
import uuid

import pytest

from weewx_php_ingest.protocol import ProtocolError, decode_response, encode, event_from_loop


def event(**data):
    return event_from_loop(
        {"dateTime": 1234567890, "usUnits": 17, "rain": 0.2, **data},
        str(uuid.uuid4()),
        "user.example",
    )


@pytest.mark.parametrize("units", [1, 16, 17])
def test_preserves_units_nulls_values_and_distinct_equal_events(units):
    a = event(usUnits=units, customTemp=None, windGust=35)
    b = event(usUnits=units, customTemp=None, windGust=35)
    assert a["event_id"] != b["event_id"]
    assert json.loads(encode(a))["data"] == {"rain": 0.2, "customTemp": None, "windGust": 35}
    assert a["usUnits"] == units


@pytest.mark.parametrize(
    "data",
    [
        {"dateTime": 1.0},
        {"dateTime": True},
        {"dateTime": 0},
        {"usUnits": True},
        {"usUnits": 2},
        {"outTemp": float("nan")},
        {"outTemp": float("inf")},
        {"outTemp": True},
        {"outTemp": "20"},
        {"outTemp": []},
        {"SOURCE": 1},
        {"bad-name": 1},
        {"interval": 5},
    ],
)
def test_invalid_source_events_fail_loudly(data):
    with pytest.raises(ProtocolError):
        event(**data)


def test_only_explicit_exclusion_removes_driver_metadata():
    p = event_from_loop(
        {"dateTime": 1, "usUnits": 1, "source": "sensor", "rain": 0},
        str(uuid.uuid4()),
        "user.test",
        ["source"],
    )
    assert p["data"] == {"rain": 0}


def test_ack_matches_identity_not_position_and_missing_is_unconfirmed():
    a, b = event(), event()
    response = {
        "version": 1,
        "status": "ok",
        "results": [{"event_id": b["event_id"], "station_id": b["station_id"], "status": "stored"}],
    }
    results, _ = decode_response(encode(response), [a, b])
    assert a["event_id"] not in results
    assert results[b["event_id"]]["status"] == "stored"
    response["results"][0]["station_id"] = a["station_id"]
    with pytest.raises(ProtocolError):
        decode_response(encode(response), [a, b])


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b"<html>OK</html>",
        b'{"version":1,"version":1,"status":"ok","results":[]}',
        b'{"version":true,"status":"ok","results":[]}',
        b'{"version":1,"status":"ok","results":[null]}',
    ],
)
def test_malformed_ack_is_never_acceptance(body):
    with pytest.raises(ProtocolError):
        decode_response(body, [event()])
