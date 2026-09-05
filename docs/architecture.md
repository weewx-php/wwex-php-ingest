# weewx-php-ingest architecture

Status: 2026-09-05. The native PHP receiver v1 is implemented in the separate
weewx-php project. The Python v1 collector is implemented; see
[implementation and verification](implementation.md). Hardware history and
rollups remain future protocol work.

The binding integration reference is [native ingest v1](native-ingest-v1.md),
with the [request schema](../schemas/weewx-v1.schema.json). These files mirror
the PHP project's contract. Hardware history and accumulator rollups below
describe future work; v1 accepts only original LOOP observations.

## Project boundary

`weewx-php-ingest` is a separate Python project in
`D:\Git\weewx-php-ingest`. It runs on a Raspberry Pi or another computer in
the station owner's LAN. `weewx-php` is the separate PHP application on a
remote web host. The collector initiates outbound HTTPS requests; the web
host does not connect to USB devices, mount the collector's database, or
require an inbound connection into the home network.

The first concrete setup is a locally connected Davis console sending to a
remote weewx-php installation. Multiple independent driver instances remain
a core architectural requirement. Collector code, tests, packaging and
deployment instructions belong in this project. Changes to the receiving
endpoint, live journal and archiver belong in the separate weewx-php project;
the PHP work listed below is an integration requirement, not collector code.

## Decision

Run original WeeWX drivers on the computer connected to the hardware. Use one
process, configuration and WeeWX engine per driver instance, managed as one
collector installation. Give every instance a persistent station identity.
Send its observations over HTTPS into weewx-php's live journal. Archive
selection, field placement, calculations and reporting remain on the web host.

Start with durable LOOP packets batched every configurable number of seconds.
Add accumulator rollups only with an explicit aggregate format and PHP support.
A short software archive record is not interchangeable with a LOOP packet.

```text
Raspberry Pi / home LAN                        Remote web host
weewx-php-ingest                               weewx-php

USB/serial A -> WeeWX engine A -> spool A --\
USB/serial B -> WeeWX engine B -> spool B ---- uploader -> HTTPS JSON receiver
network C   -> WeeWX engine C -> spool C --/                    |
                                                        live.sdb
                                                     sender A/B/C
                                                             |
                                                   tick -> archives
```

One installation can run the same driver module several times, including two
Davis consoles. Each instance keeps the driver's usual section names in its
own section of a shared `weewx.conf`. No primary hardware station is required by the collector.
Archives may still select a primary sender through the existing PHP settings.

## What the inspected code establishes

