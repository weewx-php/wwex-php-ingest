"""Independent subprocesses, bounded shutdown and cadence watchdogs."""

import importlib
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .runtime import driver_config
from .sensor_sources import MODULES
from .spool import Spool, spool_path
from .uploader import backoff

log = logging.getLogger(__name__)


def open_spools(config, *, sensors=True):
    spools = []
    devices = set()
    try:
        for station in config.stations:
            cfg, module = driver_config(station)
            if module in MODULES.values():
                from .config import ConfigError

                settings = importlib.import_module(module).settings(
                    cfg[cfg["Station"]["station_type"]]
                )
                # A UDP port has one listener, even with distinct hub filters.
                device = (module, settings[1] if module.endswith("weatherflow") else settings[0])
                if device in devices:
                    raise ConfigError("configure each receiver only once")
                devices.add(device)
            spools.append(Spool(spool_path(config, station), config.collector_id, module, station))
        if sensors:
            from .sensor_sources import discover_spools

            discover_spools(config, spools)
        return spools
    except BaseException:
        for spool in spools:
            spool.close()
        raise


@dataclass
class Child:
    key: str
    python: str
    args: list
    startup_timeout: float
    silence_timeout: float
    spool: Spool
    process: subprocess.Popen | None = None
    deadline: float = 0
    next_start: float = 0
    last_seen: float | None = None
    failures: int = 0
    restarts: int = 0
    started: float = 0
    permanent: bool = False


class Supervisor:
    def __init__(self, config):
        self.config = config
        self.spools = open_spools(config, sensors=False)
        self.stop = threading.Event()
        self.stop_file = config.state_dir / "stop"
        self.children = [
            Child(
                s.station.key,
                s.station.python,
                ["_worker", s.station.key],
                s.station.startup_timeout,
                s.station.silence_timeout,
                s,
            )
            for s in self.spools
        ]
        self.children.append(
            Child(
                "uploader",
                sys.executable,
                ["_upload"],
                config.timeout * 3 + 30,
                config.timeout * 3 + 30,
                self.spools[0],
            )
        )

    def _start(self, child, now):
        env = os.environ.copy()
        if child.key != "uploader" and self.config.token_env:
            env.pop(self.config.token_env, None)
        package_root = str(Path(__file__).resolve().parent.parent)
        # Preserve editable/source execution; alternate venvs must have dependencies installed.
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_root, env.get("PYTHONPATH")]))
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        child.process = subprocess.Popen(
            [
                child.python,
                "-m",
                "weewx_php_ingest",
                "--config",
                str(self.config.path),
                *child.args,
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )
        child.started = now
        child.deadline = now + child.startup_timeout
        child.last_seen = child.spool.get_meta(
            "uploader_heartbeat" if child.key == "uploader" else "last_collected"
        )
        if child.key != "uploader":
            child.spool.set_meta("worker_pid", child.process.pid)
            child.spool.set_meta("worker_restarts", child.restarts)
        log.info("%s started (pid %s)", child.key, child.process.pid)

    def poll(self, now=None):
        now = time.monotonic() if now is None else now
        for child in self.children:
            if child.process is not None:
                code = child.process.poll()
                if code is None:
                    heartbeat = child.spool.get_meta(
                        "uploader_heartbeat" if child.key == "uploader" else "last_collected"
                    )
                    if heartbeat != child.last_seen:
                        child.last_seen = heartbeat
                        child.deadline = now + child.silence_timeout
                        if now - child.started > max(60, child.silence_timeout):
                            child.failures = 0
                    full = (
                        child.key != "uploader"
                        and child.spool.get_meta("collection_state") == "spool_full"
                        and not child.spool.can_fit(child.spool.get_meta("blocked_bytes", 0))
                    )
                    if full:
                        child.deadline = now + child.silence_timeout
                    if now > child.deadline:
                        kill_child(child.process)
                        child.process.wait(timeout=5)
                        log.error("%s: silence_timeout", child.key)
                        if child.key != "uploader":
                            child.spool.set_meta("collection_error", "silence_timeout")
                            child.spool.set_meta("collection_state", "restarting")
                    continue
                if os.name != "nt":
                    kill_child(child.process)  # Reap descendants after an abrupt worker exit.
                child.process = None
                child.permanent = code == 65
                child.failures += 1
                child.restarts += 1
                child.next_start = now + backoff(child.failures, 1, 300)
                log.error("%s exited (%s)", child.key, code)
                if child.key != "uploader":
                    child.spool.set_meta("worker_pid", None)
                    if code != 65:
                        child.spool.set_meta("collection_state", "restarting")
                        child.spool.set_meta("collection_error", f"worker_exit_{code}")
            if child.process is None and not child.permanent and now >= child.next_start:
                try:
                    self._start(child, now)
                except OSError:
                    child.failures += 1
                    child.next_start = now + backoff(child.failures, 1, 300)
                    log.error("%s: process_start_failed", child.key)

    def run(self):
        self.stop_file.unlink(missing_ok=True)
        previous = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: self.stop.set())
        try:
            while not self.stop.is_set():
                self.poll()
                self.stop.wait(0.25)
        finally:
            self.shutdown()
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def shutdown(self):
        self.stop_file.touch()
        deadline = time.monotonic() + self.config.shutdown_timeout
        for child in self.children:
            if child.process and child.process.poll() is None and os.name != "nt":
                child.process.terminate()
        for child in self.children:
            if child.process:
                try:
                    child.process.wait(timeout=max(0.01, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    kill_child(child.process)
                    child.process.wait(timeout=5)
        for spool in self.spools:
            spool.set_meta("worker_pid", None)
            spool.set_meta("collection_state", "stopped")
            spool.close()


def kill_child(process):
    if os.name == "nt":
        # SDR is installed on Linux; taskkill also reaps probe/worker descendants on Windows.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
