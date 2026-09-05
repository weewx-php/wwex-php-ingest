# Copyright 2025 by John A Kline <john@johnkline.com>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

"""
WeeWX module that records AirGradient air quality sensor readings.

AirGradient's local API is documented here:
https://github.com/airgradienthq/arduino/blob/master/docs/local-server.md
"""

import datetime
import json
import logging
import math
import requests
import sys
import threading
import time

from dateutil import tz
from dateutil.parser import parse
from dateutil.parser import ParserError

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Type

import weeutil.logger
import weeutil.weeutil
import weewx
import weewx.accum
import weewx.units
import weewx.xtypes

from weewx.units import ValueTuple
from weeutil.weeutil import timestamp_to_string
from weeutil.weeutil import to_bool
from weeutil.weeutil import to_float
from weeutil.weeutil import to_int
from weewx.engine import StdService

log = logging.getLogger(__name__)

WEEWX_AIRGRADIENT_VERSION = "4.0"

if sys.version_info[0] < 3 or (sys.version_info[0] == 3 and sys.version_info[1] < 9):
    raise weewx.UnsupportedFeature(
        "weewx-airgradient requires Python 3.9 or later, found %s.%s" % (sys.version_info[0], sys.version_info[1]))

def weewx_version_at_least(minimum: Tuple[int, ...]) -> bool:
    """Is the running WeeWX at least `minimum` (e.g. (4, 6))?

    Compared as integers, not as text: WeeWX 4.10 sorts BELOW "4.6" as a
    string, so a plain comparison would reject the whole 4.10 series (the
    last of WeeWX 4).  weeutil's own version_compare cannot be used here --
    it arrived after 4.6, so it is missing from some of the versions this
    has to reject.
    """
    running = []
    for chunk in weewx.__version__.split('.')[:len(minimum)]:
        digits = ''
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        running.append(int(digits) if digits else 0)
    return tuple(running) >= minimum

# The demo skin's template uses $lang and $gettext, which arrived in WeeWX
# 4.6.0.  Below that they are simply undefined, and #errorCatcher Echo
# renders them into the page verbatim -- a visibly broken page, not an
# English fallback -- so 4.6 is the floor for the whole extension.
if not weewx_version_at_least((4, 6)):
    raise weewx.UnsupportedFeature(
        "weewx-airgradient requires WeeWX 4.6 or later, found %s" % weewx.__version__)

# Set up observation types not in weewx.units

weewx.units.USUnits['air_quality_index']       = 'aqi'
weewx.units.MetricUnits['air_quality_index']   = 'aqi'
weewx.units.MetricWXUnits['air_quality_index'] = 'aqi'

weewx.units.USUnits['tvoc_index']       = 'tvoc_index'
weewx.units.MetricUnits['tvoc_index']   = 'tvoc_index'
weewx.units.MetricWXUnits['tvoc_index'] = 'tvoc_index'

weewx.units.USUnits['nox_index']       = 'nox_index'
weewx.units.MetricUnits['nox_index']   = 'nox_index'
weewx.units.MetricWXUnits['nox_index'] = 'nox_index'

weewx.units.USUnits['air_quality_color']       = 'aqi_color'
weewx.units.MetricUnits['air_quality_color']   = 'aqi_color'
weewx.units.MetricWXUnits['air_quality_color'] = 'aqi_color'

weewx.units.default_unit_label_dict['aqi']  = ' AQI'
weewx.units.default_unit_label_dict['aqi_color'] = ' RGB'
weewx.units.default_unit_label_dict['tvoc_index']  = ' TVOC Index'
weewx.units.default_unit_label_dict['nox_index']  = ' NOx Index'

weewx.units.default_unit_format_dict['aqi']  = '%d'
weewx.units.default_unit_format_dict['aqi_color'] = '%d'
weewx.units.default_unit_format_dict['tvoc_index'] = '%d'
weewx.units.default_unit_format_dict['nox_index'] = '%d'

weewx.units.obs_group_dict['pm2_5_aqi'] = 'air_quality_index'
weewx.units.obs_group_dict['pm2_5_aqi_color'] = 'air_quality_color'
weewx.units.obs_group_dict['tvocIndex'] = 'tvoc_index'
weewx.units.obs_group_dict['tvoc'] = 'group_concentration'
weewx.units.obs_group_dict['noxIndex'] = 'nox_index'
weewx.units.obs_group_dict['nox'] = 'group_concentration'

class Source:
    def __init__(self, config_dict, name, is_proxy):
        self.is_proxy = is_proxy
        # Raise KeyEror if name not in dictionary.
        source_dict = config_dict[name]
        self.enable = to_bool(source_dict.get('enable', False))
        self.hostname = source_dict.get('hostname', '')
        if is_proxy:
            self.port = to_int(source_dict.get('port', 8080))
            # A proxy answers out of its own database on the local network,
            # and this timeout also bounds the archive backfill, which runs on
            # weewx's main thread once per archive record.  A proxy that has
            # not answered in a second is down.
            self.timeout = to_int(source_dict.get('timeout', 1))
        else:
            self.port = to_int(source_dict.get('port', 80))
            # A monitor's own processor is slow and easily overwhelmed, so
            # give it more room than a proxy.
            self.timeout = to_int(source_dict.get('timeout', 15))

@dataclass
class Reading:
    measurementTime : datetime.datetime # time of reading
    serialno        : str             # Serial Number of the monitor
    wifi            : Optional[float] # WiFi signal strength
    pm01            : Optional[float] # PM1.0 in ug/m3 (atmospheric environment)
    pm02            : Optional[float] # PM2.5 in ug/m3 (atmospheric environment)
    pm10            : Optional[float] # PM10 in ug/m3 (atmospheric environment)
    pm02Compensated : Optional[float] # PM2.5 in ug/m3 with correction applied (from fw version 3.1.4 onwards)
    pm01Standard    : Optional[float] # PM1.0 in ug/m3 (standard particle)
    pm02Standard    : Optional[float] # PM2.5 in ug/m3 (standard particle)
    pm10Standard    : Optional[float] # PM10 in ug/m3 (standard particle)
    rco2            : Optional[float] # CO2 in ppm
    pm003Count      : Optional[float] # Particle count 0.3um per dL
    pm005Count      : Optional[float] # Particle count 0.5um per dL
    pm01Count       : Optional[float] # Particle count 1.0um per dL
    pm02Count       : Optional[float] # Particle count 2.5um per dL
    pm50Count       : Optional[float] # Particle count 5.0um per dL (only for indoor monitor)
    pm10Count       : Optional[float] # Particle count 10um per dL (only for indoor monitor)
    atmp            : Optional[float] # Temperature in Degrees Celsius
    atmpCompensated : Optional[float] # Temperature in Degrees Celsius with correction applied
    rhum            : Optional[float] # Relative Humidity
    rhumCompensated : Optional[float] # Relative Humidity with correction applied
    tvocIndex       : Optional[float] # Sensirion VOC Index
    tvocRaw         : Optional[float] # VOC raw value
    noxIndex        : Optional[float] # Sensirion NOx Index
    noxRaw          : Optional[float] # NOx raw value
    boot            : Optional[int  ] # Counts every measurement cycle. Low boot counts indicate restarts.
    bootCount       : Optional[int  ] # Same as boot property. Required for Home Assistant compatability. (deprecated soon!)
    ledMode         : Optional[str  ] # Current configuration of the LED mode
    firmware        : Optional[str  ] # Current firmware version
    model           : Optional[str  ] # Current model name

