# memport archive format — version 1

A `.memport` file is a **gzip-compressed tar archive** with a fixed layout:

```
<name>.memport
├── MANIFEST.txt        # plain-text manifest (UTF-8), see below
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
(tool name + version), `file-count`. Readers MUST ignore unknown header
keys (forward compatibility).

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

## Design notes

- Manifest is human-readable on purpose: you can `tar xOzf x.memport
  MANIFEST.txt` and audit exactly what is inside before importing.
- No compression-level tricks, no encryption: if you need secrecy, put
  the `.memport` file inside an encrypted container yourself.
- Redaction (`--redact REGEX`) replaces whole matching *lines* with
  `[REDACTED]`, never touching non-UTF-8 payloads. It is a blunt privacy
  aid for sharing memory across platforms, not a guarantee — review
  `inspect` output before handing the archive to anyone.
