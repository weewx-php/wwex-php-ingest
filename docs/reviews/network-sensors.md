# GW1000 / WeatherFlow sensor review

Security-Sensitive: YES
Reviewed by: root agent, 2026-09-06
Status: PASS

## Scope

Collector: gw1000.py, weatherflow.py, network_sources.py, sensor_sources.py,
sdr.py refactor, runtime.py, hardware.py, configure.py, supervisor.py,
uploader.py, scripts/sync_gw1000.py and its scheduled workflow.
Receiver: src/Ingest/SensorSource.php, src/Ingest/NativeParser.php and the v3 schema.
The pinned upstream GW1000 source is unchanged; only ApiParser is used by the
adapter. No upstream gateway client, service, extension installer or write API
is started. Source revision, SHA-256 and GPL license ship in the wheel.

## OWASP review

| Category | Result | Evidence |
|---|---|---|
| A01 Access control | PASS | Source type/module/UUID binding checked in PHP; pending sensors cannot store measurements; adoption stays per station. |
| A02 Cryptography | PASS | HTTPS collector authentication is unchanged. LAN UDP/binary protocols are unauthenticated by design, documented for trusted LAN use. UUIDv5 is identity derivation, not authentication. |
| A03 Injection | PASS | Remote names do not select classes, files, shell commands or SQL. Fixed module allowlist and UUID-derived filenames. Existing parameterized journals retained. |
| A04 Design | PASS | Durable per-stream duplicate state and atomic rain baselines. No status-only hub station, synthetic silence values or guessed common-field sensor ownership. |
| A05 Configuration | PASS | Bounded reads/timeouts, no UDP port sharing, duplicate receiver configurations refused. Driver workers receive no ingest configuration/token environment. |
| A06 Components | PASS | pip-audit reports no known vulnerabilities; composer audit --locked reports no advisories/abandoned packages. Upstream parser checksum verified offline and inside wheel. |
| A07 Authentication | PASS | Source metadata cannot register credentials or adopt stations. No additional token flow or device write endpoint. |
| A08 Integrity | PASS | JSON duplicate/type/finite checks, binary size/command/checksum checks, inventory ID comparison before/after reads, source UUID vectors match PHP/Python. Upstream refresh uses fixed HTTPS hosts and commit-addressed downloads; tests gate publishing. |
| A09 Logging | PASS | Local collection errors and rate-limited diagnostic codes; no datagram contents or secrets logged. Receiver heartbeat kept separate from sensor freshness. |
| A10 SSRF | PASS | GW host is explicitly selected IPv4 or peer-matched LAN discovery. Only fixed read commands, no credentials sent; no URL fetched from measurement metadata. |

All ten categories reviewed. No unresolved HIGH/CRITICAL findings.

## Validation

- Full Linux suite: 194 passed before the final five additional regression cases.
- Final focused Linux suite: 72 passed (network sources, SDR and hardware).
- Full Windows suite: 188 passed, 11 skipped (POSIX/PHP prerequisites).
- PHP suite: 368 passed; final SensorSource tests: 2 passed / 39 assertions.
- PHPStan maximum level: no errors. Python lint/format and PHP file formatting pass.
- Simulated TCP GW1000 and real localhost UDP devices feed the real PHP HTTP
  endpoint through the HTTPS uploader: five independent pending/adopted stations,
  lost-response retry, no duplicate rows, forged UUID/module rejection.
- Supervisor processes discover devices and stop cleanly. Isolated guided probes
  succeed. Fragmented TCP frames, deadlines, stale sensors, channel replacements,
  inventory swaps, unknown fields, UDP silence/duplicates, malformed JSON, source
  capacity and counter reset/restart are covered.
- Wheel and source distribution build. Wheel includes parser, manifest and GPL
  license; checksum verified. Contract documentation and schema identical in both repos.
- GW1000 upstream refresh and offline verification commands executed successfully.

These are simulated-device tests; no physical GW1000 or Tempest was available.
The scheduled workflow is prepared locally and has not run on GitHub yet.

## Publication check (2026-09-06)

The selected commit trees were exported and tested independently of unrelated
backup changes in the PHP working directory: collector 199 tests passed against
the selected PHP receiver; PHP 359 tests passed; PHPStan maximum level clean;
four chart tests passed. Bundled WeeWX and GW1000 checksum checks passed.