# What an airgradient-proxy's /fetch-two-minute-record covers: an average of
# the last two minutes of readings.  It can stand in for an archive period
# that closed within that span, and for no other -- see
# AirGradient.backfill_values.
TWO_MINUTE_AVERAGE_SECS: int = 120

@dataclass
class Configuration:
    lock          : threading.Lock
    reading       : Optional[Reading] # Controlled by lock
    poll_secs     : int               # Immutable
    fresh_secs    : int               # Immutable
    loop_fields   : Dict[str, str]    # Immutable
    sources       : List[Source]      # Immutable
    enable_aqi    : bool              # Immutable

def datetime_from_reading(dt_str):
    # 2025-10-25T17:45:00.000Z
    dt_str = dt_str.replace('Z', 'UTC')
    tzinfos = {'CST': tz.gettz("UTC")}
    return parse(dt_str, tzinfos=tzinfos)

def utc_now():
    return datetime.datetime.now(tz=tz.gettz("UTC"))

def reraise_if_terminate(e: BaseException) -> None:
    """weewxd stops by raising Terminate from its SIGTERM signal handler --
    inside whatever the main thread is executing at that instant.  Every
    broad exception handler on a main-thread path must call this first and
    hand the exception back, or weewx cannot shut down.  weewxd runs as
    __main__, so its Terminate class cannot be imported here and is
    recognized by name."""
    if type(e).__name__ == 'Terminate':
        raise e

def get_reading(cfg: Configuration):
    for source in cfg.sources:
        if source.enable:
            reading = collect_data(source.hostname,
                                  source.port,
                                  source.timeout,
                                  source.is_proxy)
            if reading is not None:
                log.debug('get_reading: source: %s' % reading)
                age_of_reading = time.time() - reading.measurementTime.timestamp()
                # Ignore old readings.  We can't accept a reading of age
                # fresh_secs (or close to it) because the reading will age
                # out before the next poll.  Reduce fresh_secs - poll_secs
                # by 5s (as a buffer).
                if abs(age_of_reading) > (cfg.fresh_secs - cfg.poll_secs - 5.0):
                    log.info('Ignoring reading from %s:%d--age: %d seconds.' % (
                        source.hostname, source.port, age_of_reading))
                    continue
                log.debug('get_reading: reading: %s' % reading)
                return reading
    log.error('Could not get reading from any source.')
    return None

def check_type(j: Dict[str, Any], types: List[Type], names: List[str]) -> Tuple[bool, str]:
    """Check that each named field in j, if present and non-null, is an
    instance of one of types.  All fields are optional (AirGradient models
    differ in which fields they report) and JSON null is acceptable.  bool
    is never acceptable (JSON true/false parse as bool, a subclass of
    int)."""
    try:
        for name in names:
            x = j.get(name)
            if x is None:
                continue
            match_found = not isinstance(x, bool) and any(isinstance(x, t) for t in types)
            if not match_found:
                return False, '%s is not an instance of any of the following type(s): %r: %s' % (name, types, x)
        return True, ''
    except Exception as e:
        reraise_if_terminate(e)
        return False, 'check_type: exception: %s' % e

def is_sane(j: Dict[str, Any]) -> Tuple[bool, Optional[str]]:

    ok, reason = check_type(j, [str], ['measurementTime', 'serialno','ledMode','firmware', 'model'])
    if not ok:
        return False, reason

    if j.get('measurementTime') is not None:
        try:
            _ = datetime_from_reading(j['measurementTime'])
        except ParserError:
            return False, 'measurementTime could not be converted to a dateTime: %s' % j['measurementTime']

    ok, reason = check_type(j, [int], ['boot', 'bootCount'])
    if not ok:
        return False, reason

    ok, reason = check_type(j, [float,int], [ 'wifi', 'pm01','pm02', 'pm10', 'pm02Compensated',
             'pm01Standard', 'pm02Standard', 'pm10Standard', 'rco2', 'pm003Count',
             'pm005Count', 'pm01Count', 'pm02Count', 'pm50Count', 'pm10Count',
             'atmp', 'atmpCompensated', 'rhum', 'rhumCompensated', 'tvocIndex',
             'tvocRaw', 'noxIndex', 'noxRaw'])
    if not ok:
        return False, reason

    return True, None

def opt_float(j: Dict[str, Any], name: str) -> Optional[float]:
    """A field that is absent or JSON null yields None."""
    return float(j[name]) if j.get(name) is not None else None

def parse_response(hostname: str, response: requests.Response) -> Optional[Reading]:
    try:
        # convert to json
        j: Dict[str, Any] = response.json()
        return reading_from_json(hostname, j)
    except Exception as e:
        log.info('parse_response: %r raised exception %r' % (response.text, e))
        raise e

def reading_from_json(hostname: str, j: Dict[str, Any]) -> Optional[Reading]:
    """One AirGradient reading, from the json a sensor or an
    airgradient-proxy returned.  The live path reaches this through
    parse_response; the archive backfill calls it once per record an
    airgradient-proxy hands back, which carry the same field names (the
    proxy's own convert_to_json).  Returns None for a reading that is not
    sane; raises for one that cannot be parsed at all (a missing serialno)."""
    sane, reason = is_sane(j)
    if not sane:
        log.warning('airgradient reading from %s not sane, %s: %s' % (hostname, reason, j))
        return None

    # if json contains 'measurementTime', the reading is from an airgradient-proxy.
    # if missing, the reading is directly from an AirGradient sensor.  For the
    # latter case, the measurementTime is now.
    if j.get('measurementTime') is not None:
        measurementTime = datetime_from_reading(j['measurementTime'])
    else:
        measurementTime = utc_now()

    return Reading(
        measurementTime = measurementTime,
        serialno        = j['serialno'],
        wifi            = opt_float(j, 'wifi'),
        pm01            = opt_float(j, 'pm01'),
        pm02            = opt_float(j, 'pm02'),
        pm10            = opt_float(j, 'pm10'),
        pm02Compensated = opt_float(j, 'pm02Compensated'),
        pm01Standard    = opt_float(j, 'pm01Standard'),
        pm02Standard    = opt_float(j, 'pm02Standard'),
        pm10Standard    = opt_float(j, 'pm10Standard'),
        rco2            = opt_float(j, 'rco2'),
        pm003Count      = opt_float(j, 'pm003Count'),
        pm005Count      = opt_float(j, 'pm005Count'),
        pm01Count       = opt_float(j, 'pm01Count'),
        pm02Count       = opt_float(j, 'pm02Count'),
        pm50Count       = opt_float(j, 'pm50Count'),
        pm10Count       = opt_float(j, 'pm10Count'),
        atmp            = opt_float(j, 'atmp'),
        atmpCompensated = opt_float(j, 'atmpCompensated'),
        rhum            = opt_float(j, 'rhum'),
        rhumCompensated = opt_float(j, 'rhumCompensated'),
        tvocIndex       = opt_float(j, 'tvocIndex'),
        tvocRaw         = opt_float(j, 'tvocRaw'),
        noxIndex        = opt_float(j, 'noxIndex'),
        noxRaw          = opt_float(j, 'noxRaw'),
        boot            = j.get('boot'),
        bootCount       = j.get('bootCount'),
        ledMode         = j.get('ledMode'),
        firmware        = j.get('firmware'),
        model           = j.get('model'))

