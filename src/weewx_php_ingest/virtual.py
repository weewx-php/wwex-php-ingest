"""A measurement-free LOOP source for instance-local WeeWX services."""

import math
import threading
import time

import weewx
from weewx.drivers import AbstractConfEditor, AbstractDevice

from .config import ConfigError

DRIVER_NAME = "Virtual"
DRIVER_MODULE = "weewx_php_ingest.virtual"


def options(section):
    try:
        interval = float(section.get("loop_interval", 10))
        units = section.get("unit_system", "METRICWX")
        if not math.isfinite(interval) or not 0.1 <= interval <= 60:
            raise ValueError
        if units not in ("US", "METRIC", "METRICWX"):
            raise ValueError
    except (AttributeError, TypeError, ValueError) as exc:
        raise ConfigError("invalid Virtual settings") from exc
    return interval, getattr(weewx, units)


def loader(config_dict, engine):
    section = config_dict[config_dict["Station"]["station_type"]]
    interval, units = options(section)
    return Virtual(interval, units)


class Virtual(AbstractDevice):
    hardware_name = DRIVER_NAME

    def __init__(self, loop_interval=10, unit_system=weewx.METRICWX):
        self.loop_interval = loop_interval
        self.unit_system = unit_system
        self.closed = threading.Event()

    def genLoopPackets(self):
        while not self.closed.is_set():
            # A new dictionary on every tick prevents stale service fields from leaking.
            yield {"dateTime": int(time.time()), "usUnits": self.unit_system}
            self.closed.wait(self.loop_interval)

    def getTime(self):
        return int(time.time())

    def closePort(self):
        self.closed.set()


def confeditor_loader():
    return VirtualConfEditor()


class VirtualConfEditor(AbstractConfEditor):
    @property
    def default_stanza(self):
        return """[Virtual]
    driver = weewx_php_ingest.virtual
    loop_interval = 10
    unit_system = METRICWX
"""

    def prompt_for_settings(self):
        return {
            "loop_interval": self._prompt("loop_interval", "10"),
            "unit_system": self._prompt("unit_system", "METRICWX", ("US", "METRIC", "METRICWX")),
        }
