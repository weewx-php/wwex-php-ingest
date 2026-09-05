# Air-quality service profiles review

Reviewed 2026-09-05 by Codex (`/root`). Builds on the virtual station implementation.

## Scope and result

Virtual setup accepts named profiles for PurpleAir, AirGradient, AirLink and air-Q,
or explicit Python service classes. Profiles configure the instance's local
sensor, preserve custom mappings and install no third-party code automatically.
Virtual engine configuration now supplies explicit software archive interval and
delay settings required by services such as AirLink.

AirGradient receives a default numeric field mapping only when its mapping is
empty. TVOC/NOx raw values and indices retain distinct names. air-Q adds its units
service and reads the device password with a hidden prompt. Text/status fields
are excluded. Upstream air-Q `no2` is excluded because its fraction semantics
conflict with PHP's mass-concentration definition; `no2_m` remains available for
explicit mapping. Exclusions follow any configured device prefix.

## Verification

- Linux/Python 3.13 with the real PHP receiver: **147 tests passed**.
- Windows/Python 3.14: **140 passed, 7 skipped** (six PHP integration cases and
  one POSIX-only case).
- Original AirGradient 4.0, AirLink 4.1 and air-Q 0.9b3 source files are retained
  unmodified, checksum-verified and licensed under `tests/fixtures/*_upstream/`.
- Each service was run in two separate worker processes against two simulated
  local device APIs. air-Q `/config` and `/data` used AES-encrypted JSON replies.
- Tests verify instance separation, real enriched LOOP probes, invalid-data
  timeouts, numeric field values, normal PHP adoption and exact journal storage.
- Profile tests verify custom mapping preservation, hidden/retained device
  passwords, malformed host input and prefixed field exclusions.
- No external network was available during the Linux runtime tests. No physical
  sensors, public receiver or production credentials were used.

Device-specific limitations remain upstream behavior: the tested air-Q service
does not enforce wall-clock age on readings, and virtual instances do not enable
proxy archive recovery. Additional units/XTypes are not transferred to PHP.

## Security review

**Security-Sensitive: YES. Reviewer: `/root`. OWASP categories checked: 10/10.**

| Category | Result |
|---|---|
| Access control | Profiles remain per-instance; each worker runs its own engine and credentials/configuration. Existing receiver adoption and authentication unchanged. |
| Cryptography | Ingest TLS unchanged. air-Q device password uses getpass, is never displayed as a default and is written through the existing protected configuration flow. AES protocol is supplied by the original service. |
| Injection | Service names use validated module notation. Host/port prompts reject credentials, schemes, paths, control characters and invalid ports. No new shell or SQL interpolation. |
| Insecure design | No automatic third-party installation or trust escalation. Distinct raw/index fields and no2 exclusion prevent known semantic mislabeling. |
| Misconfiguration | Required air-Q prep service added, AirGradient mapping supplied when absent, Virtual archive settings explicit; empty data remains untransmitted. |
| Components | Source fixtures pinned to hashes/commits with original licenses. Test-only dependencies include pycryptodomex and six. pip-audit reports no known vulnerabilities. |
| Authentication | Sensor passwords are separate from ingest tokens; no token registration or admission bypass introduced. PHP integration verifies distinct station admission. |
| Integrity | Existing strict numeric/null contract applies. Text exclusions are explicit; no unrestricted coercion or global silent dropping of invalid fields. Receiver tests compare stored data to queued events. |
| Logging | Profile output contains no device password. Tests assert passwords absent from worker logs and prompt output. Third-party logs remain subject to the existing collector logging filter. |
| SSRF | Sensor endpoints are explicit trusted local administrator configuration, not upload or server-provided input. Tests use loopback APIs only. |

No XML parser, HTML renderer or object deserialization was introduced.
The unchanged third-party services remain trusted local code; process separation
isolates their state, not malicious code with the same OS privileges.

**Security Review Status: PASS.**
