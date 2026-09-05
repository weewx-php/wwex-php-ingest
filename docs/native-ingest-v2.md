# Native WeeWX ingest v2: hardware archives

Both projects implement this extension to [native ingest v1](native-ingest-v1.md).
The endpoint, HTTPS authentication, station discovery/adoption, unit systems,
limits, durable receipts, retries and replay rules remain those of v1.
PHP needs no hardware driver, Python installation or station-specific parser.

## Collector configuration

In the station's WeeWX configuration:

```ini
[StdArchive]
    record_generation = hardware
    archive_delay = 15
    no_catchup = false
```

For multiple instances in a single collector configuration, put `[[[StdArchive]]]`
inside each `[[station-key]]` under `[Stations]`. The choice is per driver instance;
every instance keeps its own station UUID, process, spool and logger cursor.

`record_generation` accepts `hardware` (the WeeWX default) or `software`.
LOOP observations are transmitted in either mode. In hardware mode the collector
also calls the original driver's `genStartupRecords(since)` at startup and
`genArchiveRecords(since)` after a completed archive period and its delay.
Calls run in the same engine process, between LOOP reading cycles, not concurrently
on the USB/serial connection. Records pass through `NEW_ARCHIVE_RECORD` callbacks
with `origin='hardware'` before serialization.

The driver's `archive_interval` is authoritative, in **seconds**. If the property
is unsupported, `[StdArchive] archive_interval` is used for polling; its fallback
is the collector's `lifecycle_interval` (300 seconds by default). `archive_delay`
falls back to `lifecycle_delay` (15 seconds). Delay must be positive and shorter
than the effective interval. The collector does not reprogram the logger interval.

If a driver raises `NotImplementedError`, the collector continues sending LOOP
data and PHP generates software archives. A hardware read error is retried at the
next period; its durable cursor does not jump over unread records. In `software`
mode neither logger API is called. No local software aggregate is transmitted.
`no_catchup=true` skips startup catch-up; for a new spool it establishes the current
time as the cursor. Subsequent periodic polling still operates normally.

The engine has no `StdArchive` database service, report service or Cheetah layer.
`[StdWXCalculate] ... = hardware` is a separate calculation policy and is not
controlled by `record_generation`.

## Request and acknowledgement

A batch containing hardware records uses integer `version: 2`. LOOP-only batches
continue using v1, including previously queued events. PHP accepts both versions;
a successful response echoes the request version. Capabilities advertise
`versions: [1, 2]` and `kinds: ["loop", "archive"]`. A v1 receiver rejects a v2
request; the collector retains its unacknowledged events. Upgrade PHP before
enabling hardware collection. There is no lossy conversion to LOOP on downgrade.

Example packet inside a v2 `packets` array:

```json
{
  "station_id": "f7b175bd-5130-4568-94b5-54f1e3e5d3fd",
  "event_id": "a5ec85d9-3e34-5bd6-a785-41c3fc64ce19",
  "driver_module": "weewx.drivers.vantage",
  "kind": "archive",
  "dateTime": 1788600000,
  "interval": 5,
  "usUnits": 17,
  "data": {"outTemp": 18.7, "windSpeed": 2.5, "rain": 0.6}
}
```

The [v2 JSON Schema](../schemas/weewx-v2.schema.json) defines the shape.

| Field | Meaning |
|---|---|
| `kind` | `loop` for an individual observation; `archive` only for an original hardware logger record |
| `dateTime` | Original interval **end**, integer Unix seconds; not the upload time |
| `interval` | Required only for `archive`, original duration in **minutes**, as in WeeWX records |
| `usUnits` | Original WeeWX unit system: 1 US, 16 METRIC, 17 METRICWX |
| `data` | Numeric/null observations with WeeWX names and their original semantics; `rain` is the amount during the interval |