The WeeWX reference is the local upstream checkout at
`9fb5a0ddc5379a0bbd87de1919eefefbef3b9455` (2026-09-04), whose package version
is 5.5.0. The metadriver reference is
[`tkeffer/weewx-metadriver` at `3f0bae7`](https://github.com/tkeffer/weewx-metadriver/tree/3f0bae7fb092536e6667de80d53b0f1b41a48684)
(2026-08-22). Findings below come from source inspection, not hardware tests.

### Tom's metadriver

The [implementation](https://github.com/tkeffer/weewx-metadriver/blob/3f0bae7fb092536e6667de80d53b0f1b41a48684/bin/user/metadriver.py)
already loads normal driver modules, starts a thread per child and tags each
LOOP packet with `source = station_type`. It multiplexes packets into one
stream; the downstream WeeWX pipeline makes the combined archive.

For this collector, the following details need a different design:

| Current metadriver behavior | Collector requirement |
|---|---|
| Children receive the same configuration and real engine | Separate configuration, event callbacks and state for every instance |
| `source` is the station type | Persistent instance identity, independent of module, label and USB enumeration |
| Workers are keyed by station type | Multiple instances of the same driver with independent device settings |
| Archive reads and clock operations delegate to the primary | Hardware history and clock lifecycle available to each station |
| A primary LOOP exception propagates to the main engine; a secondary is dropped | Restart only the failed station with backoff |
| Queues are unbounded and memory-only | Bounded, durable local storage |
| A command waits while the worker is blocked inside `next(loop_iter)` | Watchdog and process termination/restart after a configured deadline |

The README's failure description is broader than the current implementation:
the code and failure tests explicitly treat a primary failure as fatal.
The [TODO](https://github.com/tkeffer/weewx-metadriver/blob/3f0bae7fb092536e6667de80d53b0f1b41a48684/TODO.md)
also calls out child engine events as unfinished work.

The shared event engine matters for data correctness. WeeWX's
[`VantageService`](https://github.com/weewx/weewx/blob/9fb5a0ddc5379a0bbd87de1919eefefbef3b9455/src/weewx/drivers/vantage.py)
subscribes to `NEW_LOOP_PACKET` and writes gust values into that event's
packet. With a shared engine it can receive another child's packet. Merely
adding a station ID at the end would not undo that contamination.

The worker/queue pattern is useful reference material. Preserving independent
WeeWX engines is a better starting point for the stated compatibility goal.

### PHP integration points inspected before implementation

| Component | Existing behavior and consequence |
|---|---|
| `Live/Packet.php`, `Live/LiveDb.php` | Already carry sender, driver, identity, time, units, kind and arbitrary measurement names |
| `LiveDb::add()` | Transactionally stores a packet and marks archive intervals pending |
| Live uniqueness | Uses `(driver, identity, kind, dateTime, digest)`, not `sender`; different instances must have different identities |
| `Ingest/Receiver.php`, `Ingest/Protocol.php` | Public HTTP ingest currently accepts Ecowitt and WU forms, not native WeeWX JSON |
| `Ingest/Parser.php::timestamp()` | Device times more than four hours old are replaced with reception time; unsuitable for collector replay |
| `Archive/Archiver.php::build()` | LOOP observations are calculated individually and accumulated with weight 1 |
| Same method, archive branch | Archive packets overlay one another with `array_replace`; short archive packets are not combined into a longer interval |
| `LiveDb::recent()`, `Archiver::latest()` | Current live views use LOOP packets; an archive-only sender would need additional live-view support |
| `Archiver::processDue()` | Existing intervals are left unchanged unless replacement is enabled |
| `LiveDb::prune()` | Deletes by measurement time, including old pending marks; accepted historical replay needs protection until processed |

This table records the initial findings. The implemented receiver now adds
native JSON transport, transactionally durable event receipts, protected
retention and resumable archive/day repairs. Ecowitt/WU behavior remains on
its existing receiver. Station-to-archive mapping is reused.

## Driver runtime

Use upstream WeeWX as a versioned dependency, with a collector package providing
the service profile, supervisor and transport. Keep the normal
`loader(config_dict, engine)` contract, `user.*` import paths, extension
installation and hardware dependencies. Retain per-instance virtual environments
as an option when third-party dependencies conflict.

Use real `StdEngine` instances rather than `loader(config, None)` or a minimal
fake engine. Preserve startup, LOOP callbacks, end-of-period events, serial
housekeeping and shutdown. Configuration must explicitly include every service
group; for example, omitting `xtype_services` makes `StdEngine` supply defaults.

The collector service replaces `StdArchive`'s storage responsibilities and
retains the necessary lifecycle coordination. It journals packets after the
driver's own event processing. Archive calculation services in the collector
are off by default, so PHP's existing per-archive rules operate on observations.
Driver-required auxiliary services remain configurable per instance; document
which transformations they apply to avoid applying the same correction twice.

Disable report, image, FTP/rsync and external weather-upload services. Cheetah
then takes no part in collection. Upstream's package still declares CT3 and
other reporting dependencies: physically omitting those from a distribution
is a separate packaging change requiring import and extension-install checks.
Do not strip arbitrary upstream modules before that compatibility is verified.

Each worker owns its device and spool. A separate uploader reads committed
spool rows, so an HTTP timeout cannot hold up the driver. A supervisor restarts
individual workers and detects silence using a per-driver expected cadence.
Stable device paths such as `/dev/serial/by-id/...` should be used where
available. Other USB selectors remain driver-specific.

Supporting arbitrary drivers means retaining their API and runtime contract;
it does not establish that every third-party driver has been tested. A driver
that internally merges sensors is still one station here. Splitting its
internal sources requires a separate driver-aware feature.

## Three independent intervals

1. **Reading cadence:** determined by hardware and driver. A transport setting
   cannot make a console produce measurements more frequently.
2. **Send interval:** configurable seconds, for example 10. Each request can
   contain all readings from multiple stations collected since the previous
   send, with original timestamps and identities intact.
3. **Archive interval:** configured on the PHP archive. Hardware logger
   intervals are another independent property and remain attached to their
   records.

WeeWX already accepts a software `archive_interval` in seconds and emits
`record['interval'] = archive_interval / 60`. Its accumulator is reusable.
However, `getRecord()` extracts a summary; it does not serialize all statistics.
Changing that interval alone does not provide a lossless transport format.

## Native HTTP contract: implemented v1

Use the complete [receiver contract](native-ingest-v1.md) and
[JSON Schema](../schemas/weewx-v1.schema.json) when implementing the collector.
The authoritative originals live in weewx-php at `docs/native-ingest.md` and
`resources/ingest/weewx-v1.schema.json`.

- Endpoint: `POST /ingest/weewx.php`, HTTPS, UTF-8 `application/json`, no rewriting.
- Authentication: exactly one of `Authorization: Bearer <token>` or
  `X-WeeWX-Token: <token>`; never a URL/query credential.
- Setup: the collector generates its UUID/token locally. First delivery discovers
  stations as pending; the existing Adopt UI or `ingest adopt` admits each station.
  Pending is not a durable weather-data acknowledgement.
- Envelope: integer `version=1`, `collector_id`, `packets` array. Every event
  carries `station_id`, `event_id`, `driver_module`, `kind=loop`, integer
  `dateTime`, `usUnits` and numeric/null `data`.
- Identity: persist UUIDs per driver instance, including repeated modules.
  The journal uses `driver=weewx`, `identity=collector_id/station_id` and
  a server-generated stable sender. Module names are metadata, never PHP code.
- Event IDs: assigned once before HTTP; immutable content and identity on retries.
  Distinct source events remain distinct even with equal values in one second.
- Acknowledgement: one result per event, `stored`, `duplicate`, `pending`
  or `rejected`. Release only the first two; HTTP 200 alone is insufficient.
- Bounds: 262,144 bytes, 128 events, 256 fields/event; unit codes 1/16/17.
  Maximum event age is `min(7d, live_retention - 2d)`; future allowance 60 s.
  Receipts persist 30 days. Size `max_native_receipts` for all station cadences.
- Failure: preserve unconfirmed events across timeouts, malformed replies and
  5xx; back off for 429/pending. Surface permanent refusals. Retry unchanged
  accepted IDs after a lost response; changed content is `event_conflict`.
- Lifecycle: rotation keeps all identities and receipts. Preserve collector ID,
  station UUIDs and spool when replacing the Raspberry. Cross-collector identity
  rebinding is not implemented.

The server never replaces historical measurement times with reception time.
It stores arbitrary valid observation names, but Python unit/accumulator
registries must be configured separately on PHP. Version 1 rejects hardware
archive records, extracted accumulator records and compacted rollups.

## Outages, replay and durability

Persist readings in SQLite before attempting HTTP. Use a crash-durable spool
configuration and give every station storage quotas, an oldest-unconfirmed
timestamp and an explicit full-disk state. A bounded spool cannot promise
unlimited outage survival. Never silently discard unconfirmed readings; stop
the affected collection with a visible error if no configured retention policy
can make room.

Use bounded batches, timeouts, exponential backoff with jitter and `Retry-After`
for rate limiting. Retry a lost acknowledgement with the original event IDs.
Fresh data and backlog can have separate upload quotas, with fair service among
stations. Acknowledgements must identify events; a single maximum timestamp or
sequence would incorrectly confirm older events skipped to send fresh data.

The PHP side now implements:

- Original timestamps with explicit age/future-clock rejections.
- One `synchronous=FULL` SQLite transaction for journal packets, receipts,
  archive pending marks and repair work before acknowledging acceptance.
- Retention protection for unfinished native archive work, with whole-day and
  calculation run-up input retained. Unrelated expired history can be pruned.
- Historical repair even with `late_packets=ignore`, through the reception-time
  interval so later rain/counter and historical-temperature calculations change.
- Durable cursors and partial daily statistics across tick budgets and restarts;
  concurrent deliveries cannot clear newer repair work.
- Replacement of daily summaries and normal analytics-cache invalidation.
  Available LOOP extremes and existing records on older grids are retained.
- Bounded request work; the normal scheduled tick performs archive repairs.

Full timing, retention, capacity and ACK semantics are in the
[implemented contract](native-ingest-v1.md#durability-and-replay). Pending repairs
may temporarily expose provisional archive/day values. External services that
already consumed an older record do not automatically receive corrections.

Network outages and collector downtime are different cases. A running collector
can replay its spool. After power loss, hardware logger backfill can recover
only what the device stored; historical LOOP detail cannot be recreated.

Future hardware history (outside v1): poll `genStartupRecords()` and `genArchiveRecords()` independently per worker,
coordinated with its LOOP generator on the same device-owning thread. Advance
the local hardware-history cursor only after committing those records locally.
PHP must resolve LOOP/hardware overlap per station, field and time coverage so
rain is not counted twice. An exact matching hardware interval can supply an
archive record; larger or misaligned intervals must not be silently split or
overlaid onto smaller PHP intervals. This requires dedicated archive handling.

## Accumulator rollups

Distinguish replay (sending original events later) from rollup (discarding some
detail by merging events). For the first version, batching original packets
already reduces HTTP request overhead while retaining PHP's calculation model.

For example, a window containing nine temperatures of 10 degrees and another
containing one temperature of 30 degrees has a combined mean of 12 degrees.
Sending only the two means and averaging them gives 20 degrees. Min/max times,
wind vectors, missing-field counts and rain deltas introduce further differences.

A future aggregate packet must be a distinct kind, with a versioned schema:

- Station identity, units, exact `(start, stop]` coverage, source event coverage
  and processing-policy version.
- Per-observation sums, counts, weighting, min/max with timestamps, first/last
  where the observation policy requires them, and explicit missingness.
- Wind vector components, weights, square sums and gust direction/time.
- Appropriate rain sums and counter/reset information; no invented distribution
  of rainfall across a long missing period.
- A distinct current reading if live views should show the latest measurement
  rather than a window average.

Merge only disjoint, compatible ranges belonging to the same station. Source
coverage and replacement rules must prevent a raw packet and its rollup being
counted together, including retries across a compaction boundary. Aggregate
boundaries must respect every target archive grid; arbitrary future rebinning
requires retaining raw events or explicitly accepting reduced precision.

Even complete univariate accumulator statistics cannot reproduce every later
PHP calculation: dew point requires paired temperature/humidity observations,
and configurable QC may exclude individual readings. Exact behavior would
require retaining raw input or moving the relevant processing into the
collector with a fixed, versioned policy. Different archive policies make that
more complex. Mark aggregate-only history and its limitations explicitly.

Therefore introduce optional older-backlog rollups only after defining that
tradeoff. Do not send `Accum.getRecord()` as `kind=loop`, and do not rely on
today's `kind=archive` branch to combine short windows.

## Implementation sequence and acceptance

1. **Native receiver and replay — implemented in weewx-php:** authenticated
   station binding, validation, durable receipts, retention and bounded repairs.
   Receiver tests cover multi-station identity, original WeeWX Simulator
   transport, concurrent retries, clock limits, rollback, quotas and repair
   equivalence to timely input. Real USB hardware still needs collector tests.
2. **Collector — implemented:** separate upstream engine processes, persistent identity and
   spool, configurable batching, fair retries and restart supervision.
3. **Hardware history:** independent cursors for every driver, interval and
   overlap handling, recovery after collector power loss.
4. **Optional compaction:** versioned aggregate contract, merge support, live
   reading support and declared limits on subsequent recalculation.

Required verification before calling the collector compatible and reliable:

- Two Simulator instances, two instances of the same hardware driver and mixed
  drivers retain independent identities, configurations, events and measurements.
- A driver using engine callbacks (Vantage or a faithful test double) cannot
  alter another station's data; confirm with real hardware before claiming it.
- Driver failure, a blocked read and USB reconnect affect only that worker;
  device housekeeping does not invalidate an active LOOP session.
- A representative `user.*` extension installs and runs with its dependencies.
- Rain, gusts, nulls, custom observations and US/METRIC/METRICWX survive transport.
- Receiver commit followed by lost HTTP response produces no duplicate weather
  contribution; restarts preserve both queued events and confirmed identity.
- Multi-hour/day replay, an actively draining tick and cleanup leave no accepted
  events unarchived; repaired daily summaries and derived values match a run
  that received the same packets on time.
- Hardware backfill from every capable station handles overlapping LOOP data,
  mismatched grids and partial intervals explicitly.
- Future rollups are compared against original LOOP input, including unequal
  counts, wind directions across north, rain resets and changing archive policy.

The PHP receiver security review is recorded in
[weewx-php](../../weewx-php/docs/reviews/native-ingest-security.md).
Run the same review workflow for collector credentials, spool and HTTP client
implementation. PHP receiver tests do not establish hardware compatibility
or physical hardware compatibility. Collector-specific verification and its
remaining hardware limitations are documented in [implementation.md](implementation.md).
