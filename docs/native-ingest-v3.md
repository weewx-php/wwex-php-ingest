# Native ingest v3: independent physical sensors

A configured receiver runs one worker. Supported sources are RTL433, GW1000 LAN
and WeatherFlow/Tempest UDP. Every identified sensor
with numeric observations gets its own station UUID and durable queue. New sensors
are discovered while the collector is running; no per-sensor configuration or
registration is required. The receiver worker itself sends no measurements and does not
appear as an adoptable station. GW1000 gateway measurements may create a separate
station identified by the gateway MAC.

PHP lists each sensor by model, ID and channel in the existing Adopt area. Adopt
and map each desired sensor there. Pending sensors send a preview but cannot write
measurements to the live journal. Blocking one sensor does not block other sensors.
The collector retains unacknowledged events under the existing age/queue limits.

## Wire contract

The endpoint and collector token remain unchanged. A batch containing `source`
uses integer `version: 3`; it can also carry ordinary LOOP and hardware archive
events. PHP accepts versions 1, 2 and 3 and echoes the request version in its ACK.
All v1/v2 validation, byte/packet limits, receipts and adoption rules apply.

An SDR event has `driver_module: "weewx_php_ingest.sdr"`, `kind: "loop"`,
`usUnits: 17` (METRICWX), and this additional property:

```json
"source": {
  "type": "rtl_433",
  "receiver_id": "11111111-1111-4111-8111-111111111111",
  "model": "Acurite-Tower",
  "sensor_id": "42",
  "channel": "1"
}
```

All five properties are required; additional properties are forbidden. `model`
and `sensor_id` are 1â€“64 printable ASCII characters. `channel` has the same limit
but is `""` when absent. IDs and channels are strings; the collector converts
integer decoder identifiers to decimal strings. `receiver_id` is a lowercase UUID
stored in the receiver's local journal. Source metadata is outside `data` and is
never interpreted as measurements, credentials or executable configuration.

The station UUID is UUIDv5 with `receiver_id` as namespace and the name:

```text
<source.type>:<compact JSON array [model,sensor_id,channel]>
```

The JSON array uses ordinary ASCII JSON escaping, no escaped slashes and no spaces
between members. Both implementations enforce this identity. Model, ID or channel
changes produce a new station; the same identity under another receiver also
produces a different station. Keep the collector state directory across updates
and restarts. Some devices change ID after a battery replacement; these must be
adopted again. Indistinguishable radio identities cannot be separated in software.

| `source.type` | Required `driver_module` |
|---|---|
| `rtl_433` | `weewx_php_ingest.sdr` |
| `gw1000` | `weewx_php_ingest.gw1000` |
| `weatherflow_udp` | `weewx_php_ingest.weatherflow` |

`source` is required for these modules and forbidden on other modules or archive
records. It is covered by the event receipt digest and stored with the discovered
station. It cannot grant adoption, rebind a token or select another station UUID.
An older receiver rejects v3; queued events remain unacknowledged.

## Observation handling

The collector consumes bounded JSON lines from `rtl_433`, without the WeeWX SDR
extension or its `sensor_map`. Reception uses `-M time:unix -C si`; Unix timestamps
may be strings or numbers. They become integer Unix seconds. Missing timestamps
use reception time; timestamps outside the live input window (-300/+60 seconds)
are rejected before discovery. Offline upload retains the original event time.

| rtl_433 field | Transmitted field/unit |
|---|---|
| `temperature_C`, `temperature_F` | `outTemp`, Â°C |
| `humidity` | `outHumidity`, percent |
| `pressure_hPa` | `pressure`, mbar |
| `wind_avg_m_s`, `wind_avg_km_h`, `wind_avg_mi_h` | `windSpeed`, m/s |
| `wind_max_m_s`, `wind_max_km_h`, `wind_max_mi_h` | `windGust`, m/s |
| `wind_dir_deg` | `windDir`, degrees |
| `battery_ok` | `batteryStatus`, 0 OK / 1 low |
| Other numeric fields | Original name with `rtl_` prefix, original numeric unit |

Unknown numeric fields require a matching PHP measurement definition and unit
before mapping. Do not infer their units from `usUnits`; that code applies to
standard WeeWX names. Decoder metadata, strings, arrays and nulls are not readings.
Invalid numbers, duplicate JSON properties, ambiguous unit aliases, packets without
model/ID and packets without numeric readings are rejected. Radio discovery is
limited to the decoders enabled by the installed `rtl_433` version and the selected
frequency; this does not promise support for every RF device.

`rain_mm` and `rain_in` remain available as `rtl_rain_mm` / `rtl_rain_in` totals.
The collector also computes `rain` in mm from consecutive nondecreasing totals
for that sensor. The first reading, counter reset or unit change only establishes
a baseline. Baseline and event commit atomically; restart/retry cannot double-count.
Identical observations within two seconds are suppressed as radio repeats. No
synthetic zeros or time-based measurement packets are emitted for silent sensors.

## Limits and operation

The default is 256 sensors per receiver (`max_sensors`, range 1â€“2000). PHP's own
pending-station and total-station limits also apply. Each sensor inherits the
receiver's queue limits. Input lines are bounded to 64 KiB and the pipe queue to
64 lines. Once a sensor queue is full, reception pauses with that event held in
memory until upload frees space. Other queues can still upload. An SDR cannot
recover RF transmissions missed while reception is paused or the system is off.