The coverage is `(dateTime - interval * 60, dateTime]`. Duration must be finite,
positive, no greater than 1440 minutes, and represent whole seconds (1–86400).
`dateTime` must exceed the duration. Age limits apply to the original end time.
`interval` is forbidden in LOOP packets and inside `data`. Do not send local
accumulator summaries or outage rollups as hardware records. Custom observations
still require the existing PHP measurement definitions when their semantics are
not standard WeeWX; Python unit registries and accumulator implementations are
not transferred.

Hardware event IDs use UUIDv5 with the persistent station UUID as namespace and
`archive:<driver_module>:<dateTime>:<duration_seconds>` as the name (decimal
integers, no padding). Re-reading the same logger record produces the same ID.
Changed contents under that ID are an `event_conflict`, not a second measurement.
The receipt digest includes `kind` and `interval`. Existing v1 LOOP receipts stay
valid inside v2 batches. Standard adoption, blocked stations and ACK rules apply
equally to both kinds. `stored` acknowledges the live journal, not archive output.

## Durability and recovery

The collector advances its per-station `hardware_cursor` in the same SQLite
transaction as the queued hardware event. It does not wait for the HTTP ACK.
Restart resumes from this durable cursor, while already queued events keep their
original IDs and payloads. A full spool pauses collection without advancing the
cursor; an acknowledged upload frees space. Keep the spool and station UUID when
replacing or upgrading the collector.

Driver generators must yield records chronologically, as required by WeeWX's
catch-up API. Boundary records at or before the cursor are skipped. Catch-up is
bounded by the collector's `max_age_seconds` and, once known, the receiver's smaller
age limit; older logger history is outside this ingest contract. Pending stations
keep their events until adoption, subject to the normal age/quarantine policy.
Future logger timestamps more than 60 seconds ahead fail collection without
advancing the cursor. Status includes `record_generation`, `hardware_state`,
`hardware_error`, `hardware_interval` (polling seconds), `hardware_last_interval` (last queued record duration in seconds), and `hardware_cursor` (Unix time).

The local spool covers Internet/web-host outages. The station logger can also
recover intervals while the collector was off, subject to the logger's actual
capacity, driver capabilities and the contract's age limit.

## Independent intervals

Three settings have different purposes and do not need to match:

| Cadence | Owner | Meaning |
|---|---|---|
| Hardware logger | Device/driver | Resolution of stored measurements; each record carries its own duration |
| Upload | Collector | How often queued events are transmitted; does not change their timestamp or duration |
| Target archive | PHP `archive_interval` | Fixed output grid built from available LOOP and hardware observations |

The collector does not resample records to its polling interval. A logger backlog
may contain different durations after a device configuration change. Each original
interval is validated, queued and acknowledged independently. Polling uses the
current driver interval; `hardware_last_interval` describes the most recently
queued record and may differ. PHP target settings are never needed on the
collector. The delay constraint applies to the effective polling interval,
not an unused software fallback setting.

## PHP archive selection and retention

Both kinds remain separate in the live journal under the same station identity.
The live display continues to use LOOP readings. No extra receiver switch is
needed; the usual station adoption and archive assignment apply.

PHP copies hardware history into the selected archive's `weewx_hardware` table
before releasing its journal retention hold. It retains the original interval and
source with the archive's mapped, converted, calibrated, quality-checked values.
These records survive live-journal pruning. This is not a permanent copy of the
unmapped wire payload: remapping old source fields still requires retained inputs.
There is no automatic expiry of this archive history. Back up the entire SQLite
archive, including the additional table.

For each output observation independently, a complete, non-overlapping sequence
of whole hardware intervals may fill a target archive interval. The observation's
accumulation policy applies: duration-weighted averages, sums for interval amounts
such as rain/ET, maximum gust with its associated direction, or configured
first/last/min/max. Hardware replaces overlapping LOOP values, never adds to them.
LOOP accumulation fills fields without complete hardware coverage and contributes
available observed daily extremes. Existing station/field mappings remain in force.

