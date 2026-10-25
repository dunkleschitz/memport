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

# Collect another platform's native memory layout, then export it
python3 memport.py export --from claude-code --root ~/projects/myapp \
    --out myapp-context.memport

# Or collect first (inspectable tree + SOURCE.txt provenance), export later
python3 memport.py collect --platform cursor --root ~/projects/myapp --out ctx-tree
python3 memport.py export --source ctx-tree --out ctx-tree.memport

# See what's inside, and verify checksums
python3 memport.py inspect backup-2026-10-21.memport --verify

# Import it elsewhere (refuses to overwrite, refuses tampered archives)
python3 memport.py import backup-2026-10-21.memport --target ./restored

# Diff two snapshots — what changed in memory since the last export?
python3 memport.py diff backup-2026-10-20.memport backup-2026-10-21.memport

# Merge two agents' exports into one tree (conflicts resolved by strategy)
python3 memport.py merge base.memport other.memport --into ./merged \
    --strategy keep-base   # or keep-other | both | report (dry run)

# Forget files from an archive — audited, checksummed, never silent
python3 memport.py forget backup-2026-10-21.memport --path 'memory/2026-09-*.md' \
    --reason 'stale' --output pruned.memport   # or --in-place, or --dry-run
```

## Platform adapters (`collect`, `export --from`)

`export --source` packs any directory you point it at. `collect` and
`export --from` go one step further: they know where each major agent
platform keeps its memory and instruction files, gather them into a
normalized tree (`<platform>/…` plus a `SOURCE.txt` provenance record),
and hand that tree to the normal export pipeline. Point `--root` at a
project root for project-scoped files, or at the platform's home
directory (e.g. `~/.claude`) for user-scoped files.

| Platform | `--root` examples | What gets collected |
|---|---|---|
| `claude-code` | project root · `~/.claude` · `~/.claude/projects/<slug>` | `CLAUDE.md` (+ nested), `CLAUDE.local.md`, `.claude/rules/*.md`, personal `rules/*.md`, auto-memory `memory/*.md` |
| `cursor` | project root | `.cursor/rules/*.mdc`, `.cursorrules`, `AGENTS.md` |
| `opencode` | project root | `AGENTS.md` (+ nested), `.opencode/**/*.md` |
| `codex` | project root | `AGENTS.md` (+ nested), `AGENTS.override.md` |
| `gemini` | project root · `~/.gemini` | `GEMINI.md` (+ nested), `.gemini/GEMINI.md` |
| `generic` | any markdown memory tree | `MEMORY.md` (+ nested), `memory/**/*.md`, `AGENTS.md` |

Deliberately **never** collected: session transcripts, chat logs, prompt
history, `settings.json`/TOML configs, MCP configs, and anything
credential-shaped (`auth.json`, tokens). Each adapter documents its exact
exclusions in `memport.py` (`ADAPTERS`) and in the tree's `SOURCE.txt`.
Transcripts are session-scoped and often huge; configs hold secrets and
machine-specific settings — neither is portable memory.

Caveat: adapters track each platform's *documented* layout as of Oct
2026. Platforms move files around; if `collect` finds nothing it says so
(stderr warning, `export --from` exits 2). Check `SOURCE.txt`'s `globs:`
line to see exactly what was looked for.

## Options

- `export --exclude GLOB` (repeatable): skip files, e.g. `--exclude "*.tmp"`.
- `export --name NAME`: archive name recorded in the manifest (default:
  the output file's basename).
- `--version`: print the version and exit.
- `export --redact REGEX` (repeatable): replace whole lines matching the
  regex with `[REDACTED]` before hashing — useful before sharing memory
  across platforms. Non-UTF-8 files are never touched.
- `import --layout flat`: join subdirectories with `__` instead of
  recreating the tree (handy for platforms that want one flat folder).
- `merge --strategy`: conflict handling for same-path different-content
  files. `keep-base` (default) and `keep-other` pick a side; `both` writes
  the base version as-is and the other version as `<path>.other`; `report`
  is a dry run that writes nothing (`0` = no conflicts, `10` = conflicts
  found). `merge` always checksums both archives first, and `--into` must
  be new or empty.
- `forget --path GLOB` (repeatable): remove matching files from an
  archive, with `--reason TEXT` recorded. Globs match like
  `export --exclude`. Needs exactly one of `--output OUT` or `--in-place`
  (atomic rewrite); `--dry-run` lists matches and writes nothing (and
  takes no output args). Every operation is appended to `FORGETLOG.txt`
  inside the archive (timestamp, removed paths, reason, sha256 of the
  input archive beforehand), the manifest gains a `forget-operations`
  count, and top-level `SOURCE.txt` provenance is never removed.

Exit codes: `0` ok, `1` verification/overwrite failure, `2` usage error.
`diff` follows the quietwatch convention instead: `0` identical,
`10` differences found, `1` verification failure, `2` usage error —
so it composes with watch/alert pipelines. `merge` uses `0` for a
completed merge (conflicts resolved per strategy), `10` for conflicts
found under `report`, `1`/`2` as usual. `forget` uses `0` for success
(including "nothing matched", which writes nothing), `1` for
verification failure or a refused operation (tampered archive, would
empty the archive), `2` for usage/read errors.

## Format

See [FORMAT.md](FORMAT.md) for the full `.memport` archive specification.

## Safety

- Archives are checksum-verified before import; tampering is detected.
- Path traversal (`..`, absolute paths) is rejected at parse time.
- Symlinks are never exported.
- Redaction is one-way and blunt — audit with `inspect` before sharing.
- `forget` verifies the archive before mutating it, refuses tampered
  input, never removes the top-level `SOURCE.txt` provenance record,
  refuses to empty the archive, and records every operation in
  `FORGETLOG.txt` (verify the chain with `inspect`). Forgetting is
  lossy — keep the original archive if you might want the files back.

## Limitations

- **Format is memport-native, but collection is cross-platform.** `collect`
  / `export --from` normalize Claude Code, Cursor, OpenCode, Codex, and
  Gemini layouts into one tree, so the archive *content* is portable.
  Unpacking still needs memport (or an independent reader of the
  documented FORMAT.md) — there is no writer for any platform's
  proprietary memory store, and `import` restores files as-is rather
  than translating them into the target platform's layout.
- **Redaction is blunt.** `--redact` matches whole lines against a regex;
  it is not a semantic scrubber, and non-UTF-8 files are never redacted
  (they pass through untouched). Always `inspect` before sharing.
- **Merging is checksum-level, not semantic.** `merge` reconciles files by
  content hash; it doesn't understand Markdown sections or memory
  semantics. For true cross-agent memory reconciliation, review conflicts
  with `merge --strategy report` first.
- **Integrity, not provenance.** A tampered archive is detected, but an
  honestly exported archive of junk memory is still junk — checksums
  can't tell you the source was worth keeping.

## Tests

```bash
python3 test_memport.py   # 56 tests: roundtrips, tamper, redact, excludes, safety, diff, merge, forget
```

## License

MIT — see [LICENSE](LICENSE).