def collect_data(hostname, port, timeout, proxy = False) -> Optional[Reading]:

    reading: Optional[Reading] = None
    url = 'http://%s:%s/measures/current' % (hostname, port)

    try:
        # fetch data
        log.debug('collect_data: fetching from url: %s, timeout: %d' % (url, timeout))
        r = requests.get(url=url, timeout=timeout)
        r.raise_for_status()
        log.debug('collect_data: %s returned %r' % (hostname, r))
        if r:
            reading = parse_response(hostname, r)
    except Exception as e:
        reraise_if_terminate(e)
        log.info('collect_data: Attempt to fetch from: %s failed: %s.' % (hostname, e))
        reading = None

    return reading

def loop_values(reading: Reading, us_units: int, loop_fields: Dict[str, str]) -> Dict[str, Any]:
    """The fields this extension contributes for one reading, keyed by the
    weewx field names [LoopFields] maps them to.  Both the loop path and the
    archive backfill go through here, so a backfilled archive record carries
    the same quantity, in the same units, that the loop path would have put
    there.  A field the reading does not carry is absent from the result.

    us_units is the unit system of the packet or record being filled: an
    AirGradient reports temperature in Celsius, and it has to arrive in
    whatever the destination is using."""
    values: Dict[str, Any] = {}
    reading_dict = reading.__dict__
    for ag_field, weewx_field in loop_fields.items():
        if ag_field not in reading_dict or reading_dict[ag_field] is None:
            continue
        if ag_field in ['atmp', 'atmpCompensated']:
            # temperature needs to match units in the destination record
            temperature, _, _ = weewx.units.convertStd(
                (reading_dict[ag_field], 'degree_C', 'group_temperature'), us_units)
            values[weewx_field] = temperature
        else:
            # Don't have to worry about units conversion.
            values[weewx_field] = reading_dict[ag_field]
    return values

def fetch_proxy_archive_records(source: Source, since_ts: int, max_ts: int) -> Optional[List[Reading]]:
    """Ask an airgradient-proxy for the archive records it holds for
    (since_ts, max_ts].  The proxy's since_ts is exclusive and its max_ts
    inclusive, which is exactly a WeeWX archive period.

    Honors the source's configured timeout, which for a proxy is short by
    design (see install.py): the proxy answers this out of its own sqlite
    database in milliseconds, and this runs on the main thread once per
    archive record.

    Returns None if the proxy could not be asked, otherwise the sane records
    it returned -- possibly an empty list.  A proxy writes a period's record
    on its first poll at or past the boundary, and its polls are clock
    aligned, so it normally has the record a second or two after the
    boundary -- ahead of WeeWX, which archives the period at archive_delay.
    An empty answer for the period that just closed means a proxy running
    with a poll-freq-offset, or one that was down."""
    url = 'http://%s:%s/fetch-archive-records?since_ts=%d,max_ts=%d' % (
        source.hostname, source.port, since_ts, max_ts)
    try:
        log.debug('fetch_proxy_archive_records: fetching from url: %s, timeout: %d' % (url, source.timeout))
        r = requests.get(url=url, timeout=source.timeout)
        r.raise_for_status()
        j = r.json()
        if not isinstance(j, list):
            log.info('fetch_proxy_archive_records: %s returned %r, expected a list of records.' % (
                source.hostname, j))
            return None
        # Parsing stays inside the try: this runs on the main thread, where
        # anything that escapes takes weewxd down with it.  A [[ProxyN]] port
        # pointed at some other service can return well-formed json that is
        # nothing like a list of readings.
        readings: List[Reading] = []
        for entry in j:
            if not isinstance(entry, dict):
                # Skipped, not bailed on: whatever this is, it is something
                # this proxy sent, so reporting it to the caller as an
                # unreachable proxy would be a false diagnosis -- a full
                # archive interval of cooldown, and no two minute fallback
                # either, for what is a parse problem.
                log.warning('fetch_proxy_archive_records: %s returned %r, expected a reading.' % (
                    source.hostname, entry))
                continue
            # Caught per entry, so one bad record costs that record and not
            # the whole period.  reading_from_json raises on a record with no
            # serialno -- is_sane treats serialno as optional and the proxy
            # omits the field when it is NULL, so a proxy can emit such a
            # record persistently.  Letting that escape to the caller would
            # report it as an unreachable proxy: cooldown for an archive
            # interval, skipped for the two minute fallback, and a log
            # message blaming the network for a parse problem.
            try:
                reading = reading_from_json(source.hostname, entry)
            except Exception as e:
                reraise_if_terminate(e)
                log.warning('airgradient archive record from %s could not be parsed, %s: %s' % (
                    source.hostname, e, entry))
                continue
            if reading is not None:
                readings.append(reading)
    except Exception as e:
        reraise_if_terminate(e)
        log.info('fetch_proxy_archive_records: Attempt to fetch from: %s failed: %s.' % (source.hostname, e))
        return None
    return readings

def fetch_proxy_two_minute_reading(source: Source) -> Optional[Reading]:
    """The average of the last two minutes of readings an airgradient-proxy
    has taken, or None if it could not be asked or holds no such record.

    This is a SEPARATE endpoint from the /measures/current this extension
    polls: against a proxy, /measures/current answers with the single latest
    reading, and only /fetch-two-minute-record is an average.  It is fetched
    solely to stand in for an archive period that closed within the span it
    covers -- see AirGradient.backfill_values -- and so is asked for at most
    once per archive record, in a case that should almost never arise.

    The returned reading is stamped with the time of the last reading in the
    span, so it describes (measurementTime - TWO_MINUTE_AVERAGE_SECS,
    measurementTime]; the caller checks that span against the period it is
    filling.  A proxy holding no two minute record answers with an empty
    object."""
    url = 'http://%s:%s/fetch-two-minute-record' % (source.hostname, source.port)
    try:
        log.debug('fetch_proxy_two_minute_reading: fetching from url: %s, timeout: %d' % (url, source.timeout))
        r = requests.get(url=url, timeout=source.timeout)
        r.raise_for_status()
        j = r.json()
        # Parsing stays inside the try -- main thread.  An empty object is
        # the proxy's answer for "I have no two minute record"; it passes
        # is_sane (every field is optional) but has no serialno to parse.
        if not isinstance(j, dict) or not j:
            log.debug('fetch_proxy_two_minute_reading: %s returned %r.' % (source.hostname, j))
            return None
        # Caught here rather than by the handler below, which would report a
        # record this proxy answered with -- one with no serialno, say -- as
        # a failure to reach the proxy at all.
        try:
            return reading_from_json(source.hostname, j)
        except Exception as e:
            reraise_if_terminate(e)
            log.warning('airgradient two minute record from %s could not be parsed, %s: %s' % (
                source.hostname, e, j))
            return None
    except Exception as e:
        reraise_if_terminate(e)
        log.info('fetch_proxy_two_minute_reading: Attempt to fetch from: %s failed: %s.' % (source.hostname, e))
        return None