| Logger to PHP target | Result |
|---|---|
| 5 min to 15 min, aligned | Three complete records form one archive record |
| 5 min to 1 min | LOOP supplies minutes; logger-only periods remain gaps in the minute grid |
| 5 min to 7 min | No division at crossing boundaries; use available LOOP for the fixed grid |
| Variable 5/10 min to 15 min | Combine only if whole intervals tile the target completely |

Late hardware input schedules durable replay, preserves the original interval,
repairs eligible fixed-grid records and invalidates affected query caches.
Retries do not duplicate measurements. On upgrade, retained native hardware
history is scheduled for replay once per archive, including intervals the earlier
exact-match implementation could not use. History already pruned before upgrade
cannot be recovered this way.

## Historical queries and JSON series

PHP frontend period queries also read the retained hardware history. For each
observation and requested span, complete non-overlapping hardware intervals take
precedence over overlapping fixed-grid values. Other fields keep their own
sources. Only intervals wholly contained in the query are included: no rain is
prorated across a boundary, and average values are duration-weighted. Coverage
reports the included duration. A missing measurement is `null`, not zero rain.
This lets a 15-minute or daily query use five-minute logger history even when the
configured archive grid is one minute. It also prevents counting the same rain
once from LOOP and again from the logger.

Fine or misaligned series preserve their requested `points` grid. If an original
hardware interval cannot fit a bucket and overlaps missing or partial coverage,
the full JSON result additionally includes:

```json
{
  "fallbackSource": "hardware",
  "fallback": [
    {"start": 1788599700, "end": 1788600000, "value": 0.6, "coverage": 1.0}
  ]
}
```

These values have the same unit as the series but retain the **original logger
observation semantics**. This layer is provided for `avg`, `weighted_avg`, `sum`,
`cumulative`, `min`, `max`, `first` and `last` measurement series. It is omitted for
degree-day observations, counts, timestamps, thresholds, derivatives and analysis
reports, whose values have different semantics. It is a separate, non-additive layer: do not add it
to bucket values, spread them over minute points, or interpret them as cumulative
values. An original interval may extend beyond the query bounds; render its true
bounds (and clip visually if needed). `coverage: 1` describes that complete logger
interval, not the requested fine buckets. Fields are omitted when there is no
fallback. Unit conversion, persisted computation, result caching and JSON feeds
preserve this layer. The combined series/feed limit includes fallback intervals.

The bundled time charts draw logger spans separately; the data table lists them
with their actual bounds. Cumulative series with hardware history stop at the
first incomplete bucket. `Series::pairs()` and the compact points-only JSON helper
return only the requested grid; use the full serialized `Series` for fallback.
`records()` exposes the selected whole intervals directly. Point-in-time
`current`/`latest`/trend queries retain their fixed-archive semantics; use period
queries or `records()` to inspect original logger coverage. Live reads use LOOP.

Overlapping hardware intervals with different bounds for the same mapped field
are retained but excluded from historical aggregation, rather than resolved by
interpolation. Identical spans from different sources have a deterministic
source-order winner in historical queries. An overlapping fixed record is either
selected whole or excluded whole. Misaligned bucket totals therefore need not sum
to a fresh query over their combined span; such cached aggregates are never merged
across bounds. Daily-specific statistics cannot allocate a logger interval across
local midnight; such intervals reduce day coverage. A broader ordinary period
query may include the complete interval.

Hardware averages do not contain original sample counts, exact extrema or vector
sums. Coalesced wind direction/vector statistics are estimates from the recorded
speeds, directions and durations. Min/max statistics use available logger values
and retained LOOP extrema; unavailable within-interval extrema and their times
cannot be reconstructed. Custom observations still need their PHP policy/units.

The standard WeeWX `archive` and daily tables remain the fixed-grid export.
External tools reading those tables directly do not see PHP's combined history or
its coarse fallback layer. PHP's frontend resolves those when querying.

Upstream semantics: [StdArchive options](https://weewx.com/docs/latest/reference/weewx-options/stdarchive/),
[driver API](https://github.com/weewx/weewx/blob/9fb5a0ddc5379a0bbd87de1919eefefbef3b9455/src/weewx/drivers/__init__.py).
