<!-- Mirrored from D:/Git/weewx-php/docs/native-ingest.md on 2026-09-05. -->

# Native WeeWX ingest: version 1

Implemented endpoint: **`POST /ingest/weewx.php`**. The Python collector runs
separately on a Raspberry Pi or another machine in the station owner's LAN.
It initiates HTTPS requests to weewx-php on the web host. PHP needs no Python,
USB access, background daemon or inbound connection into the home network.

Version 1 accepts original **LOOP observations in batches**. Hardware archive
records, accumulator summaries and rollups are not accepted. The send interval
controls request frequency; every observation retains its own time and values.

## Set up the receiver

Serve `public/`, place the data directory outside the document root, and configure:

```ini
live_retention = 14d

[Ingest]
    enabled = true
    public_url = https://weather.example.org
    tick_mode = external
```

The real PHP file works without URL rewriting and under a subdirectory, for
example `https://example.org/weather/ingest/weewx.php`. Schedule the existing
tick, preferably every minute. `tick_mode = auto` can also trigger it after
FastCGI finishes a successful response; other runtimes still need the scheduled
tick. No archive work runs before the collector's HTTP acknowledgement.

The collector installer generates a collector UUID and a 256-bit random token.
Run its guided `weewx-php-ingest configure` assistant and enter this server's domain.
No collector registration or token copying is required.

The first valid upload discovers the collector and its station IDs. Only the token's
SHA-256 digest is stored in `live.sdb`; the collector retains the token in its
protected `weewx.conf`. Existing UUID/token bindings cannot be replaced by discovery.

New stations appear as pending in the normal administration station list. Use the
existing Adopt button, or:

```sh
php bin/weewx-php ingest list
php bin/weewx-php ingest adopt <sender> "Davis"
```

The response before adoption is `pending`, with a stable `sender`. No weather data
has reached the journal, so the collector keeps its events for retry. Discovery
uses the configured pending limit and a global cap of 100 collectors and 2,000
native stations. Known tokens retain per-collector request limits before body reads.

The next delivery, including retries of the original events, can be stored.
Adopted stations are available to configuration and normal archive field
assignment without a manual `[Stations]` entry. Use the returned `sender`:

```ini
[Archives]
    [[garden]]
        primary = weewx_<returned_suffix>
        senders = weewx_<returned_suffix>
```

Two driver instances, including two instances of the same module, must use
different station UUIDs. Keep these UUIDs across restarts, software updates,
USB port changes and label changes. The server derives identity from
`collector_id/station_id`; a collector never chooses the journal's sender ID.
Replacing the collector ID creates a new namespace. Identity migration is not
part of v1; preserve its ID and configuration when replacing the Raspberry.

Admission uses the existing administration UI or ingest CLI. Archive assignment
and station configuration work with admitted native senders.

## HTTP request

Use exactly one credential header:

```http
Authorization: Bearer <token>
```

or, on shared hosts that strip `Authorization` before invoking PHP:

```http
X-WeeWX-Token: <token>
```

Supplying both fails authentication. Query parameters, form bodies, cookies
as credentials and plaintext HTTP are not supported. There are no redirects.
Set `Content-Type: application/json`; optional media-type parameters such as
`charset=utf-8` are accepted. UTF-8 is required. Request compression is not
supported; `Content-Encoding` must be absent or `identity`.

For TLS termination at a reverse proxy, configure the existing exact
`trusted_proxies` IP list. The trusted proxy must replace `X-Forwarded-For`
and `X-Forwarded-Proto`. Untrusted forwarding headers cannot enable HTTPS.

```json
{
  "version": 1,
  "collector_id": "6a069536-840e-4d56-8bd0-05b18783a412",
  "packets": [
    {
      "station_id": "f7b175bd-5130-4568-94b5-54f1e3e5d3fd",
      "event_id": "a5ec85d9-3e34-4bd6-a785-41c3fc64ce19",
      "driver_module": "weewx.drivers.vantage",
      "kind": "loop",
      "dateTime": 1788600000,
      "usUnits": 17,
      "data": {
        "outTemp": 18.7,
        "windSpeed": 2.5,
        "rain": 0.2,
        "extraTemp1": null
      }
    }
  ]
}
```

