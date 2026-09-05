# Collector security review

Reviewed: 2026-09-05. Reviewer: Codex, primary implementation agent.
Security-sensitive: **YES**. Review status: **ISSUES_FIXED**.

Scope: all collector Python modules, packaging/dependencies, example configuration,
systemd service and transport/database integration tests. The matching PHP receiver discovery and existing Adopt UI/CLI integration were also reviewed. Review followed the local security-review skill's OWASP checklist.

| Category | Result | Evidence |
|---|---|---|
| Injection | PASS | SQLite values use parameters; child processes use argument arrays, no shell. UUID/module/observation names are validated. |
| Authentication | PASS | Exactly one configured token header. Native token format enforced. No credential CLI argument or URL parameter. |
| Sensitive data / cryptography | PASS | Verified HTTPS and TLS 1.2 minimum; redirects/proxies disabled; optional CA file, no insecure switch. Credential files, including weewx.conf, require mode 0600 on POSIX; setup uses secrets.token_hex(32). |
| XML / external entities | N/A | Collector parses ConfigObj/JSON, not XML. |
| Access control | PASS | Local OS permissions and process locks; immutable collector/station binding. Receiver continues to enforce admission/authentication. |
| Misconfiguration | PASS | Strict setting names/bounds, explicit service groups, no default report/archive/external upload services, unprivileged systemd account. |
| XSS | N/A | No web UI or HTML rendering. |
| Deserialization / integrity | PASS | Bounded JSON responses; duplicate members/results and invalid identities rejected. Stored event payloads never change on retry. |
| Vulnerable components | PASS at review time | `pip-audit --skip-editable` reported no known vulnerabilities in the installed environment. WeeWX pinned to 5.5.0; configobj at least 5.0.9. |
| Logging / monitoring | PASS | Collector logs stable failure codes, station keys and process status; never token or HTTP body. Persistent queue/age/quarantine/retry status. |

## Findings resolved during implementation

| Finding | Resolution |
|---|---|
| Newly collected events could bypass pending-station backoff. | Persist station-wide upload delay; admitted stations continue independently. |
| Independent SQLite reads could see different candidate counts while a worker committed. | Candidate interleaving tolerates concurrent insertions without failing the uploader. |
| A proxy's smaller request limit could be forgotten on restart. | Persist learned packet/byte bounds and only tighten them after successful replies. |
| Station keys could collide with supervisor/uploader lock names. | Reserve `collector` and `uploader`. |

## Verification and operational boundary

Tests check invalid endpoint/credential injection, untrusted certificates, no
redirect following, both exclusive auth headers, lost ACK with duplicate recovery,
malformed/duplicate ACK refusal, transactional rollback and spool identity binding.
The PHP integration test checks that the receiver's server log contains no token.

The uploader rereads its configured token on each request. The unified config is
read by workers, but Ingest credentials are removed before passing configuration
to the driver. Workers do not inherit the configured token environment variable. They share the
service account, so process isolation is not a security sandbox against malicious
local extensions. Treat driver code, config files and import paths as trusted local
administration inputs. Collector logging suppresses upstream logger records by
default; an arbitrary extension can still print its own output.

Weather data and driver credentials are not encrypted on disk by this package.
Protect the service account/config directories and backup media; use OS disk
encryption when required. SQLite FULL/WAL durability depends on filesystem and
device guarantees. There are no deferred critical or high findings. No physical
hardware compatibility or independent external security audit is claimed.

## Guided installation, updates and discovery

- Root-managed release sources have group/other writes removed, are built separately,
  checked against the vendored manifest,
  and activated only after configuration validation. Paths and repository URLs are
  restricted; external requirements are trusted administrator input.
- Configuration writes are atomic, mode 0600, and generated tokens never appear in
  CLI arguments or diagnostic output. Hardware reads run with a hard timeout in a
  child process under the installed service account. No raw driver output is exposed during the probe.
- Upstream source downloads use HTTPS, bounded sizes and SHA-256 verification.
  Extraction rejects traversal, links and special files. Original licenses remain.
- The PHP receiver accepts a new locally generated token only after HTTPS, rate,
  size and complete packet validation. It stores a digest and creates pending
  stations. Adoption is performed by the existing authenticated admin/CLI path.
- Existing IDs cannot be rebound to another token, rotated/disabled tokens cannot
  rediscover an existing collector, and no pending data reaches the journal.
  Discovery is bounded by max_pending, 100 collectors and 2,000 native stations.
  Public discovery can consume pending capacity; the limit returns 503 without
  discarding collector queues. The user explicitly authorized this admission model.
- A regression test caught rate checks moving behind body reads. Known collectors
  now enforce their limit before parsing, with token revalidation in the write
  transaction. Native receiver/Admin/CLI tests pass (42 tests, 598 assertions).
- The discovery test covers normal Adopt, token takeover/rebinding, disabled tokens,
  rejection, duplicate delivery and pending capacity. Dependency audit: no known
  vulnerabilities at review time.
