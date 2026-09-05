# RTL433 sensor stations: security review

Security-Sensitive: YES. Reviewed by: primary implementation agent.
Scope: SDR decoder process/input, dynamic journals and upload discovery, native
v3 parsing/receipts/adoption, Admin ReadModel, installer and update dependencies.
Status: PASS. No unresolved critical or high findings.

| OWASP category | Result | Evidence |
|---|---|---|
| A01 Access control | PASS | Token remains collector-bound; each derived sensor UUID has independent adoption/block state; pending packets do not write measurements. |
| A02 Cryptographic failures | PASS | HTTPS transport unchanged; no new secret; metadata participates in receipt digest. UUIDv5 identifies a sensor, not an authenticated radio device. |
| A03 Injection | PASS | Fixed subprocess argv, restricted device/frequency values, parameterized SQL; UUID-derived paths; existing escaped Admin rendering. |
| A04 Insecure design | PASS | Bounded sensor count, line length and pipe queue; durable individual queues; no fake observations for silent sensors; no adoption through metadata. |
| A05 Misconfiguration | PASS | Implicit rtl_433 config disabled; no extra output destinations or shell; duplicate configured device selectors rejected; probe and worker descendants terminated. |
| A06 Vulnerable components | PASS | Python pip-audit reports none. Composer lock audit reports no advisories or abandoned packages. rtl-433 comes from configured OS repositories and is included in collector update. |
| A07 Authentication failures | PASS | Existing collector/token mismatch checks unchanged and covered by PHP tests; configured token environment variable removed from worker and probe environments. |
| A08 Data integrity | PASS | Strict source shape and deterministic UUID validation on both sides; duplicate JSON members/nonfinite values rejected; counter baseline and event commit together. |
| A09 Logging | PASS | Bounded reason codes in status; malformed decoder diagnostics rate-limited; no decoder text or tokens in new logs. |
| A10 SSRF | PASS | Radio metadata cannot choose a URL; no network decoder mode accepted; uploader uses the configured validated HTTPS endpoint. |

Full changed input/API/storage files were reviewed. Radio broadcasts themselves
are not authenticated: indistinguishable or spoofed model/ID/channel values cannot
be separated by this adapter. Local dependencies and their package repositories
remain part of the operating system trust boundary.

## Verification

- Linux collector suite: 168 passed, including real PHP HTTP handling behind TLS.
- Windows collector suite: 158 passed, 10 skipped (optional PHP/POSIX scenarios).
- PHP complete suite: 357 passed. The 15 native receiver tests were rerun after
  test type corrections: 241 assertions passed.
- PHPStan, PHP CS Fixer on changed files, Ruff and `git diff --check`: pass.
- Python wheel and source distribution build; installer `bash -n`: pass.
- Same-model, same-ID sensors on different channels adopt independently; new
  sensors discovered after uploader startup; lost ACK does not duplicate data.
- Decoder worker crash/restart preserves sensor IDs and reaps the old process;
  silent probe timeout reaps the decoder; receiver holds no measurement events.
- Rain baseline survives restart/full-queue retry, resets do not invent rain;
  sensor capacity, malformed input, forged source/UUID and block isolation tested.

Radio integration uses simulated rtl_433 JSON output and a decoder subprocess.
No physical SDR stick, RF reception or full privileged OS installation was tested.