The example timestamp is illustrative; new events must satisfy the age limits.
The [request JSON Schema](../resources/ingest/weewx-v1.schema.json) describes the
shape. The receiver additionally enforces timestamp age, finite numbers,
case-insensitive reserved names, duplicate member/event checks and credentials.

| Property | Contract |
|---|---|
| `version` | JSON integer `1` |
| `collector_id`, `station_id`, `event_id` | Lowercase UUID text in `8-4-4-4-12` hexadecimal form |
| `event_id` | Assigned once when the local observation is persisted; unique across all stations of that collector; never reused |
| `driver_module` | Python module notation, at most 160 bytes; metadata only, never imported by PHP |
| `kind` | Exactly `loop` |
| `dateTime` | Positive JSON integer Unix seconds, original measurement time; no strings, fractional or exponent notation |
| `usUnits` | JSON integer `1` (US), `16` (METRIC) or `17` (METRICWX) |
| `data` | Object containing 1–256 finite numeric or null observations, without `dateTime`/`usUnits` metadata |

Field names match `[A-Za-z][A-Za-z0-9_]{0,63}`. `dateTime`, `usUnits`,
`interval`, `source`, `sender`, `identity`, `driver` and `kind` are reserved in
`data`, in any letter case. Strings, booleans, arrays and nested measurement
objects are rejected. Missing observations remain absent; explicit null stays
null. A batch may mix stations and WeeWX unit systems; each packet has one
unit system. Standard LOOP `rain` is an incremental amount, not a lifetime
counter. Preserve the driver's semantics.

Unknown observation names are retained in the journal. Define custom
measurement kinds, target columns and field assignments in PHP as necessary;
v1 does not transfer Python unit registries or accumulator code. Driver-specific
metadata belongs in the envelope, not in `data`. Unknown envelope properties
and duplicate JSON members, including escaped spellings of the same key, are
rejected. Event IDs must also be distinct within a batch.

## Limits

| Limit | Value |
|---|---|
| Uncompressed request body | 262,144 bytes |
| Events per request | 1–128 |
| Fields per event | 1–256 |
| Future clock allowance | 60 seconds |
| Maximum event age | `min(7 days, live_retention - 2 days)` |
| Receipt retention | 30 days after initial acceptance |
| Registered collectors | 100 |
| Discovered native stations | 2,000 total; pending per collector limited by existing `max_pending` |
| Retained native receipts | `max_native_receipts`, default 2,000,000 total; new writes return 503 at capacity |

With the application's default `live_retention = 7d`, event age is limited to
five days. With `14d`, it is seven days. Retention of two days or less returns
`retention_too_short` for this endpoint. The extra two days preserve input for
daily extrema and calculation run-up. Successful responses include effective
limits; clients should also configure these limits before their first request.

The existing `requests_per_minute` limits each peer, with a global allowance
of ten times that value. `sender_requests_per_minute` limits each authenticated
collector across all its stations and source IPs. Defaults are 300 and 120
requests/minute respectively. Batching reduces requests, not measurement detail.

Size `max_native_receipts` for at least 30 days of the combined observation rate,
plus headroom. One station producing a packet every 2.5 seconds needs about
1,036,800 receipts for 30 days. For example, four such stations need more than
4,147,200; configure at least 5,000,000 and provision the corresponding disk
space for receipts, journal data and SQLite indexes. Batching does not reduce
receipt count. Capacity errors never discard accepted data to make room.

## HTTP response and acknowledgement

Responses are JSON with `Cache-Control: no-store`. A valid processed batch
returns HTTP 200, even if some events are pending or rejected:

