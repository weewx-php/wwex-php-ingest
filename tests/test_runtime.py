import dataclasses
import importlib
import sys
import time

import pytest
import weewx

from weewx_php_ingest.locking import AlreadyRunning, ProcessLock
from weewx_php_ingest.runtime import SERVICE_GROUPS, create_engine, driver_config
from weewx_php_ingest.supervisor import Supervisor, open_spools


def wait_for(supervisor, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        supervisor.poll()
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition timed out")


def test_real_simulators_independent_processes_survive_one_crash(make_config):
    cfg = make_config()
    supervisor = Supervisor(cfg)
    # Network delivery is exercised separately over TLS; keep these observations in the spool.
    supervisor.children = supervisor.children[:2]
    try:
        wait_for(supervisor, lambda: all(s.status()["events"] >= 3 for s in supervisor.spools))
        a, b = supervisor.children
        first_pid, other_pid = a.process.pid, b.process.pid
        identities = [s.station_id for s in supervisor.spools]
        assert len(set(identities)) == 2
        count = supervisor.spools[1].status()["events"]
        a.process.kill()
        a.process.wait(timeout=3)
        wait_for(
            supervisor,
            lambda: (
                a.process is not None
                and a.process.pid != first_pid
                and supervisor.spools[0].status()["events"] >= 5
            ),
        )
        assert b.process.pid == other_pid
        assert supervisor.spools[1].status()["events"] > count
        assert [s.station_id for s in supervisor.spools] == identities
    finally:
        supervisor.shutdown()


def test_blocked_driver_watchdog_restarts_only_silent_process(make_config):
    cfg = make_config()
    path = cfg.stations[0].config
    path.write_text(path.read_text().replace("loop_interval=0.1", "loop_interval=600"))
    cfg = dataclasses.replace(
        cfg, stations=(dataclasses.replace(cfg.stations[0], startup_timeout=1), cfg.stations[1])
    )
    supervisor = Supervisor(cfg)
    supervisor.children = supervisor.children[:2]
    try:
        wait_for(supervisor, lambda: all(c.process is not None for c in supervisor.children))
        other_pid = supervisor.children[1].process.pid
        wait_for(supervisor, lambda: supervisor.children[0].restarts >= 1, timeout=6)
        assert supervisor.children[1].process.pid == other_pid
        assert supervisor.spools[1].status()["events"] > 1
    finally:
        supervisor.shutdown()


def test_callbacks_lifecycle_extensions_and_no_default_services(make_config, tmp_path):
    cfg = make_config(count=1)
    user = tmp_path / "extensions" / "user"
    user.mkdir(parents=True)
    (user / "__init__.py").write_text("")
    (user / "fixture_driver.py").write_text("""
import weewx
from weewx.drivers import AbstractDevice
def loader(config, engine):
    return Device(config, engine)
class Device(AbstractDevice):
    hardware_name = "Callback fixture"
    def __init__(self, config, engine):
        self.engine = engine
        self.gust = 0
        self.periods = 0
        self.closed = False
        engine.bind(weewx.STARTUP, self.startup)
        engine.bind(weewx.END_ARCHIVE_PERIOD, self.end)
    def startup(self, event):
        self.engine.bind(weewx.NEW_LOOP_PACKET, self.loop)
    def loop(self, event):
        self.gust = max(self.gust, event.packet['windSpeed'])
        event.packet['windGust'] = self.gust
    def end(self, event):
        self.periods += 1
        self.gust = 0
    def getTime(self):
        return 1000
    def genLoopPackets(self):
        yield {'dateTime':1001, 'usUnits':17, 'windSpeed':10, 'rain':0.2, 'extraTemp1':None}
        yield {'dateTime':1003, 'usUnits':17, 'windSpeed':3, 'rain':0}
    def closePort(self):
        self.closed = True
""")
    station = cfg.stations[0]
    station.config.write_text(
        station.config.read_text().replace("weewx.drivers.simulator", "user.fixture_driver")
    )
    sys.path.insert(0, str(user.parent))
    importlib.invalidate_caches()
    spools = open_spools(cfg)
    engine = None
    try:
        raw, _ = driver_config(station)
        assert set(raw["Engine"]["Services"]) == set(SERVICE_GROUPS)
        assert all(not v for v in raw["Engine"]["Services"].values())
        engine = create_engine(raw, station, spools[0], lambda: spools[0].status()["events"] == 2)
        with pytest.raises(weewx.StopNow):
            engine.run()
        assert engine.console.closed
        import json

        packets = [json.loads(r["payload"]) for r in spools[0].candidates(time.time(), 2)]
        assert all(p["data"]["windGust"] == 10 for p in packets)
        assert packets[0]["data"]["extraTemp1"] is None
        assert engine.service_obj == []
    finally:
        if engine:
            engine.shutDown()
        for spool in spools:
            spool.close()
        sys.path.remove(str(user.parent))
        for module in ("user.fixture_driver", "user"):
            sys.modules.pop(module, None)


def test_process_lock_prevents_duplicate_collectors(tmp_path):
    with ProcessLock(tmp_path / "collector.lock"):
        with pytest.raises(AlreadyRunning), ProcessLock(tmp_path / "collector.lock"):
            pass
    with ProcessLock(tmp_path / "collector.lock"):
        pass


def test_full_spool_pauses_one_worker_and_resumes_without_restart(make_config):
    cfg = make_config(max_events=2)
    # Only s0 has the small quota; s1 must keep collecting.
    text = cfg.path.read_text()
    prefix, second = text.split("[stations.s1]")
    cfg.path.write_text(
        prefix + "[stations.s1]" + second.replace("max_events = 2", "max_events = 200")
    )
    from weewx_php_ingest.config import load_config

    cfg = load_config(cfg.path)
    supervisor = Supervisor(cfg)
    supervisor.children = supervisor.children[:2]
    try:
        a, b = supervisor.spools
        wait_for(supervisor, lambda: a.get_meta("collection_state") == "spool_full")
        pid = supervisor.children[0].process.pid
        wait_for(supervisor, lambda: b.status()["events"] > 8)
        assert a.status()["events"] == 2
        first = a.candidates(time.time(), 1)[0]
        collected = a.get_meta("last_collected")
        a.acknowledge(first["event_id"], time.time())
        wait_for(supervisor, lambda: a.get_meta("last_collected") > collected)
        assert supervisor.children[0].process.pid == pid
        assert a.status()["events"] == 2
    finally:
        supervisor.shutdown()


def test_supervisor_collects_and_uploads_in_separate_processes(make_config, tls_server):
    from weewx_php_ingest.config import load_config

    cfg = make_config(endpoint=tls_server["url"])
    cfg.path.write_text(
        cfg.path.read_text().replace(
            "[collector]", f'[collector]\nca_file = "{tls_server["ca"].as_posix()}"'
        )
    )
    cfg = load_config(cfg.path)
    supervisor = Supervisor(cfg)
    try:
        wait_for(supervisor, lambda: len(tls_server["receipts"]) >= 6)
        assert len({c.process.pid for c in supervisor.children}) == 3
        assert {p["station_id"] for p in tls_server["receipts"].values()} == {
            s.station_id for s in supervisor.spools
        }
        assert all(s.get_meta("last_success") for s in supervisor.spools)
    finally:
        supervisor.shutdown()


def test_lifecycle_dispatches_period_end_and_releases_loop(make_config):
    import weewx.engine

    cfg = make_config(count=1)
    spool = open_spools(cfg)[0]
    raw, _ = driver_config(cfg.stations[0])
    engine = create_engine(raw, cfg.stations[0], spool)
    events = []
    engine.console.getTime = lambda: 1000
    for kind in (weewx.PRE_LOOP, weewx.END_ARCHIVE_PERIOD, weewx.POST_LOOP):
        engine.bind(kind, lambda event: events.append(event.event_type))
    try:
        engine.dispatchEvent(weewx.Event(weewx.STARTUP))
        engine.dispatchEvent(weewx.Event(weewx.PRE_LOOP))
        p = {"dateTime": 1003, "usUnits": 17, "rain": 0.2}
        engine.dispatchEvent(weewx.Event(weewx.NEW_LOOP_PACKET, packet=p))
        with pytest.raises(weewx.engine.BreakLoop):
            engine.dispatchEvent(weewx.Event(weewx.CHECK_LOOP, packet=p))
        engine.dispatchEvent(weewx.Event(weewx.POST_LOOP))
        engine.dispatchEvent(weewx.Event(weewx.PRE_LOOP))
        assert events == [weewx.PRE_LOOP, weewx.END_ARCHIVE_PERIOD, weewx.POST_LOOP, weewx.PRE_LOOP]
        assert spool.status()["events"] == 1
    finally:
        engine.shutDown()
        spool.close()
