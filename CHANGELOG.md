# memport changelog

## v1 — 2026-10-21

Initial release. Single-file stdlib CLI (`memport.py`) for portable
agent-memory export/import.

- **Archive format**: `.memport` = gzipped tar of a plain-text
  `MANIFEST.txt` (magic `MEMPORT/1`, sha256 per file) + `files/` tree.
  Full spec in FORMAT.md.
- **Commands**: `export` (glob excludes, regex line-redaction, one-way),
  `inspect --verify` (tamper detection), `import` (checksum-verified,
  refuses overwrite, rejects path traversal; `preserve`/`flat` layouts).
- **Safety**: symlinks never exported; non-UTF-8 files never redacted;
  redaction is one-way and blunt — audit with `inspect` before sharing.
- **Tests**: 13 tests (roundtrips, tamper detection, redact, excludes,
  path-traversal safety); smoke-tested on the operator's own memory tree.
- Exit codes: `0` ok, `1` verification/overwrite failure, `2` usage error.

### 2026-10-21 — publication readiness

- Added **Pre-publish checklist** section to README (per mission roadmap:
  the standalone secrets scanner was rejected as a saturated space, so the
  secrets-scan gate lives in each project's README — gitleaks/trufflehog
  before any public release).

## v1.0.1 — 2026-10-22

- README gains a Limitations section (memport-native format, blunt
  redaction, no merge on import, integrity-vs-provenance).

## v0.2.0 — 2026-10-22

- New `diff OLD.memport NEW.memport` subcommand: compares two exports by
  manifest checksums (not tar bytes, so a re-export with only timestamp
  changes diffs clean). Prints ADDED / REMOVED / MODIFIED (with byte
  deltas and sha prefixes) plus an unchanged-count summary.
- Exit codes follow the quietwatch convention: `0` identical,
  `10` differences found, `1` verification failure, `2` usage error.
- `diff --verify` checksums both archives before comparing.
- 8 new tests (identical re-export silence, modify/add/remove detection,
  verify flag, unreadable/missing archive handling); suite now 21/21.
- README quick start and exit-code docs updated; FORMAT.md unchanged
  (no format change — `diff` consumes the v1 manifest).