Malformed packets and sensor-capacity errors appear in local status/logs. Radio
identifiers and decoder output are never used as file paths or shell commands.
Stopping, probing or restarting a receiver terminates its decoder process too.
The receiver's health heartbeat does not represent sensor freshness; each sensor
keeps its own last measurement timestamp.

The system package `rtl-433` supplies RF decoders and USB support. The installer
installs it; `weewx-php-ingest update` also checks the configured OS repositories
for a newer package. If only that package changes, the running service restarts
to use it. Collector queues and identities are preserved. The daily WeeWX sync
does not mirror this separate OS package's source code.

Upstream: [rtl_433 options and installation](https://github.com/merbanan/rtl_433),
[timestamp and SI conversion implementation](https://github.com/merbanan/rtl_433/blob/master/src/r_api.c).


## GW1000 LAN sensors

The collector reads the local binary TCP API (default port 45000), including MAC,
sensor inventory and current observations. Inventory is read before and after a
snapshot; an ID change discards that snapshot. Supported channel families are
WH31, WH51, WH41, WH55, WN34 and WN35, plus WH57, WH45/WH46 and registered weather
arrays (WH24/WH65, WH68, WS80, WH40, WH25, WH26/WH32, WS90 and WS85).

`model` is the sensor family, `sensor_id` is `<gateway MAC>/<RF ID>` (lowercase hex,
12 and 8 characters), and `channel` is the channel number or empty. Some inventory
slots do not distinguish product variants; their stable names are `WH24/WH65`,
`WH26/WH32` and `WH45/WH46`. Gateway measurements use model `GW1000`, its MAC alone
and an empty channel. An IP change preserves identity; a changed sensor ID does not.
Unregistered/disabled entries and sensors reporting signal zero send no readings.

Channel fields belong to their identified sensors. Common outdoor fields are
assigned only when exactly one registered sensor can provide them. Otherwise they
remain `gw_` aggregate fields on the gateway station. The API does not provide a
separate common-field value per array, so the collector never duplicates that
value across candidate sensors. Even an ambiguous sensor may appear with its own
battery/signal diagnostics only. Values without known units remain unmapped.

Standard measurements use METRICWX units. Soil moisture and leaf wetness are
`gw_soilMoisturePercent` and `gw_leafWetnessPercent` (percent, not centibar).
`gw_batteryRaw` is the unconverted sensor-specific battery code; `gw_signal` is
the firmware signal level. Unknown/custom `gw_` fields require a PHP definition
and explicit unit before mapping. WH57 `gw_lightningcount` is a daily counter,
not a count to sum on every poll. Rain totals `gw_t_raintotals`, `gw_t_rainyear`
or `gw_p_rainyear` establish durable per-station baselines for interval `rain`.
Counter resets never produce negative rain. Piezo rain uses the read-rain API.

Snapshots carry collector reception time: the API does not expose each sensor's
measurement timestamp. Signal zero suppresses disconnected sensors; a nonzero
signal cannot prove that every field changed since the previous poll. Checksum,
command, frame length, duplicate tags and inventory shape are checked before the
bundled upstream parser runs. Unknown tags/models reject the snapshot with a
local diagnostic. Requests are read-only and bounded to 8192 bytes / 5 seconds.

## WeatherFlow / Tempest UDP

One listener on UDP port 50222 accepts `obs_st`, `obs_air`, `obs_sky` and
`rapid_wind`. Source models are `Tempest`, `AIR` and `SKY`; `sensor_id` is the
original `ST-…`, `AR-…` or `SK-…` serial, with an empty channel. A hub filter is
optional. The hub is not adopted; each physical observation device is.

Rapid wind and observation packets use the same station but independent durable
duplicate/order checks. Event IDs are UUIDv5 under the station UUID with names
`<packet type>:<Unix timestamp>`; GW1000 uses `snapshot:<Unix timestamp>`.
Identical repeats are discarded, including after restart. A conflicting payload
for the same stream/timestamp is rejected. An older observation can arrive after
a newer rapid wind reading without being discarded as a different station.

`windSpeed`/`windDir` are instantaneous rapid-wind readings. Observation wind
averages remain `wf_windAverage`/`wf_windDirection`, with `wf_windLull` and standard
`windGust`; the API does not identify gust direction. This avoids mixing period
averages into instantaneous samples. Standard rain is the previous-minute amount
in mm, never the day total. Lightning observation counts are summed once;
`evt_strike`, `evt_precip`, device status and hub status are ignored.

Datagrams are limited to 32 KiB and 16 observation rows. Duplicate JSON members,
non-finite values, invalid serial/type combinations and timestamps outside
-300/+60 seconds are rejected. Null observations are omitted. No observations
are invented during silence. UDP delivery cannot recover lost packets; local
binary gateway and WeatherFlow UDP protocols do not authenticate LAN devices.
Use the collector on the trusted device LAN. HTTPS authentication/adoption
remain mandatory between collector and PHP.

References: [GW1000 upstream](https://github.com/weewx-contrib/weewx-gw1000),
[WeatherFlow UDP v171](https://weatherflow.github.io/Tempest/api/udp/v171/).