def average_readings(readings: List[Reading], us_units: int, loop_fields: Dict[str, str]) -> Dict[str, float]:
    """Average the values of the proxy archive records covering one WeeWX
    archive period.  Each reading is converted through loop_values before it
    is averaged, so the result is the average of exactly the quantities the
    loop path stores -- which is what the accumulator would have arrived at
    from loop packets.

    Non-numeric fields are skipped: [LoopFields] can map ledMode, firmware,
    model or serialno, and there is no average of those.  A field only some
    of the readings carry is averaged over the readings that carry it."""
    sums  : Dict[str, float] = {}
    counts: Dict[str, int]   = {}
    for reading in readings:
        for field, value in loop_values(reading, us_units, loop_fields).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            sums[field]   = sums.get(field, 0.0) + value
            counts[field] = counts.get(field, 0) + 1
    return {field: sums[field] / counts[field] for field in sums}


class AirGradient(StdService):
    """Collect AirGradient Air air quality measurements."""

    def __init__(self, engine, config_dict):
        super(AirGradient, self).__init__(engine, config_dict)
        log.info("Service version is %s." % WEEWX_AIRGRADIENT_VERSION)

        self.engine = engine
        self.config_dict = config_dict.get('AirGradient', {})
        self.stale_logged = False

        # The interval WeeWX actually archives on, decided the way the engine
        # decides it: under SOFTWARE record generation weewx.conf's value is
        # used and the console is ignored; under HARDWARE the console's is
        # used -- differing from weewx.conf only earns a log message -- unless
        # the driver cannot report one.
        archive_dict = config_dict.get('StdArchive', {})
        configured_interval = to_int(archive_dict.get('archive_interval', 300))
        if archive_dict.get('record_generation', 'hardware').lower() == 'hardware':
            try:
                self.archive_interval = to_int(engine.console.archive_interval)
            except (AttributeError, NotImplementedError):
                self.archive_interval = configured_interval
            # A driver that answers None would otherwise stop weewx from
            # starting, with a traceback pointing at this extension.
            if not self.archive_interval:
                self.archive_interval = configured_interval
        else:
            self.archive_interval = configured_interval

        # The times of the loop packets this extension had a fresh reading
        # for.  An archive record carries no proof of its own: under hardware
        # record generation the accumulator's values are grafted on AFTER this
        # service's handler runs, so a missing field says nothing about
        # whether the accumulator is empty.  Whether this extension was
        # feeding the accumulator does say so, and only this extension knows
        # it.  Main thread only.
        self.reading_times: List[float] = []
        # Two archive intervals is plenty to answer for the period that just
        # closed, and bounds the list.
        self.reading_retention_secs = 2 * self.archive_interval
        # A proxy that could not be reached is not asked again until this
        # time.  A startup catchup delivers its records back to back: without
        # this, an unreachable proxy would cost its whole timeout PER RECORD.
        # If it is down, it is down.
        self.proxy_retry_after: Dict[str, float] = {}

        poll_secs  = to_int(self.config_dict.get('poll_secs', 15))
        fresh_secs = max(120, 3 * poll_secs)

        enable_aqi  = to_bool(self.config_dict.get('enable_aqi', True))

        self.cfg = Configuration(
            lock          = threading.Lock(),
            reading       = None,
            poll_secs     = poll_secs,
            fresh_secs    = fresh_secs,
            loop_fields   = AirGradient.configure_loop_fields(self.config_dict),
            sources       = AirGradient.configure_sources(self.config_dict),
            enable_aqi    = enable_aqi)

        log.info('poll_secs : %d' % self.cfg.poll_secs)
        log.info('fresh_secs: %d' % self.cfg.fresh_secs)
        log.info('enable_aqi: %s' % self.cfg.enable_aqi)
        log.info('archive_interval: %d' % self.archive_interval)
        loopfield_count: int = 0
        for key in self.cfg.loop_fields:
            loopfield_count += 1
            log.info('LoopField %d: %s = %s' % (loopfield_count, key, self.cfg.loop_fields[key]))
        source_count = 0
        for source in self.cfg.sources:
            if source.enable:
                source_count += 1
                log.info(
                    'Source %d for AirGradient readings: %s %s:%s, proxy: %s, timeout: %d' % (
                    source_count, 'airgradient-proxy' if source.is_proxy else 'sensor',
                    source.hostname, source.port, source.is_proxy, source.timeout))
        if source_count == 0:
            log.error('No sources configured for airgradient extension.  AirGradient extension is inoperable.')
        else:
            if self.cfg.enable_aqi:
                weewx.xtypes.xtypes.insert(0, AQI())
                AQI.register_accumulator_extractors()

            with self.cfg.lock:
                self.cfg.reading = get_reading(self.cfg)

            # Start a thread to query proxies and make aqi available to loopdata
            dp: DevicePoller = DevicePoller(self.cfg)
            t: threading.Thread = threading.Thread(target=dp.poll_device, name='AirGradient', daemon=True)
            t.start()

            self.bind(weewx.NEW_LOOP_PACKET, self.new_loop_packet)

            # Backfilling an archive period this extension contributed nothing
            # to means asking a proxy for its archive history.  A direct sensor
            # keeps no history, so with no proxy configured there is nothing to
            # ask and the handler is not bound at all -- no fetches, no log
            # messages, nothing.
            if any(source.enable and source.is_proxy for source in self.cfg.sources):
                self.bind(weewx.NEW_ARCHIVE_RECORD, self.new_archive_record)

    def new_loop_packet(self, event):
        log.debug('new_loop_packet(%s)' % event)
        with self.cfg.lock:
            log.debug('new_loop_packet: self.cfg.reading: %s' % self.cfg.reading)
            if self.cfg.reading is not None and \
                    self.cfg.reading.measurementTime.timestamp() + self.cfg.fresh_secs >= time.time():
                if self.stale_logged:
                    log.info('Fresh reading available again.')
                    self.stale_logged = False
                log.debug('Time of reading being inserted: %s' % timestamp_to_string(self.cfg.reading.measurementTime.timestamp()))

                values = loop_values(self.cfg.reading, event.packet['usUnits'], self.cfg.loop_fields)
                for weewx_field, value in values.items():
                    log.debug('packet[%s] = %r' % (weewx_field, value))
                    event.packet[weewx_field] = value
                self.record_reading_time(event.packet)
                if self.cfg.enable_aqi:
                    # compute aqi from pm02Compensated if present, else pm02
                    pm02: Optional[float] = None
                    if self.cfg.reading.pm02Compensated is not None:
                        pm02 = self.cfg.reading.pm02Compensated
                    elif self.cfg.reading.pm02 is not None:
                        pm02 = self.cfg.reading.pm02
                    if pm02 is not None:
                        event.packet['pm2_5_aqi'] = AQI.compute_pm2_5_aqi(pm02)
                        event.packet['pm2_5_aqi_color'] = AQI.compute_pm2_5_aqi_color(event.packet['pm2_5_aqi'])
            else:
                # Log at error level once per outage, not once per loop packet.
                if not self.stale_logged:
                    log.error('Found no fresh reading to insert.')
                    self.stale_logged = True
                else:
                    log.debug('Found no fresh reading to insert.')

    def record_reading_time(self, packet: Dict[str, Any]) -> None:
        """Remember that this extension had a fresh reading for this loop
        packet -- this is what new_archive_record consults to tell a period
        the accumulator has data for from one it has nothing for.  Main thread
        only, so no lock is taken.

        Recorded once per packet, NOT per field.  Which fields that reading
        happened to carry is a property of the monitor, not of whether this
        extension was running: AirGradient models differ in what they report
        (no CO2 on an outdoor Open Air, no NOx without an SGP41), and a
        monitor that does not report a field never will.  Tallying per field
        would leave every such field looking like a gap on every archive
        record, for a proxy that cannot supply it either -- a fetch and a log
        line every archive period, forever, on a perfectly healthy install.

        The aqi fields are irrelevant here either way: they carry noop
        accumulator extractors and never reach an archive record at all.  The
        AQI xtype computes them from a stored pm2_5, which means a record this
        backfill fills gets its aqi back too."""
        # `or time.time()`, not a get() default: a packet carrying an
        # explicit dateTime of None would otherwise make to_float return None
        # and the cutoff arithmetic below raise.  new_loop_packet has no
        # handler of its own, so that TypeError would escape into
        # dispatchEvent and stop weewxd.
        ts = to_float(packet.get('dateTime') or time.time())
        self.reading_times.append(ts)
        cutoff = ts - self.reading_retention_secs
        self.reading_times = [t for t in self.reading_times if t >= cutoff]

    def saw_reading_in(self, start_ts: float, end_ts: float) -> bool:
        """Did this extension have a fresh reading to insert during
        (start_ts, end_ts]?  If it did, it was feeding the accumulator for
        that period and nothing needs backfilling: whatever the monitor
        reported is in there, and whatever it did not report is not something
        a proxy polling the same monitor can supply."""
        return any(start_ts < ts <= end_ts for ts in self.reading_times)

    def new_archive_record(self, event):
        """Fill in this extension's fields for an archive period it
        contributed nothing to -- the periods WeeWX was down for, handed over
        by the logger at startup catchup.  Bound only when a proxy source is
        configured.

        Runs on the main thread, in the data_services slot, so the record can
        still be altered: StdArchive stores it (and, for hardware records,
        grafts the accumulator's values onto the fields still missing) only
        after every data service has seen it.  Whatever is set here therefore
        survives -- which is also why a value is only ever set for a period
        this extension injected nothing into."""
        record = event.record
        end_ts = to_int(record['dateTime'])
        # The record's own interval, not the configured archive interval: on a
        # long catchup a logger's records need not fall on archive boundaries.
        # to_float, not to_int: under software record generation WeeWX sets
        # interval to archive_interval / 60, which is fractional whenever the
        # archive interval is not a whole number of minutes (90s -> 1.5), and
        # truncating it would put start_ts inside the period.  round() keeps
        # the result an int -- backfill_values takes ints and the proxy parses
        # the URL's timestamps with int() -- and absorbs float error, since an
        # archive interval of 100s gives 1.6666... * 60 = 99.999...  The `or 0`
        # covers a record carrying an explicit interval of None: this runs
        # outside the try below, on a main-thread path, so a TypeError here
        # would escape into dispatchEvent and stop weewxd.
        interval_secs = round(to_float(record.get('interval') or 0) * 60)
        if interval_secs <= 0:
            interval_secs = self.archive_interval
        start_ts = end_ts - interval_secs

        # A period this extension was feeding readings into keeps whatever
        # the accumulator made of them.
        if self.saw_reading_in(start_ts, end_ts):
            return

        # Test for None, not just for absence.  Under software record
        # generation the accumulator has already had its say by the time this
        # runs, and it writes None for a type it holds with no usable values.
        needed = [f for f in self.cfg.loop_fields.values() if record.get(f) is None]
        if not needed:
            return

        # Main thread: an exception escaping here goes up through
        # dispatchEvent and stops weewxd.  Nothing about filling in an old
        # record is worth that.
        try:
            values = self.backfill_values(start_ts, end_ts, to_int(record['usUnits']))
        except Exception as e:
            reraise_if_terminate(e)
            log.error('Could not fill %s in archive record %s: %s' % (
                ', '.join(needed), timestamp_to_string(end_ts), e))
            return
        filled = [f for f in needed if f in values]
        for weewx_field in filled:
            record[weewx_field] = values[weewx_field]
        if filled:
            log.info('Backfilled %s into archive record %s.' % (
                ', '.join(filled), timestamp_to_string(end_ts)))
        else:
            log.info('No proxy data with which to fill %s in archive record %s.' % (
                ', '.join(needed), timestamp_to_string(end_ts)))

    def backfill_values(self, start_ts: int, end_ts: int, us_units: int) -> Dict[str, float]:
        """The values for the period (start_ts, end_ts], from the first
        enabled proxy that holds archive records covering it.

        A proxy normally holds the period that just closed -- its polls are
        clock aligned, so one lands on the boundary and the record is written
        a second or two later, ahead of WeeWX's own archiving.  When none of
        them has it, ask for the proxy's two minute average instead, and take
        it only if the span it covers overlaps the period being filled.  Any
        period further back is a period that average says nothing about: if no
        proxy holds an archive record for it, its columns stay empty.  That is
        the right answer, not a defeat."""
        now = time.time()
        reachable: List[Source] = []
        for source in self.cfg.sources:
            if not source.enable or not source.is_proxy:
                continue
            key = '%s:%s' % (source.hostname, source.port)
            if now < self.proxy_retry_after.get(key, 0.0):
                continue
            readings = fetch_proxy_archive_records(source, start_ts, end_ts)
            if readings is None:
                # Unreachable.  Leave it alone until the next archive period:
                # every record in a catchup burst would otherwise wait out the
                # same timeout.
                self.proxy_retry_after[key] = now + self.archive_interval
                continue
            reachable.append(source)
            if readings:
                return average_readings(readings, us_units, self.cfg.loop_fields)
        # No proxy had an archive record for the period.  This should be rare:
        # it means a proxy running with a poll-freq-offset, or one that was
        # down for the period.  Its two minute average can still stand in, but
        # only for a period that average actually covers.
        if now - end_ts < TWO_MINUTE_AVERAGE_SECS:
            for source in reachable:
                reading = fetch_proxy_two_minute_reading(source)
                if reading is None:
                    continue
                # The reading is stamped at the end of the span it covers, so
                # it describes (ts - TWO_MINUTE_AVERAGE_SECS, ts].  That span
                # overlaps the period being filled when ts > start_ts and
                # ts - TWO_MINUTE_AVERAGE_SECS < end_ts -- and the enclosing
                # guard has already established the second half: the period
                # closed less than TWO_MINUTE_AVERAGE_SECS ago, and a proxy
                # cannot stamp a reading in the future.  So the span can only
                # miss the period by being older than it, which is what a
                # stale record from a proxy that stopped polling looks like.
                ts = reading.measurementTime.timestamp()
                if ts > start_ts:
                    return average_readings([reading], us_units, self.cfg.loop_fields)
        return {}

    @staticmethod
    def configure_loop_fields(config_dict):
        loop_fields = {}
        loop_fields_dict = config_dict.get('LoopFields', {})
        for key in loop_fields_dict:
            if not isinstance(key, str):
                log.info('keys in LoopFields must be strings that correspond to AirGradient fields, skipping this entry: %s' % key)
            elif not isinstance(loop_fields_dict[key], str):
                log.info('values in LoopFields must be strings that correspond to Loop record fields, skipping this entry: %s' % key)
            else:
                loop_fields[key] = loop_fields_dict[key]
        if not loop_fields:
            log.error("No [LoopFields] entries in weewx.conf's [AirGradient] section: "
                      "no fields will be written to loop packets.  See the README for "
                      "the suggested mapping.")
        return loop_fields

    @staticmethod
    def configure_sources(config_dict):
        sources = []
        # Configure Proxies
        idx = 0
        while True:
            idx += 1
            try:
                source = Source(config_dict, 'Proxy%d' % idx, True)
                sources.append(source)
            except KeyError:
                break
        # Configure Sensors
        idx = 0
        while True:
            idx += 1
            try:
                source = Source(config_dict, 'Sensor%d' % idx, False)
                sources.append(source)
            except KeyError:
                break

        return sources

