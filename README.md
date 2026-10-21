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
```

## Options

- `export --exclude GLOB` (repeatable): skip files, e.g. `--exclude "*.tmp"`.
- `export --redact REGEX` (repeatable): replace whole lines matching the
  regex with `[REDACTED]` before hashing — useful before sharing memory
  across platforms. Non-UTF-8 files are never touched.
- `import --layout flat`: join subdirectories with `__` instead of
  recreating the tree (handy for platforms that want one flat folder).

Exit codes: `0` ok, `1` verification/overwrite failure, `2` usage error.

## Format

See [FORMAT.md](FORMAT.md) for the full `.memport` archive specification.

## Safety

- Archives are checksum-verified before import; tampering is detected.
- Path traversal (`..`, absolute paths) is rejected at parse time.
- Symlinks are never exported.
- Redaction is one-way and blunt — audit with `inspect` before sharing.

## Tests

```bash
python3 test_memport.py   # 13 tests: roundtrips, tamper, redact, excludes, safety
```

## License

License TBD — awaiting project owner's approval (MIT proposed).
