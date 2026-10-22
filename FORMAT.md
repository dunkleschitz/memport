# memport archive format — version 1

A `.memport` file is a **gzip-compressed tar archive** with a fixed layout:

```
<name>.memport
├── MANIFEST.txt        # plain-text manifest (UTF-8), see below
├── FORGETLOG.txt       # present only if forget was used: audit trail of removals
└── files/              # one member per exported file, relative paths preserved
    ├── MEMORY.md
    └── memory/
        └── 2026-10-21.md
```

## MANIFEST.txt

UTF-8 plain text. First line is the magic `MEMPORT/1` (identifies format
version). Then `key: value` header lines, then the literal line `files:`,
then one entry per file:

```
MEMPORT/1
name: snapshot-2026-10-21
created: 2026-10-21T20:52:44+0000
source: /home/user/memory
exporter: memport 0.1.0
file-count: 2
files:
  MEMORY.md sha256:4b9c… size:23
  memory/2026-10-21.md sha256:a1f2… size:32
```

Header keys defined by version 1: `name`, `created` (local ISO-8601 with
offset), `source` (absolute path the tree was exported from), `exporter`
(tool name + version), `file-count`. When the export came from
`export --from PLATFORM`, two more keys are present: `source-platform`
(the adapter name) and `source-root` (the platform root the files were
collected from). After any `forget` operation the key
`forget-operations` holds the running count of forget operations applied
to the archive. Readers MUST ignore unknown header keys (forward
compatibility).

Each file entry is: two spaces, the relative POSIX path (no leading `/`,
no `..` components — a reader MUST reject archives violating this),
`sha256:` + 64 lowercase hex digits of the stored payload bytes,
`size:` + decimal byte count.

Checksums cover the **stored** bytes — i.e. *after* any export-time
redaction. Redaction is one-way: the original bytes cannot be recovered
from the archive.

## Integrity and safety rules (normative for readers)

1. Reject the archive if the magic line is not exactly `MEMPORT/1`.
2. Reject file entries with absolute paths or `..` components.
3. Verify every payload's sha256 against the manifest before importing;
   refuse to import on any mismatch.
4. Never follow or export symlinks; skip them.
5. Never overwrite an existing file on import; fail instead.
6. Treat redaction as lossy: `inspect` reports `exporter` but cannot tell
   you what was redacted.
7. A writer implementing `forget` MUST verify all payload checksums
   before removing anything, MUST refuse to produce an archive with zero
   `files/` entries, and MUST NOT remove the top-level `SOURCE.txt`
   provenance member.

## FORGETLOG.txt (written by `forget`)

Present only in archives that have had at least one forget operation.
UTF-8 plain text. First line is the magic `memport-forget/1`, then one
block per operation, separated by a line containing exactly `---`:

```
memport-forget/1
---
at: 2026-10-22T17:32:30-0400
tool: memport 0.5.0
archive-sha256-before: fd0533b5…
removed: 1
reason: stale
files:
  memory/2026-09-01.md
```

Each block records when the operation ran, which tool ran it, the sha256
of the input archive *file* before the operation (so anyone who kept the
previous archive file can verify the chain), how many files were
removed, the operator-supplied reason (`-` when none was given), and the
removed paths. Blocks are append-only: a forget never rewrites history,
only appends.

`FORGETLOG.txt` is archive metadata, like `MANIFEST.txt` — it is not
listed in the manifest's `files:` section and its checksum is not
tracked there. The log is an audit trail, not a cryptographic proof:
intermediate manifests are not retained, so the chain is verifiable only
by holders of the previous archive files. `inspect` reports the number
of logged operations.

## Design notes

- Manifest is human-readable on purpose: you can `tar xOzf x.memport
  MANIFEST.txt` and audit exactly what is inside before importing.
- No compression-level tricks, no encryption: if you need secrecy, put
  the `.memport` file inside an encrypted container yourself.
- Redaction (`--redact REGEX`) replaces whole matching *lines* with
  `[REDACTED]`, never touching non-UTF-8 payloads. It is a blunt privacy
  aid for sharing memory across platforms, not a guarantee — review
  `inspect` output before handing the archive to anyone.
