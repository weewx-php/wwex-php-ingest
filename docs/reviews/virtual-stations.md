# Virtual station review

Reviewed 2026-09-05 by Codex (`/root`).

## Behavior and verification

The collector's `Virtual` driver generates metadata-only LOOP ticks. Explicitly
configured instance-local services add observations; empty/null-only packets are
skipped before serialization. All emitted measurements still pass through the
existing protocol validation, durable spool, HTTPS upload and admission path.
Hardware record generation is refused for virtual instances.

The guided setup offers Virtual, service classes, and PurpleAir host/port settings.
Its probe waits for an actual service-enriched packet. Each virtual station runs
in its own worker process with its own configuration and persistent station UUID.

The unchanged upstream PurpleAir 7.2 service is pinned at
`e433d858c33c9f5c2e0083f27baa48f190d9feb8`, checksum-verified and retained with its
license as a test-only fixture. It is not installed by the runtime package.

Verified with two simulated local PurpleAir HTTP APIs and separate real worker
processes: distinct sensor fields/identities, stale-data suppression and recovery,
no invented weather values, setup/probe behavior and software-only operation.
The real PHP endpoint test covers two independent adoptions, exact journal fields,
verified TLS and a lost ACK followed by duplicate-safe replay.

Full-suite results: Linux/Python 3.13 with real PHP integration: 125 passed.
Windows/Python 3.14: 121 passed, 4 skipped (PHP integration and POSIX-only behavior).
Linux runtime tests used a read-only container and no external network access.
No physical PurpleAir device was tested.

## Security review

**Security-Sensitive: YES. Reviewer: `/root`. OWASP categories checked: 10/10.**

| Category | Result |
|---|---|
| Access control | Existing isolated workers, per-station service configuration, token binding and normal PHP adoption apply. No shared service registry across processes. |
| Cryptography | Upload TLS and credential handling unchanged. Local sensor HTTP is limited to the administrator-configured upstream service; no ingest token is passed to it. |
| Injection | Service module notation is validated; the existing trusted-administrator extension mechanism imports it. No new shell execution or SQL construction. |
| Insecure design | Fresh metadata-only dictionary per tick; no fabricated weather fields, no empty events, no local hardware aggregates. |
| Misconfiguration | Bounded finite tick interval, enumerated unit systems and software-only configuration. Tests exercise invalid settings and empty probes. |
| Components | Original PurpleAir file hash checked; GPL license retained. Test dependencies isolated from runtime requirements. Local pip-audit found no known vulnerabilities. |
| Authentication | Existing uploader credentials and station adoption unchanged; PHP integration exercises both stations independently. |
| Data integrity | Numeric/null field validation, immutable event IDs, transactional spool and ACK matching retained. Real receiver test verifies exact data and deduplication. |
| Logging/monitoring | Waiting-for-service status; empty ticks do not reset the measurement watchdog. Existing supervisor handles stalled services. |
| SSRF | Service destinations remain explicit trusted local configuration, never supplied by the PHP receiver or an upload payload. Fake API tests use loopback only. |

No XML parser, HTML renderer or object-deserialization path was introduced.
Services remain trusted local code; process separation prevents accidental shared
state, not hostile code running under the same OS account. A service's freshness,
unit conversion and caching policy remain its responsibility.

**Security Review Status: PASS.**
