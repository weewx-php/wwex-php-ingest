# Collector v1 implementation

Implemented on 2026-09-05 against the mirrored native v1 contract and upstream
WeeWX 5.5.0. Collector code, tests and deployment files are confined to this
project. The matching PHP receiver change enables native discovery through its existing Adopt UI/CLI.

| Module | Responsibility |
|---|---|
| `config.py` | Unified ConfigObj weewx.conf, isolated station sections, bounded settings, HTTPS and token validation. |
| `runtime.py` | Real StdEngine per worker; isolated configuration, upstream extension startup, post-callback journaling and LOOP lifecycle. |
| `spool.py` | Per-station UUID/collector binding, immutable event payloads, FULL/WAL commits, quotas and persistent retry/quarantine state. |
| `protocol.py` | v1 field/value validation, exact timestamp/unit integer encoding and identity-based ACK validation. |
| `transport.py` | Verified direct HTTPS, bounded response body, timeouts, no redirects and one credential header. |
| `uploader.py` | Fair live/backlog batches, persisted learned limits, station/global backoff and explicit event acknowledgements. |
| `supervisor.py`, `locking.py` | Worker/uploader subprocesses, silence watchdogs, independent restart backoff and exclusive OS locks. |
| `configure.py`, `update.py`, `install.sh` | Guided setup, local credentials, managed Linux installation and collector/driver updates. |
| `hardware.py` | USB/serial inventory, upstream driver catalog and bounded real-driver probes. |
| `scripts/sync_weewx.py` | Verified official source vendoring and matching WeeWX dependency pin. |
| `cli.py` | Configure/scan/update/check/init/status/run/upload-once/retry commands and secret-free collector diagnostics. |

Station identity is stored in each `state_dir/stations/<key>.sqlite3`. The module
and collector binding cannot be changed while retaining that spool. Labels and
hardware connection paths do not participate in identity. Token rotation leaves
all station/event identities untouched.

The engine journals after the complete NEW_LOOP_PACKET dispatch, including
callbacks registered by a driver during STARTUP. The lifecycle replaces only the
coordination responsibilities needed for LOOP collection. It never runs StdArchive,
calls hardware history generators, creates rollups or starts report services.
All seven upstream service groups are explicitly configured, including empty
xtype services, to avoid implicit defaults.

SQLite transactions persist each event before HTTP. Equal-valued events in one
second have different UUIDs. Failed/malformed/partial responses cannot confirm an
unidentified event; duplicate result IDs invalidate the response. Permanent event
refusals retain payloads in quarantine. Old events are submitted unchanged because
an existing receiver receipt may still acknowledge them as duplicates.

## Verification

Local environment: Windows, Python 3.14.3, bundled WeeWX 5.5.0 and the separate
weewx-php project using PHP/SQLite3. Test coverage includes:

- Two actual Simulator processes, independent IDs, source configuration and PIDs.
- A killed worker and a blocked read while the other worker continues.
- Full-spool pause and resume without worker restart or queued-event deletion.
- Startup-registered driver gust callbacks, period-end/POST_LOOP/PRE_LOOP events.
- An importable `user.*` driver fixture and numeric/null custom observations.
- All three WeeWX unit systems, original rain increments and timestamp validation.
- SQLite rollback, reopen/restart identity, immutable retry and quarantine recovery.
- Mixed acknowledgements, missing/duplicate/malformed results, station fairness,
  one-packet fairness, byte bounds, 413 splitting and admission backoff for new data.
- Real TLS certificate validation, both token headers, redirect refusal and a
  receiver commit followed by a lost response.
- The complete supervisor, two worker processes and separate uploader over HTTPS.
- The actual PHP endpoint behind a test TLS terminator: pending/adopt/store,
  lost ACK/duplicate, exact journal values and independent station receipts.
- Guided Simulator setup, generated credentials, domain normalization, USB/serial inventory,
  hardware timeout, first upload and cancelled-setup preservation.
- Standard and nested weewx.conf settings, inline token rotation and credential permissions.
- Linux installer: install/update/no-op, preserved configuration, station ID and queued event,
  and rejected update with invalid configuration.
- Vendored source and installed driver checksums, wheel/source builds, Ruff and dependency audit.

The workflow in `.github/workflows/test.yml` defines Windows/Linux and Python
3.11/3.14 checks and a Linux installer smoke test.

## Remaining acceptance boundaries

Physical Davis consoles, USB unplug/reconnect, mixed physical drivers and Raspberry
Pi storage behavior require hardware testing. The `user.*` test proves loader and
callback integration, not compatibility with every third-party extension. Upstream
`weectl` imports Unix-specific `grp` on Windows; use Linux for the documented
extension-install workflow. The collector's Simulator/runtime tests run on Windows.

The PHP integration test verifies journal/receipt delivery. Multi-day archive/day
repair equivalence remains covered by the separate PHP project's tests and was not
re-run as part of this collector suite. Hardware archive recovery and aggregate
protocols remain explicitly outside v1. No production receiver credentials or
device connection have been configured in this project.
