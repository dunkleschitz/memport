## Unreleased

- Added `--version` flag (bench-wide CLI convention: `<tool> <VERSION>`; module-level `VERSION` constant as single source of truth).
- **v0.4.0: platform adapters.** New `collect --platform P --root DIR --out DIR`
  subcommand and `export --from P --root DIR` sugar: normalize the native
  memory layouts of `claude-code`, `cursor`, `opencode`, `codex`, `gemini`
  (plus `generic` for plain markdown trees) into `<platform>/…` +
  `SOURCE.txt` provenance, then export through the normal pipeline.
  Adapters collect instruction/memory markdown only — transcripts, logs,
  configs, and credentials are deliberately excluded (documented per
  adapter in `ADAPTERS` and in `SOURCE.txt`). Manifest gains
  `source-platform` / `source-root` header keys (FORMAT.md updated).
  16 new tests (per-platform fixtures incl. decoys, symlink refusal,
  usage errors, export/import roundtrip); suite now 45/45. Two real bugs
  caught by the new tests: `memory/**/*.md`-style globs miss one level
  deep (added explicit single-level globs), and `export --from` on zero
  matches archived a lone SOURCE.txt (now exits 2).

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

## v0.3.0 — 2026-10-22

- New `merge BASE.mport OTHER.mport --into DIR [--strategy keep-base|keep-other|both|report]`
  subcommand: merges two exports into one tree. Files present in one
  archive only are copied; identical files written once; same-path
  different-content files are conflicts resolved by strategy —
  `keep-base` (default), `keep-other`, `both` (base as-is, other as
  `<path>.other`, collision-safe), `report` (dry run, writes nothing).
- `merge` always integrity-verifies both archives first (checksum/tamper
  failure exits 1); `--into` must be new or empty (never merges over
  existing data); path traversal still rejected at parse time.
- Exit codes: `0` merge completed (or no conflicts under `report`),
  `10` conflicts found under `report`, `1` verification/target failure,
  `2` usage/read error.
- 8 new tests (per-strategy conflict resolution, dry-run semantics,
  non-empty target refusal, tampered/unreadable archives); suite 29/29.
- README quick start, options, and exit-code docs updated; FORMAT.md
  unchanged (no format change — `merge` consumes the v1 manifest).