```json
{
  "version": 1,
  "status": "ok",
  "results": [
    {
      "event_id": "a5ec85d9-3e34-4bd6-a785-41c3fc64ce19",
      "station_id": "f7b175bd-5130-4568-94b5-54f1e3e5d3fd",
      "sender": "weewx_<server_generated_suffix>",
      "status": "stored"
    }
  ],
  "limits": {
    "max_bytes": 262144,
    "max_packets": 128,
    "max_fields": 256,
    "max_age_seconds": 604800,
    "future_skew_seconds": 60,
    "receipt_retention_seconds": 2592000,
    "max_receipts": 2000000,
    "kinds": ["loop"]
  }
}
```

Match results by event ID and station ID. Results follow request order, but
position alone must not be used as acknowledgement. A missing, malformed or
unrecognized result confirms nothing. `sender` is present for admitted or
discovered stations, but may be absent for an event rejected before discovery.

| Event status | Meaning | Collector action |
|---|---|---|
| `stored` | Journal entry, receipt and pending/repair work committed | Release this local event |
| `duplicate` | This event was already accepted with the same content | Release this local event |
| `pending` | Station awaits admission; weather event is not stored | Retain; retry with backoff after admission |
| `rejected` | Event was not accepted; inspect `reason` | Retain/quarantine for diagnosis; do not mark delivered |

Event rejection reasons are `event_conflict`, `too_old`, `future_timestamp`,
and `station_blocked`. A conflict means that an event ID was reused with
different station, driver module, time, units or observations. Fix the producer;
do not replace an already accepted observation by changing an event's content.
Changing only JSON object member order does not change its identity digest.

Each new source observation gets its own event ID, even if two observations
have equal values within the same second. The native journal identity preserves
both. Retrying an observation must reuse its ID; generating new IDs on retries
would count rainfall and samples again. Ecowitt/WU measurement deduplication
is unchanged.

Malformed envelopes/packets reject the **whole batch before discovery or writes**.
Semantic event rejections above can coexist with successful events in one batch.
A storage failure rolls back all weather entries, receipts and repair work in
that batch. The response then confirms nothing:

```json
{"version":1,"status":"error","error":"unavailable"}
```

| HTTP status | Typical errors | Action |
|---|---|---|
| 400 | `invalid_json`, `duplicate_property`, `invalid_id`, `unsupported_version`, `unsupported_kind`, `invalid_timestamp`, `invalid_units`, `invalid_field`, `invalid_value`, `invalid_packet_count`, `duplicate_event_id`, `unknown_property`, `query_not_allowed` | Correct producer/configuration; retain events |
| 401 | `unauthorized` | Check token, token rotation and disabled collector |
| 403 | `https_required`, `collector_mismatch` | Correct transport/collector configuration |
| 404 | `disabled` | Enable `[Ingest]` or correct endpoint |
| 405 | `method_not_allowed` | Use POST |
| 413 | `payload_too_large` | Split the batch; preserve event IDs |
| 415 | `unsupported_content_type`, `unsupported_content_encoding` | Send plain UTF-8 JSON |
| 429 | `rate_limited` | Honor `Retry-After: 60`, then back off with jitter |
| 503 | `unavailable`, `station_capacity`, `receipt_capacity`, `retention_too_short` | Retain; retry with backoff or resolve the reported configuration/capacity limit |

Hosts/proxies may return their own HTML errors. Treat them as unconfirmed
delivery. Authentication failures, unsupported types and rate limits are checked
before the PHP adapter reads the body. The web server still owns request upload
timeouts and its outer request-size limits.

## Durability and replay

`live.sdb` uses SQLite `synchronous=FULL`. Accepted packets, event receipts,
pending archive marks and replay cursors commit in one transaction. A crash
after commit but before the response is handled by resending the same event IDs.
Authentication and station admission are checked again inside that transaction,
so rotation/blocking cannot be bypassed using an earlier authorization check.

