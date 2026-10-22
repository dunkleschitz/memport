# memport — move agent memory between platforms

Agent platforms all keep memory differently — one has `MEMORY.md` + dated
notes, another has a sqlite blob, a third has nothing exportable at all.
memport is the dumb, portable middle layer: pack a memory directory into a
documented plain-text archive, audit it, verify it, and unpack it somewhere
else. No accounts, no servers, no dependencies beyond Python 3.

Single file, stdlib only: `memport.py`.

## Quick start

```bash
# Export this agent's memory tree
python3 memport.py export --source ~/memory --out backup-2026-10-21.memport

# See what's inside, and verify checksums
python3 memport.py inspect backup-2026-10-21.memport --verify

# Import it elsewhere (refuses to overwrite, refuses tampered archives)
python3 memport.py import backup-2026-10-21.memport --target ./restored

# Diff two snapshots — what changed in memory since the last export?
python3 memport.py diff backup-2026-10-20.memport backup-2026-10-21.memport
```

## Options

- `export --exclude GLOB` (repeatable): skip files, e.g. `--exclude "*.tmp"`.
- `export --redact REGEX` (repeatable): replace whole lines matching the
  regex with `[REDACTED]` before hashing — useful before sharing memory
  across platforms. Non-UTF-8 files are never touched.
- `import --layout flat`: join subdirectories with `__` instead of
  recreating the tree (handy for platforms that want one flat folder).

Exit codes: `0` ok, `1` verification/overwrite failure, `2` usage error.
`diff` follows the quietwatch convention instead: `0` identical,
`10` differences found, `1` verification failure, `2` usage error —
so it composes with watch/alert pipelines.

## Format

See [FORMAT.md](FORMAT.md) for the full `.memport` archive specification.

## Safety

- Archives are checksum-verified before import; tampering is detected.
- Path traversal (`..`, absolute paths) is rejected at parse time.
- Symlinks are never exported.
- Redaction is one-way and blunt — audit with `inspect` before sharing.

## Limitations

- **Format is memport-native.** The receiving side needs memport (or an
  independent reader of the documented FORMAT.md) to unpack; there is no
  importer for any platform's proprietary memory store.
- **Redaction is blunt.** `--redact` matches whole lines against a regex;
  it is not a semantic scrubber, and non-UTF-8 files are never redacted
  (they pass through untouched). Always `inspect` before sharing.
- **No merging.** Import refuses to overwrite; reconciling two divergent
  memory trees is manual work.
- **Integrity, not provenance.** A tampered archive is detected, but an
  honestly exported archive of junk memory is still junk — checksums
  can't tell you the source was worth keeping.

## Tests

```bash
python3 test_memport.py   # 21 tests: roundtrips, tamper, redact, excludes, safety, diff
```

## Pre-publish checklist

Per the project charter, nothing ships publicly until the maintainer
approves the publishing account/org and the license. Before
first publication:

1. **Secrets hygiene** — run a secrets scan over the repo and its history
   (`gitleaks detect --source .` or `trufflehog git file://.`), resolve
   every hit, and grep once more for obvious keys/tokens by hand.
2. **History review** — read `git log` and the full diff end-to-end; no
   credentials, private paths, or personal data may be committed.
3. **License** — replace the TBD notice with the maintainer-approved
   LICENSE file.
4. **Approval** — get the maintainer's go-ahead for this project's first
   public publication.

## License

License TBD — awaiting project owner's approval (MIT proposed).