class DevicePoller:
    def __init__(self, cfg: Configuration):
        self.cfg = cfg

    def poll_device(self) -> None:
        log.debug('poll_device: start')
        while True:
            try:
                log.debug('poll_device: calling get_reading.')
                reading = get_reading(self.cfg)
            except Exception as e:
                log.error('poll_device exception: %s' % e)
                weeutil.logger.log_traceback(log.critical, "    ****  ")
                reading = None
            log.debug('poll_device: reading: %s' % reading)
            if reading is not None:
                with self.cfg.lock:
                    self.cfg.reading = reading
            log.debug('poll_device: Sleeping for %d seconds.' % self.cfg.poll_secs)
            time.sleep(self.cfg.poll_secs)

class AQI(weewx.xtypes.XType):
    """
    AQI XType which computes the AQI (air quality index) from
    the pm2_5 value.
    """

    def __init__(self):
        pass

    @staticmethod
    def register_accumulator_extractors() -> None:
        """Tell the accumulator not to extract the loop-injected AQI fields
        into archive records.  new_loop_packet computes AQI per loop packet
        under the same names this xtype serves; without this, WeeWX's default
        avg extractor would fold a meaningless averaged AQI into the archive
        record, and $current would use it instead of the xtype during
        real-time report generation.  extractor = noop drops the fields so
        lookups fall through to the xtype -- the same pattern WeeWX's own
        defaults use for windSpeed.  A user's [Accumulator] section takes
        precedence over these entries."""
        weewx.accum.accum_dict.extend({
            'pm2_5_aqi'      : {'extractor': 'noop'},
            'pm2_5_aqi_color': {'extractor': 'noop'},
        })

    agg_sql_dict = {
        'avg': "SELECT AVG(pm2_5), MIN(usUnits) FROM %(table_name)s "
               "WHERE dateTime > %(start)s AND dateTime <= %(stop)s AND pm2_5 IS NOT NULL",
        'count': "SELECT COUNT(dateTime), MIN(usUnits) FROM %(table_name)s "
                 "WHERE dateTime > %(start)s AND dateTime <= %(stop)s AND pm2_5 IS NOT NULL",
        'first': "SELECT pm2_5, usUnits FROM %(table_name)s "
                 "WHERE dateTime = (SELECT MIN(dateTime) FROM %(table_name)s "
                 "WHERE dateTime > %(start)s AND dateTime <= %(stop)s AND pm2_5 IS NOT NULL)",
        'last': "SELECT pm2_5, usUnits FROM %(table_name)s "
                "WHERE dateTime = (SELECT MAX(dateTime) FROM %(table_name)s "
                "WHERE dateTime > %(start)s AND dateTime <= %(stop)s AND pm2_5 IS NOT NULL)",
        'min': "SELECT pm2_5, usUnits FROM %(table_name)s "
               "WHERE dateTime > %(start)s AND dateTime <= %(stop)s AND pm2_5 IS NOT NULL "
               "ORDER BY pm2_5 ASC LIMIT 1;",
        'max': "SELECT pm2_5, usUnits FROM %(table_name)s "
               "WHERE dateTime > %(start)s AND dateTime <= %(stop)s AND pm2_5 IS NOT NULL "
               "ORDER BY pm2_5 DESC LIMIT 1;",
    }

    day_boundary_avg_min_max_sql_dict = {
        'usUnits': "SELECT usUnits from %(table_name)s ORDER BY dateTime DESC LIMIT 1;",
        'avg'    : "SELECT sum(wsum) / sum(sumtime) FROM %(table_name)s%(pm2_5_summary_suffix)s "
                   "WHERE dateTime >= %(start)s AND dateTime < %(stop)s ",
        'min'    : "SELECT min FROM %(table_name)s%(pm2_5_summary_suffix)s "
                   "WHERE dateTime >= %(start)s AND dateTime < %(stop)s "
                   "ORDER BY min ASC LIMIT 1;",
        'max'    : "SELECT max FROM %(table_name)s%(pm2_5_summary_suffix)s "
                   "WHERE dateTime >= %(start)s AND dateTime < %(stop)s "
                   "ORDER BY max DESC LIMIT 1;",
    }

    @staticmethod
    def compute_pm2_5_aqi(pm2_5):
        #             U.S. EPA PM2.5 AQI
        #
        #  AQI Category  AQI Value  24-hr PM2.5
        # Good             0 -  50    0.0 -   9.0
        # Moderate        51 - 100    9.1 -  35.4
        # USG            101 - 150   35.5 -  55.4
        # Unhealthy      151 - 200   55.5 - 125.4
        # Very Unhealthy 201 - 300  125.5 - 225.4
        # Hazardous      301 - 500  225.5 - 325.4
        #
        # Concentrations above 325.4 map to AQI values above 500, continuing
        # on the Hazardous slope (May 2024 AirNow TAD, breakpoint-table
        # footnote 4 and the "AQI values above 500" FAQ).

        # The EPA standard for AQI says to truncate PM2.5 to one decimal place.
        # See https://www3.epa.gov/airnow/aqi-technical-assistance-document-sept2018.pdf
        x = math.trunc(pm2_5 * 10) / 10

        if x <= 9.0: # Good
            aqi = round(x / 9.0 * 50)
        elif x <= 35.4: # Moderate
            aqi = round((x - 9.1) / 26.3 * 49.0 + 51.0)
        elif x <= 55.4: # Unhealthy for sensitive groups
            aqi = round((x - 35.5) / 19.9 * 49.0 + 101.0)
        elif x <= 125.4: # Unhealthy
            aqi = round((x - 55.5) / 69.9 * 49.0 + 151.0)
        elif x <= 225.4: # Very Unhealthy
            aqi = round((x - 125.5) / 99.9 * 99.0 + 201.0)
        else: # Hazardous
            aqi = round((x - 225.5) / 99.9 * 199.0 + 301.0)

        # A negative pm2_5 (only possible if a bogus value reached the
        # database by some other means) must not map below zero.
        return max(0, aqi)

    @staticmethod
    def compute_pm2_5_aqi_color(pm2_5_aqi):
        if pm2_5_aqi <= 50:
            return 228 << 8                      # Green
        elif pm2_5_aqi <= 100:
            return (255 << 16) + (255 << 8)      # Yellow
        elif pm2_5_aqi <=  150:
            return (255 << 16) + (126 << 8)      # Orange
        elif pm2_5_aqi <= 200:
            return 255 << 16                     # Red
        elif pm2_5_aqi <= 300:
            return (143 << 16) + (63 << 8) + 151 # AirGradient
        else:
            return (126 << 16) + 35              # Maroon

    @staticmethod
    def get_scalar(obs_type, record, db_manager=None):
        log.debug('get_scalar(%s)' % obs_type)
        if obs_type not in [ 'pm2_5_aqi', 'pm2_5_aqi_color' ]:
            raise weewx.UnknownType(obs_type)
        log.debug('get_scalar(%s)' % obs_type)
        if record is None:
            log.debug('get_scalar called where record is None.')
            raise weewx.CannotCalculate(obs_type)
        if 'pm2_5' not in record:
            # Returning CannotCalculate causes exception in ImageGenerator, return UnknownType instead.
            # ERROR weewx.reportengine: Caught unrecoverable exception in generator 'weewx.imagegenerator.ImageGenerator'
            log.debug('get_scalar called where record does not contain pm2_5.')
            raise weewx.UnknownType(obs_type)
        if record['pm2_5'] is None:
            # Returning CannotCalculate causes exception in ImageGenerator, return UnknownType instead.
            # ERROR weewx.reportengine: Caught unrecoverable exception in generator 'weewx.imagegenerator.ImageGenerator'
            # This will happen for any catchup records inserted at weewx startup.
            log.debug('get_scalar called where record[pm2_5] is None.')
            raise weewx.UnknownType(obs_type)
        try:
            pm2_5 = record['pm2_5']
            if obs_type == 'pm2_5_aqi':
                value = AQI.compute_pm2_5_aqi(pm2_5)
            else: # pm2_5_aqi_color
                value = AQI.compute_pm2_5_aqi_color(AQI.compute_pm2_5_aqi(pm2_5))
            t, g = weewx.units.getStandardUnitType(record['usUnits'], obs_type)
            # Form the ValueTuple and return it:
            return weewx.units.ValueTuple(value, t, g)
        except KeyError:
            # Don't have everything we need. Raise an exception.
            raise weewx.CannotCalculate(obs_type)

    @staticmethod
    def get_series(obs_type, timespan, db_manager, aggregate_type=None, aggregate_interval=None):
        """Get a series, possibly with aggregation.
        """

        if obs_type not in [ 'pm2_5_aqi', 'pm2_5_aqi_color' ]:
            raise weewx.UnknownType(obs_type)

        log.debug('get_series(%s, %s, %s, aggregate:%s, aggregate_interval:%s)' % (
            obs_type, timestamp_to_string(timespan.start), timestamp_to_string(
            timespan.stop), aggregate_type, aggregate_interval))

        #  Prepare the lists that will hold the final results.
        start_vec = list()
        stop_vec = list()
        data_vec = list()

        # Is aggregation requested?
        if aggregate_type:
            # Yes. Just use the regular series function.
            return weewx.xtypes.ArchiveTable.get_series(obs_type, timespan, db_manager, aggregate_type,
                                           aggregate_interval)
        else:
            # No aggregation.
            sql_str = 'SELECT dateTime, usUnits, `interval`, pm2_5 FROM %s ' \
                      'WHERE dateTime >= ? AND dateTime <= ? AND pm2_5 IS NOT NULL' \
                      % db_manager.table_name
            std_unit_system = None

            for record in db_manager.genSql(sql_str, timespan):
                ts, unit_system, interval, pm2_5 = record
                if std_unit_system:
                    if std_unit_system != unit_system:
                        raise weewx.UnsupportedFeature(
                            "Unit type cannot change within a time interval.")
                else:
                    std_unit_system = unit_system

                if obs_type == 'pm2_5_aqi':
                    value = AQI.compute_pm2_5_aqi(pm2_5)
                else: # pm2_5_aqi_color
                    value = AQI.compute_pm2_5_aqi_color(AQI.compute_pm2_5_aqi(pm2_5))
                log.debug('get_series(%s): %s - %s - %s' % (obs_type,
                    timestamp_to_string(ts - interval * 60),
                    timestamp_to_string(ts), value))
                start_vec.append(ts - interval * 60)
                stop_vec.append(ts)
                data_vec.append(value)

            unit, unit_group = weewx.units.getStandardUnitType(std_unit_system, obs_type,
                                                               aggregate_type)

        return (ValueTuple(start_vec, 'unix_epoch', 'group_time'),
                ValueTuple(stop_vec, 'unix_epoch', 'group_time'),
                ValueTuple(data_vec, unit, unit_group))

    @staticmethod
    def get_aggregate(obs_type, timespan, aggregate_type, db_manager, **option_dict):
        """Returns an aggregation of pm2_5_aqi over a timespan by using the main archive
        table.

        obs_type: Must be 'pm2_5_aqi' or 'pm2_5_aqi_color'.

        timespan: An instance of weeutil.Timespan with the time period over which aggregation is to
        be done.

        aggregate_type: The type of aggregation to be done. For this function, must be 'avg',
        'count', 'first', 'last', 'min', or 'max'. Anything else will cause
        weewx.UnknownAggregation to be raised.  ('sum' is deliberately not
        supported: the AQI of summed concentrations is not a meaningful
        quantity.)

        db_manager: An instance of weewx.manager.Manager or subclass.

        option_dict: Not used in this version.

        returns: A ValueTuple containing the result.
        """
        if obs_type not in [ 'pm2_5_aqi', 'pm2_5_aqi_color' ]:
            raise weewx.UnknownType(obs_type)

        log.debug('get_aggregate(%s, %s, %s, aggregate:%s)' % (
            obs_type, timestamp_to_string(timespan.start),
            timestamp_to_string(timespan.stop), aggregate_type))

        aggregate_type = aggregate_type.lower()

        # Raise exception if we don't know about this type of aggregation
        if aggregate_type not in list(AQI.agg_sql_dict.keys()):
            raise weewx.UnknownAggregation(aggregate_type)

        # Form the interpolation dictionary
        interpolation_dict = {
            'start': timespan.start,
            'stop': timespan.stop,
            'table_name': db_manager.table_name,
            'pm2_5_summary_suffix': '_day_pm2_5'
        }

        # The daily summary table can only be used if the timespan covers
        # whole archive days: both endpoints on local midnight.  A span
        # whose length merely happens to be a multiple of 24 hours (e.g.,
        # a trailing 24-hour window) must use the regular archive table.
        on_day_boundary = (timespan.start != timespan.stop
                           and weeutil.weeutil.isStartOfDay(timespan.start)
                           and weeutil.weeutil.isStartOfDay(timespan.stop))
        log.debug('day_boundary start: %r stop: %r on_day_boundary: %s' % (
            timespan.start, timespan.stop, on_day_boundary))
        if aggregate_type in list(AQI.day_boundary_avg_min_max_sql_dict.keys()) and on_day_boundary:
            select_stmt = AQI.day_boundary_avg_min_max_sql_dict[aggregate_type] % interpolation_dict
            select_usunits_stmt = AQI.day_boundary_avg_min_max_sql_dict['usUnits'] % interpolation_dict
            need_usUnits = True
        else:
            select_stmt = AQI.agg_sql_dict[aggregate_type] % interpolation_dict
            need_usUnits = False
        if need_usUnits:
            row = db_manager.getSql(select_usunits_stmt)
            if row:
                std_unit_system, = row
            else:
                std_unit_system = None
        row = db_manager.getSql(select_stmt)
        if row:
            if need_usUnits:
                value, = row
            else:
                value, std_unit_system = row
        else:
            value = None
            std_unit_system = None

        # A count is a count of records; every other aggregate is a pm2_5
        # concentration that must be converted to an AQI (or color).
        if value is not None and aggregate_type != 'count':
            if obs_type == 'pm2_5_aqi':
                value = AQI.compute_pm2_5_aqi(value)
            else: # pm2_5_aqi_color
                value = AQI.compute_pm2_5_aqi_color(AQI.compute_pm2_5_aqi(value))
        t, g = weewx.units.getStandardUnitType(std_unit_system, obs_type, aggregate_type)
        # Form the ValueTuple and return it:
        log.debug('get_aggregate(%s, %s, %s, aggregate:%s, select_stmt: %s, returning %s)' % (
            obs_type, timestamp_to_string(timespan.start), timestamp_to_string(timespan.stop),
            aggregate_type, select_stmt, value))
        return weewx.units.ValueTuple(value, t, g)