An ACK means durable journal acceptance under the host's storage guarantees;
it does not mean the tick has completed archiving. Normal journal retention
still applies after pending work is processed. Receipts remain for 30 days,
so an already accepted retry can return `duplicate` after its journal row was
pruned. Beyond the receipt horizon the original timestamp is too old to create
a second entry. Never reuse an event ID for a new observation.

Every accepted native event is protected from cleanup until its relevant
pending archive work finishes. The pruning cutoff is held back to preserve
unfinished days, the other stations and calculation run-up needed for repair.
Unrelated expired history still gets pruned during continuous live delivery.
Removed archives release their protection during maintenance;
disabled or failing archives retain their work and can prolong disk use. With
no configured enabled archive, acceptance stores journal history subject to
ordinary retention; it cannot promise future archival after that history expires.

Late native input schedules a repair from the affected local day through the
reception-time archive interval. It deliberately includes later intervals,
since rain deltas/rates and historical-temperature calculations can change.
This repair runs even with `late_packets = ignore`; that setting retains its
existing behavior for Ecowitt/WU and other journal producers.

The tick processes repairs under its existing time and interval budgets. Its
cursor and partial daily statistics survive a restart. Concurrent delivery
invalidates a stale checkpoint; it cannot acknowledge that newer repair work
as complete. Daily statistics are replaced after the repaired day is traversed,
which removes obsolete extremes and retains LOOP extremes. Existing records
without available LOOP input are preserved. Available analytics caches are
invalidated through the normal archive-change hooks. Values can be provisional
while a repair is pending; `tick` reports `replay_pending` for that archive.

The receiver preserves historical times and never substitutes reception time.
Recovery is limited to retained/received observations. It cannot reconstruct
LOOP packets lost while the Raspberry itself was off, nor resend corrections
to external weather services that already consumed an older archive result.

## Credential and station lifecycle

```sh
php bin/weewx-php collector rotate <collector_id>
php bin/weewx-php collector disable <collector_id>
php bin/weewx-php collector enable <collector_id>
php bin/weewx-php collector block <collector_id> <station_id>
php bin/weewx-php collector adopt <collector_id> <station_id> "Davis"
```

Rotation displays a replacement token once, immediately revokes the old one,
and preserves collector/station IDs, admission, receipts and weather history.
Disabling a collector rejects all its requests. Blocking a station rejects new
events for it; retries already durably acknowledged can still return `duplicate`.
Adoption re-enables that station without creating another sender.

## Collector implementation checklist

1. Persist original LOOP observations with stable station/event UUIDs before HTTP.
2. Send batches within the advertised byte, count and age limits. Preserve units,
   nulls, rain increments and measurement times; serialize valid JSON integers.
3. Resolve each explicit result. Release only `stored`/`duplicate`; HTTP 200 alone
   is insufficient. On uncertain delivery retry identical events.
4. Keep fresh and historical queues fair across stations. Per-event ACKs allow
   fresh events to be sent before a backlog without falsely confirming it.
5. Back off on pending admission, timeouts and temporary refusals. Keep permanent
   rejections visible without an immediate endless retry loop.
6. Report oldest unconfirmed event, queue size/full state, last success and
   rejection reason. Provision enough local storage for the desired outage span.
7. Do not send hardware archives or aggregate summaries to v1. Negotiate a future
   version when support is implemented on both sides.

## Verification

Unit tests cover admission, distinct equal-valued source events, immutable
receipts, rotation/blocking, clock bounds, malformed JSON, rollback, repair
checkpoint races, stopped ticks, retention protection and restart recovery.
The replay comparison checks archive records and all WeeWX daily summary tables
against the same observations received on time.

The `native_ingest` conformance check runs the real PHP file in a subdirectory
without a router, sends original WeeWX Simulator output, races eight HTTP
requests, and verifies one durable event/receipt plus seven duplicate ACKs.
It also checks both token headers, plaintext refusal, revocation and secret-free
application/server logs. Existing `ingest` conformance covers Ecowitt and WU.