if __name__ == "__main__":
    usage = """%prog [options] [--help] [--debug]"""

    import weeutil.logger

    def main():
        import optparse

        parser = optparse.OptionParser(usage=usage)
        parser.add_option('--config', dest='cfgfn', type=str, metavar="FILE",
                          help="Use configuration file FILE. Default is /etc/weewx/weewx.conf or /home/weewx/weewx.conf")
        parser.add_option('--test-collector', dest='tc', action='store_true',
                          help='test the data collector')
        parser.add_option('--test-is-sane', dest='sane_test', action='store_true',
                          help='test the is_sane function')
        parser.add_option('--hostname', dest='hostname', action='store',
                          help='hostname to use with --test-collector')
        parser.add_option('--port', dest='port', action='store',
                          type=int, default=80,
                          help="port to use with --test-collector. Default is '80'")
        (options, args) = parser.parse_args()

        weeutil.logger.setup('airgradient', {})

        if options.tc:
            if not options.hostname:
                parser.error('--test-collector requires --hostname argument')
            test_collector(options.hostname, options.port)
        if options.sane_test:
            test_is_sane()

    def test_collector(hostname, port):
        while True:
            print(collect_data(hostname, port, 15))
            time.sleep(5)

    def test_is_sane():

        good_device = ('{'
                '"serialno": "ecda3b1eaaaf",'
                '"wifi": -65,'
                '"rco2": 730,'
                '"pm01": 2,'
                '"pm02": 3,'
                '"pm10": 4,'
                '"pm02Compensated": 3.5,'
                '"pm01Standard": 2.5,'
                '"pm02Standard": 4.5,'
                '"pm10Standard": 5.5,'
                '"pm003Count": 185,'
                '"pm005Count": 60,'
                '"pm01Count": 10,'
                '"pm02Count": 5,'
                '"pm50Count": 1,'
                '"pm10Count": 0,'
                '"atmp": 22.50,'
                '"atmpCompensated": 21.80,'
                '"rhum": 55,'
                '"rhumCompensated": 58,'
                '"tvocIndex": 100,'
                '"tvocRaw": 30000,'
                '"noxIndex": 1,'
                '"noxRaw": 16000,'
                '"boot": 7,'
                '"ledMode": "co2" }')

        good_proxy = ('{'
                '"measurementTime": "2025-10-25T17:45:00.000Z",'
                '"serialno": "ecda3b1eaaaf",'
                '"wifi": -65,'
                '"rco2": 730,'
                '"pm01": 2,'
                '"pm02": 3,'
                '"pm10": 4,'
                '"pm02Compensated": 3.5,'
                '"pm01Standard": 2.5,'
                '"pm02Standard": 4.5,'
                '"pm10Standard": 5.5,'
                '"pm003Count": 185,'
                '"pm005Count": 60,'
                '"pm01Count": 10,'
                '"pm02Count": 5,'
                '"pm50Count": 1,'
                '"pm10Count": 0,'
                '"atmp": 22.50,'
                '"atmpCompensated": 21.80,'
                '"rhum": 55,'
                '"rhumCompensated": 58,'
                '"tvocIndex": 100,'
                '"tvocRaw": 30000,'
                '"noxIndex": 1,'
                '"noxRaw": 16000,'
                '"boot": 7,'
                '"ledMode": "co2" }')
        bad_device = ('{'
                '"serialno": "ecda3b1eaaaf",'
                '"wifi": -65,'
                '"rco2": 730,'
                '"pm01": "nan",'
                '"pm02": "nan",'
                '"pm10": "nan",'
                '"pm02Compensated": 3.5,'
                '"pm01Standard": 2.5,'
                '"pm02Standard": 4.5,'
                '"pm10Standard": 5.5,'
                '"pm003Count": 185,'
                '"pm005Count": 60,'
                '"pm01Count": 10,'
                '"pm02Count": 5,'
                '"pm50Count": 1,'
                '"pm10Count": 0,'
                '"atmp": 22.50,'
                '"atmpCompensated": 21.80,'
                '"rhum": 55,'
                '"rhumCompensated": 58,'
                '"tvocIndex": 100,'
                '"tvocRaw": 30000,'
                '"noxIndex": 1,'
                '"noxRaw": 16000,'
                '"boot": 7,'
                '"ledMode": "co2" }')
        bad_proxy = ('{'
                '"measurementTime": "2025-10-25T17:45:00.000Z",'
                '"serialno": "ecda3b1eaaaf",'
                '"wifi": -65,'
                '"rco2": 730,'
                '"pm01": 2,'
                '"pm02": 3,'
                '"pm10": 4,'
                '"pm02Compensated": 3.5,'
                '"pm01Standard": 2.5,'
                '"pm02Standard": 4.5,'
                '"pm10Standard": 5.5,'
                '"pm003Count": 185,'
                '"pm005Count": 60,'
                '"pm01Count": 10,'
                '"pm02Count": 5,'
                '"pm50Count": 1,'
                '"pm10Count": 0,'
                '"atmp": 22.50,'
                '"atmpCompensated": 21.80,'
                '"rhum": 55,'
                '"rhumCompensated": "nan",'
                '"tvocIndex": 100,'
                '"tvocRaw": 30000,'
                '"noxIndex": 1,'
                '"noxRaw": 16000,'
                '"boot": 7,'
                '"ledMode": "co2" }')

        j = json.loads(good_proxy)
        sane, _ = is_sane(j)
        assert(sane)
        j = json.loads(good_device)
        sane, _ = is_sane(j)
        assert(sane)
        j = json.loads(bad_device)
        sane, reason = is_sane(j)
        assert(not sane)
        assert(reason == "pm01 is not an instance of any of the following type(s): [<class 'float'>, <class 'int'>]: nan")
        j = json.loads(bad_proxy)
        sane, reason = is_sane(j)
        assert(not sane)
        assert(reason == "rhumCompensated is not an instance of any of the following type(s): [<class 'float'>, <class 'int'>]: nan")
        print('passed')

    main()
