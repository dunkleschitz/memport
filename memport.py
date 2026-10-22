#!/usr/bin/env python3
"""memport — portable agent memory export/import.

Packs an agent's memory files (markdown notes, MEMORY.md, etc.) into a
single documented plain-text archive (.memport = gzipped tar containing
MANIFEST.txt + files/), verifies integrity on read, and imports into a
target platform layout.

Stdlib only. Read FORMAT.md for the archive format specification.

Usage:
    memport.py export  --source DIR --out FILE.memport [--name NAME]
                       [--exclude GLOB ...] [--redact REGEX ...]
    memport.py export  --from PLATFORM --root DIR --out FILE.memport
                       (collect a platform's native layout, then export it)
    memport.py collect --platform PLATFORM --root DIR --out DIR
    memport.py inspect FILE.memport [--verify]
    memport.py import  FILE.memport --target DIR [--layout preserve|flat]
    memport.py diff   OLD.memport NEW.memport [--verify]
    memport.py merge  BASE.mport OTHER.mport --into DIR
                      [--strategy keep-base|keep-other|both|report]
    memport.py forget ARCHIVE.memport --path GLOB [--path ...]
                      [--reason TEXT] (--output OUT | --in-place) [--dry-run]

Platforms for collect / export --from: claude-code, cursor, opencode,
codex, gemini, generic. --root is the platform's root directory: a
project root for project-scoped files, or the platform's home directory
(e.g. ~/.claude) for user-scoped files.
"""

import argparse
import fnmatch
import hashlib
import io
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time

MAGIC = "MEMPORT/1"
VERSION = "0.5.0"
MANIFEST_NAME = "MANIFEST.txt"
FORGETLOG_NAME = "FORGETLOG.txt"
FORGET_MAGIC = "memport-forget/1"
FILES_PREFIX = "files/"
REDACT_TOKEN = "[REDACTED]"

# Top-level archive members that are provenance/metadata, never memory
# content: forget refuses to remove them (they are skipped with a note).
PROTECTED_MEMBERS = {"SOURCE.txt"}

# Platform-native memory layouts, verified against each platform's
# documented file locations (Oct 2026). Globs are matched root-relative
# against --root with fnmatch (so '*' crosses directories). Each adapter
# collects instruction/memory markdown only — never transcripts, logs,
# settings, or credentials. The "excludes" note documents what was
# deliberately left out and why.
ADAPTERS = {
    "claude-code": {
        "description": "Claude Code instruction and memory files",
        "globs": [
            "CLAUDE.md", "**/CLAUDE.md",      # project memory (also nested)
            "CLAUDE.local.md",                # personal project memory
            ".claude/CLAUDE.md",              # alt project location
            ".claude/rules/*.md",             # project rules
            "rules/*.md",                     # --root=~/.claude: personal rules
            "memory/*.md",                    # --root=~/.claude/projects/<slug>: auto memory
        ],
        "excludes": ("session transcripts (projects/*/*.jsonl), history.jsonl, "
                     "settings.json, auth tokens — never collected"),
    },
    "cursor": {
        "description": "Cursor project rules and instruction files",
        "globs": [
            ".cursor/rules/*.mdc",            # project rules (plain .md ignored by Cursor)
            ".cursorrules",                   # legacy single-file rules
            "AGENTS.md", "**/AGENTS.md",      # Cursor reads AGENTS.md
        ],
        "excludes": ("global user rules (location varies by Cursor version; "
                     "not collected), MCP configs, anything outside the root"),
    },
    "opencode": {
        "description": "OpenCode instruction files and markdown config",
        "globs": [
            "AGENTS.md", "**/AGENTS.md",      # project rules (also CLAUDE.md fallback source)
            ".opencode/*.md", ".opencode/**/*.md",  # project agents/commands/memory markdown
        ],
        "excludes": "opencode.json and other non-markdown config — memory markdown only",
    },
    "codex": {
        "description": "Codex CLI instruction files",
        "globs": [
            "AGENTS.md", "**/AGENTS.md",
            "AGENTS.override.md",             # temporary context override
        ],
        "excludes": ("~/.codex/auth.json (credentials), sessions/, log/, "
                     "history.jsonl — never collected"),
    },
    "gemini": {
        "description": "Gemini CLI memory files",
        "globs": [
            "GEMINI.md", "**/GEMINI.md",      # project memory (root + subdirs)
            ".gemini/GEMINI.md",              # project config-dir memory
        ],
        "excludes": "chats/ (saved sessions), settings.json, credentials — never collected",
    },
    "generic": {
        "description": "Plain markdown memory trees (MEMORY.md + dated notes)",
        "globs": [
            "MEMORY.md", "**/MEMORY.md",
            "memory/*.md", "memory/**/*.md",  # dated notes, people/groups pages
            "AGENTS.md",
        ],
        "excludes": "nothing by pattern — markdown only by glob design",
    },
}


def utc_iso_now():
    # Local time with offset, second precision: human-readable and sortable.
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def iter_files(source):
    """Yield (relative posix path, absolute path) for every regular file
    under source, sorted. Skips symlinks (never follow/export them)."""
    out = []
    for root, dirs, names in os.walk(source, followlinks=False):
        dirs[:] = sorted(d for d in dirs
                         if not os.path.islink(os.path.join(root, d)))
        for name in sorted(names):
            full = os.path.join(root, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, source).replace(os.sep, "/")
            out.append((rel, full))
    return out


def apply_excludes(entries, patterns):
    if not patterns:
        return entries
    kept = []
    for rel, full in entries:
        if any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(os.path.basename(rel), p)
               for p in patterns):
            continue
        kept.append((rel, full))
    return kept


def apply_redactions(data, regexes):
    """Redact whole lines containing a match, preserving line structure."""
    if not regexes:
        return data, 0
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data, 0  # binary files: do not touch
    compiled = [re.compile(r) for r in regexes]
    count = 0
    lines = text.splitlines(keepends=True)
    out_lines = []
    for line in lines:
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        if any(c.search(body) for c in compiled):
            out_lines.append(REDACT_TOKEN + ending)
            count += 1
        else:
            out_lines.append(line)
    return "".join(out_lines).encode("utf-8"), count


def build_manifest(name, source, entries, platform=None, platform_root=None):
    """entries: list of (relpath, sha256, size). Returns manifest text."""
    lines = [MAGIC]
    lines.append("name: %s" % name)
    lines.append("created: %s" % utc_iso_now())
    lines.append("source: %s" % os.path.abspath(source))
    if platform:
        lines.append("source-platform: %s" % platform)
        lines.append("source-root: %s" % platform_root)
    lines.append("exporter: memport %s" % VERSION)
    lines.append("file-count: %d" % len(entries))
    lines.append("files:")
    for rel, digest, size in entries:
        lines.append("  %s sha256:%s size:%d" % (rel, digest, size))
    return "\n".join(lines) + "\n"


def parse_manifest(text):
    """Parse MANIFEST.txt. Returns (header dict, [(rel, sha256, size)]).
    Raises ValueError on malformed input."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != MAGIC:
        raise ValueError("not a memport manifest (bad magic line)")
    header = {}
    entries = []
    i = 1
    while i < len(lines):
        line = lines[i]
        if line.strip() == "files:":
            i += 1
            break
        if ":" in line:
            key, _, val = line.partition(":")
            header[key.strip()] = val.strip()
        i += 1
    entry_re = re.compile(r"^(\S+) sha256:([0-9a-f]{64}) size:(\d+)$")
    for j in range(i, len(lines)):
        line = lines[j].rstrip("\n")
        if not line.strip():
            continue
        m = entry_re.match(line.strip())
        if not m:
            raise ValueError("malformed file entry: %r" % line)
        entries.append((m.group(1), m.group(2), int(m.group(3))))
        # Reject path traversal at parse time, before any extraction.
        if m.group(1).startswith("/") or ".." in m.group(1).split("/"):
            raise ValueError("unsafe path in manifest: %r" % m.group(1))
    return header, entries


def collect_tree(platform, root, out):
    """Copy the adapter's matched files from root into out/<platform>/,
    preserving relative paths. Symlinks are never followed (iter_files
    already skips them). Returns the number of files collected.
    Raises ValueError on unknown platform."""
    if platform not in ADAPTERS:
        raise ValueError("unknown platform: %r (choose from: %s)"
                         % (platform, ", ".join(sorted(ADAPTERS))))
    globs = ADAPTERS[platform]["globs"]
    dest_base = os.path.join(out, platform)
    count = 0
    for rel, full in iter_files(root):
        if not any(fnmatch.fnmatch(rel, g) for g in globs):
            continue
        dest = safe_join(dest_base, rel)
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "rb") as f:
            data = f.read()
        with open(dest, "wb") as f:
            f.write(data)
        count += 1
    return count


def write_source_record(out, platform, root, count):
    """Write SOURCE.txt provenance record at the top of a collect tree."""
    record = [
        "memport-collect/1",
        "platform: %s" % platform,
        "root: %s" % os.path.abspath(root),
        "collected: %s" % utc_iso_now(),
        "collector: memport %s" % VERSION,
        "files: %d" % count,
        "globs: %s" % ", ".join(ADAPTERS[platform]["globs"]),
        "excludes: %s" % ADAPTERS[platform]["excludes"],
    ]
    with open(os.path.join(out, "SOURCE.txt"), "w") as f:
        f.write("\n".join(record) + "\n")


def cmd_collect(args):
    platform = args.platform
    if platform not in ADAPTERS:
        print("error: unknown platform: %s (choose from: %s)"
              % (platform, ", ".join(sorted(ADAPTERS))), file=sys.stderr)
        return 2
    root = args.root
    if not os.path.isdir(root):
        print("error: root is not a directory: %s" % root, file=sys.stderr)
        return 2
    out = args.out
    if os.path.exists(out):
        if not os.path.isdir(out) or os.listdir(out):
            print("error: refusing to collect into non-empty target: %s"
                  % out, file=sys.stderr)
            return 1
    else:
        os.makedirs(out, exist_ok=True)
    count = collect_tree(platform, root, out)
    write_source_record(out, platform, root, count)
    print("collected %d file(s) for platform '%s' from %s -> %s"
          % (count, platform, root, out))
    if count == 0:
        print("warning: no %s memory files found under %s"
              % (platform, root), file=sys.stderr)
    return 0


def cmd_export(args):
    from_platform = getattr(args, "from_platform", None)
    source = args.source
    if from_platform and source:
        print("error: --source and --from are mutually exclusive",
              file=sys.stderr)
        return 2
    if not from_platform and not source:
        print("error: need --source DIR or --from PLATFORM", file=sys.stderr)
        return 2
    platform_info = (None, None)
    tmp_collect = None
    try:
        if from_platform:
            if from_platform not in ADAPTERS:
                print("error: unknown platform: %s (choose from: %s)"
                      % (from_platform, ", ".join(sorted(ADAPTERS))),
                      file=sys.stderr)
                return 2
            root = args.plat_root or os.getcwd()
            if not os.path.isdir(root):
                print("error: root is not a directory: %s" % root,
                      file=sys.stderr)
                return 2
            tmp_collect = tempfile.mkdtemp(prefix="memport-collect-")
            count = collect_tree(from_platform, root, tmp_collect)
            if count == 0:
                print("error: no %s memory files found under %s"
                      % (from_platform, root), file=sys.stderr)
                return 2
            write_source_record(tmp_collect, from_platform, root, count)
            source = tmp_collect
            platform_info = (from_platform, os.path.abspath(root))
        if not os.path.isdir(source):
            print("error: source is not a directory: %s" % source,
                  file=sys.stderr)
            return 2
        entries = apply_excludes(iter_files(source), args.exclude)
        if not entries:
            print("error: nothing to export (no files after excludes)",
                  file=sys.stderr)
            return 2
        return export_tree(args, source, entries, platform_info)
    finally:
        if tmp_collect and os.path.isdir(tmp_collect):
            shutil.rmtree(tmp_collect)


def export_tree(args, source, entries, platform_info):
    """Pack the tree at source into args.out. entries is the
    [(rel, full)] file list; platform_info is (platform, root) or
    (None, None)."""
    name = args.name or os.path.basename(args.out.rstrip("/"))
    if name.endswith(".memport"):
        name = name[:-len(".memport")]

    file_records = []   # (rel, digest, size)
    payloads = {}       # rel -> bytes (post-redaction)
    redacted_lines = 0
    for rel, full in entries:
        with open(full, "rb") as f:
            data = f.read()
        data, n = apply_redactions(data, args.redact)
        redacted_lines += n
        payloads[rel] = data
        file_records.append((rel, sha256_bytes(data), len(data)))

    platform, platform_root = platform_info
    manifest = build_manifest(name, source, file_records,
                              platform=platform, platform_root=platform_root)
    tmp_out = args.out + ".tmp"
    try:
        with tarfile.open(tmp_out, "w:gz") as tar:
            mi = tarfile.TarInfo(MANIFEST_NAME)
            mbytes = manifest.encode("utf-8")
            mi.size = len(mbytes)
            mi.mtime = int(time.time())
            tar.addfile(mi, io.BytesIO(mbytes))
            for rel, _digest, _size in file_records:
                data = payloads[rel]
                ti = tarfile.TarInfo(FILES_PREFIX + rel)
                ti.size = len(data)
                ti.mtime = int(time.time())
                ti.mode = 0o644
                tar.addfile(ti, io.BytesIO(data))
        os.replace(tmp_out, args.out)
    finally:
        if os.path.exists(tmp_out):
            os.remove(tmp_out)
    print("exported %d file(s) -> %s (%d redacted line(s))"
          % (len(file_records), args.out, redacted_lines))
    return 0


def read_archive(path):
    """Open .memport, return (header dict, entries, {rel: bytes},
    forget-log text or None). Verifies tar structure only; checksums
    verified by caller when requested."""
    if not tarfile.is_tarfile(path):
        raise ValueError("not a readable archive: %s" % path)
    with tarfile.open(path, "r:gz") as tar:
        members = tar.getmembers()
        names = [m.name for m in members]
        if MANIFEST_NAME not in names:
            raise ValueError("archive missing %s" % MANIFEST_NAME)
        manifest_text = tar.extractfile(MANIFEST_NAME).read().decode("utf-8")
        header, entries = parse_manifest(manifest_text)
        blobs = {}
        for rel, _digest, _size in entries:
            arcname = FILES_PREFIX + rel
            f = tar.extractfile(arcname)
            if f is None:
                raise ValueError("archive missing payload: %s" % arcname)
            blobs[rel] = f.read()
        forgetlog = None
        if FORGETLOG_NAME in names:
            forgetlog = tar.extractfile(FORGETLOG_NAME).read().decode("utf-8")
    return header, entries, blobs, forgetlog


def verify_blobs(entries, blobs):
    problems = []
    for rel, digest, size in entries:
        data = blobs.get(rel)
        if data is None:
            problems.append("%s: missing from archive" % rel)
            continue
        if sha256_bytes(data) != digest:
            problems.append("%s: checksum mismatch" % rel)
        if len(data) != size:
            problems.append("%s: size mismatch (%d != %d)"
                            % (rel, len(data), size))
    return problems


def cmd_diff(args):
    """Compare two .memport archives by manifest (checksums, not tar bytes),
    so a re-export with only timestamp or ordering changes diffs clean.

    Exit codes mirror quietwatch: 0 = identical, 10 = differences found,
    1 = an archive failed integrity verification, 2 = usage/read error."""
    archives = []
    for path in (args.old, args.new):
        try:
            header, entries, blobs, _forgetlog = read_archive(path)
        except (ValueError, tarfile.TarError, OSError, EOFError) as e:
            print("error: %s: %s" % (path, e), file=sys.stderr)
            return 2
        archives.append((path, header, entries, blobs))
    if args.verify:
        for path, _header, entries, blobs in archives:
            problems = verify_blobs(entries, blobs)
            if problems:
                print("error: %s failed integrity check:" % path, file=sys.stderr)
                for p in problems:
                    print("  - %s" % p, file=sys.stderr)
                return 1
    old_entries = {rel: (digest, size) for rel, digest, size in archives[0][2]}
    new_entries = {rel: (digest, size) for rel, digest, size in archives[1][2]}
    added = sorted(set(new_entries) - set(old_entries))
    removed = sorted(set(old_entries) - set(new_entries))
    modified = sorted(rel for rel in set(old_entries) & set(new_entries)
                      if old_entries[rel] != new_entries[rel])
    for rel in added:
        _digest, size = new_entries[rel]
        print("ADDED    %s (%d bytes)" % (rel, size))
    for rel in removed:
        _digest, size = old_entries[rel]
        print("REMOVED  %s (was %d bytes)" % (rel, size))
    for rel in modified:
        old_size = old_entries[rel][1]
        new_size = new_entries[rel][1]
        print("MODIFIED %s (%d -> %d bytes, sha %s -> %s)"
              % (rel, old_size, new_size,
                 old_entries[rel][0][:12], new_entries[rel][0][:12]))
    n_same = (len(old_entries) - len(removed) - len(modified))
    print("summary: %d added, %d removed, %d modified, %d unchanged"
          % (len(added), len(removed), len(modified), n_same))
    return 10 if (added or removed or modified) else 0


def cmd_merge(args):
    """Merge two .memport archives into a fresh directory tree.

    Both archives are integrity-verified first (merge always writes, so
    unverified data is refused). Per relative path:
      in one archive only -> copied from that archive
      identical checksum  -> copied once
      different checksum  -> conflict, resolved by --strategy:
        keep-base (default) write the BASE version
        keep-other          write the OTHER version
        both                write base as path, other as path + ".other"
        report              dry run: write nothing, list what would happen
    --into must not exist or must be empty (never merge over data).

    Exit codes mirror quietwatch: 0 = merged cleanly (or no conflicts
    under report), 10 = conflicts found under report, 1 = integrity or
    target failure, 2 = usage/read error."""
    archives = []
    for path in (args.base, args.other):
        try:
            header, entries, blobs, _forgetlog = read_archive(path)
        except (ValueError, tarfile.TarError, OSError, EOFError) as e:
            print("error: %s: %s" % (path, e), file=sys.stderr)
            return 2
        problems = verify_blobs(entries, blobs)
        if problems:
            print("error: %s failed integrity check:" % path, file=sys.stderr)
            for p in problems:
                print("  - %s" % p, file=sys.stderr)
            return 1
        archives.append((path, {rel: (digest, size) for rel, digest, size
                                in entries}, blobs))
    _base_path, base_entries, base_blobs = archives[0]
    _other_path, other_entries, other_blobs = archives[1]

    added_base = sorted(set(base_entries) - set(other_entries))
    added_other = sorted(set(other_entries) - set(base_entries))
    common = sorted(set(base_entries) & set(other_entries))
    identical = [rel for rel in common
                 if base_entries[rel] == other_entries[rel]]
    conflicts = [rel for rel in common
                 if base_entries[rel] != other_entries[rel]]

    strategy = args.strategy
    if strategy == "report":
        for rel in added_base:
            print("BASE-ONLY  %s" % rel)
        for rel in added_other:
            print("OTHER-ONLY %s" % rel)
        for rel in identical:
            print("IDENTICAL  %s" % rel)
        for rel in conflicts:
            print("CONFLICT   %s (base %s / other %s)"
                  % (rel, base_entries[rel][0][:12], other_entries[rel][0][:12]))
        print("summary: %d base-only, %d other-only, %d identical, %d conflict(s)"
              % (len(added_base), len(added_other), len(identical),
                 len(conflicts)))
        return 10 if conflicts else 0

    target = args.into
    if os.path.exists(target):
        if not os.path.isdir(target) or os.listdir(target):
            print("error: refusing to merge into non-empty target: %s"
                  % target, file=sys.stderr)
            return 1
    else:
        os.makedirs(target, exist_ok=True)

    written = {}   # rel -> source label, guards ".other" collisions
    def write_file(rel, data, label):
        dest = safe_join(target, rel)
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        written[rel] = label

    for rel in added_base:
        write_file(rel, base_blobs[rel], "base")
        print("COPIED     %s (from base)" % rel)
    for rel in added_other:
        write_file(rel, other_blobs[rel], "other")
        print("COPIED     %s (from other)" % rel)
    for rel in identical:
        write_file(rel, base_blobs[rel], "base")
        print("IDENTICAL  %s" % rel)
    for rel in conflicts:
        if strategy == "keep-other":
            write_file(rel, other_blobs[rel], "other")
            print("CONFLICT   %s -> kept other" % rel)
        elif strategy == "both":
            write_file(rel, base_blobs[rel], "base")
            alt = rel + ".other"
            n = 1
            while alt in written:
                n += 1
                alt = "%s.other%d" % (rel, n)
            write_file(alt, other_blobs[rel], "other")
            print("CONFLICT   %s -> kept base, other as %s" % (rel, alt))
        else:  # keep-base
            write_file(rel, base_blobs[rel], "base")
            print("CONFLICT   %s -> kept base" % rel)
    print("summary: %d copied, %d identical, %d conflict(s, strategy=%s) -> %s"
          % (len(added_base) + len(added_other), len(identical),
             len(conflicts), strategy, target))
    return 0


def build_forget_manifest(header, kept, total_forgets):
    """Rebuild MANIFEST.txt after a forget: keep the original header key
    order, refresh file-count, record the running forget-operations
    count, and list only the kept entries."""
    lines = [MAGIC]
    for key, val in header.items():
        if key in ("file-count", "forget-operations"):
            continue  # rewritten below
        lines.append("%s: %s" % (key, val))
    lines.append("file-count: %d" % len(kept))
    lines.append("forget-operations: %d" % total_forgets)
    lines.append("files:")
    for rel, digest, size in kept:
        lines.append("  %s sha256:%s size:%d" % (rel, digest, size))
    return "\n".join(lines) + "\n"


def append_forget_log(existing, archive_sha_before, removed_rels, reason):
    """Append one operation block to FORGETLOG.txt. Each block records the
    sha256 of the input archive file *before* the operation, so anyone who
    kept the previous archive file can verify the chain. This is an audit
    trail, not a cryptographic proof — intermediate manifests are not
    retained."""
    block = [
        "---",
        "at: %s" % utc_iso_now(),
        "tool: memport %s" % VERSION,
        "archive-sha256-before: %s" % archive_sha_before,
        "removed: %d" % len(removed_rels),
        "reason: %s" % (reason if reason else "-"),
        "files:",
    ]
    block.extend("  %s" % rel for rel in sorted(removed_rels))
    if existing:
        return existing.rstrip("\n") + "\n" + "\n".join(block) + "\n"
    return FORGET_MAGIC + "\n" + "\n".join(block) + "\n"


def write_archive_file(out_path, manifest_text, forgetlog_text, kept, blobs):
    """Write a fresh .memport atomically (tmp + rename)."""
    tmp_out = out_path + ".tmp"
    try:
        with tarfile.open(tmp_out, "w:gz") as tar:
            for arcname, text in ((MANIFEST_NAME, manifest_text),
                                  (FORGETLOG_NAME, forgetlog_text)):
                mbytes = text.encode("utf-8")
                ti = tarfile.TarInfo(arcname)
                ti.size = len(mbytes)
                ti.mtime = int(time.time())
                ti.mode = 0o644
                tar.addfile(ti, io.BytesIO(mbytes))
            for rel, _digest, _size in kept:
                data = blobs[rel]
                ti = tarfile.TarInfo(FILES_PREFIX + rel)
                ti.size = len(data)
                ti.mtime = int(time.time())
                ti.mode = 0o644
                tar.addfile(ti, io.BytesIO(data))
        os.replace(tmp_out, out_path)
    finally:
        if os.path.exists(tmp_out):
            os.remove(tmp_out)


def cmd_forget(args):
    """Remove files matching --path globs from a .memport archive.

    The archive is integrity-verified first; forget never mutates
    unverified data. Removed entries drop out of the manifest, the
    manifest gains a forget-operations count, and every operation is
    appended to FORGETLOG.txt (who/what/when + the sha256 of the input
    archive beforehand). Protected provenance members (top-level
    SOURCE.txt) are never removed. Refuses to empty the archive.

    Exit codes: 0 ok (including "nothing matched"), 1 verification
    failure or operation refused, 2 usage/read error."""
    if args.dry_run and (args.output or args.in_place):
        print("error: --dry-run writes nothing; drop --output/--in-place",
              file=sys.stderr)
        return 2
    if not args.dry_run and bool(args.output) == bool(args.in_place):
        print("error: need exactly one of --output OUT or --in-place",
              file=sys.stderr)
        return 2
    if not args.path:
        print("error: need at least one --path GLOB", file=sys.stderr)
        return 2
    try:
        header, entries, blobs, forgetlog = read_archive(args.file)
    except (ValueError, tarfile.TarError, OSError, EOFError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    problems = verify_blobs(entries, blobs)
    if problems:
        print("error: archive failed integrity check:", file=sys.stderr)
        for p in problems:
            print("  - %s" % p, file=sys.stderr)
        return 1

    matched = []
    protected_skipped = 0
    for rel, digest, size in entries:
        hits = any(fnmatch.fnmatch(rel, p)
                   or fnmatch.fnmatch(os.path.basename(rel), p)
                   for p in args.path)
        if not hits:
            continue
        if rel in PROTECTED_MEMBERS:
            protected_skipped += 1
            continue
        matched.append((rel, digest, size))

    if args.dry_run:
        for rel, _digest, _size in matched:
            print("WOULD-REMOVE %s" % rel)
        print("dry run: %d file(s) would be removed%s; archive unchanged"
              % (len(matched),
                 " (%d protected skipped)" % protected_skipped
                 if protected_skipped else ""))
        return 0

    if not matched:
        msg = "0 file(s) matched; archive unchanged"
        if protected_skipped:
            msg += " (%d protected provenance file(s) skipped)" % protected_skipped
        print(msg)
        return 0

    matched_rels = {rel for rel, _d, _s in matched}
    kept = [e for e in entries if e[0] not in matched_rels]
    if not kept:
        print("error: forget would empty the archive (all %d file(s) "
              "matched); refusing" % len(entries), file=sys.stderr)
        return 1

    with open(args.file, "rb") as f:
        archive_sha_before = sha256_bytes(f.read())
    try:
        total_forgets = int(header.get("forget-operations", "0")) + 1
    except ValueError:
        total_forgets = 1
    manifest_text = build_forget_manifest(header, kept, total_forgets)
    forgetlog_text = append_forget_log(
        forgetlog, archive_sha_before, matched_rels, args.reason)

    out = args.output if args.output else args.file
    write_archive_file(out, manifest_text, forgetlog_text, kept, blobs)
    print("forgot %d file(s)%s -> %s"
          % (len(matched),
             " (%d protected skipped)" % protected_skipped
             if protected_skipped else "",
             out))
    return 0


def cmd_inspect(args):
    try:
        header, entries, blobs, forgetlog = read_archive(args.file)
    except (ValueError, tarfile.TarError, OSError, EOFError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    print("memport archive: %s" % args.file)
    for key in ("name", "created", "source", "source-platform",
                "source-root", "exporter", "file-count",
                "forget-operations"):
        if key in header:
            print("  %-15s %s" % (key + ":", header[key]))
    if forgetlog:
        ops = sum(1 for line in forgetlog.splitlines()
                  if line.strip() == "---")
        print("  forget-log:     %d operation(s) in %s"
              % (ops, FORGETLOG_NAME))
    print("  files:")
    total = 0
    for rel, _digest, size in entries:
        print("    %s (%d bytes)" % (rel, size))
        total += size
    print("  total: %d file(s), %d bytes" % (len(entries), total))
    if args.verify:
        problems = verify_blobs(entries, blobs)
        if problems:
            print("VERIFY FAILED:")
            for p in problems:
                print("  - %s" % p)
            return 1
        print("verify: OK — all %d checksum(s) match" % len(entries))
    return 0


def safe_join(base, rel):
    """Join base + rel, refusing path traversal."""
    if rel.startswith("/") or ".." in rel.split("/"):
        raise ValueError("unsafe path: %r" % rel)
    return os.path.join(base, *rel.split("/"))


def cmd_import(args):
    try:
        header, entries, blobs, _forgetlog = read_archive(args.file)
    except (ValueError, tarfile.TarError, OSError, EOFError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    problems = verify_blobs(entries, blobs)
    if problems:
        print("error: archive failed integrity check:", file=sys.stderr)
        for p in problems:
            print("  - %s" % p, file=sys.stderr)
        return 1
    target = args.target
    os.makedirs(target, exist_ok=True)
    written = 0
    for rel, _digest, _size in entries:
        if args.layout == "flat":
            dest_rel = rel.replace("/", "__")
            dest = os.path.join(target, dest_rel)
        else:  # preserve
            dest = safe_join(target, rel)
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if os.path.exists(dest):
            print("error: refusing to overwrite existing file: %s" % dest,
                  file=sys.stderr)
            return 1
        with open(dest, "wb") as f:
            f.write(blobs[rel])
        written += 1
    print("imported %d file(s) from '%s' -> %s (layout: %s)"
          % (written, header.get("name", "?"), target, args.layout))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="memport",
        description="Portable agent memory export/import (see FORMAT.md).")
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = ap.add_subparsers(dest="command", required=True)

    ex = sub.add_parser("export", help="export a memory directory to .memport")
    ex.add_argument("--source", default=None,
                    help="memory directory to export (or use --from)")
    ex.add_argument("--from", dest="from_platform", default=None,
                    choices=sorted(ADAPTERS), metavar="PLATFORM",
                    help="collect from a platform's native layout, then export "
                         "(choices: %s)" % ", ".join(sorted(ADAPTERS)))
    ex.add_argument("--root", dest="plat_root", default=None,
                    help="platform root for --from (default: current directory)")
    ex.add_argument("--out", required=True, help="output .memport file")
    ex.add_argument("--name", default=None, help="archive name (default: out basename)")
    ex.add_argument("--exclude", action="append", default=[],
                    help="glob to exclude (repeatable)")
    ex.add_argument("--redact", action="append", default=[],
                    help="regex: redact whole lines matching it (repeatable)")
    ex.set_defaults(func=cmd_export)

    co = sub.add_parser("collect",
                        help="collect a platform's native memory files into a tree")
    co.add_argument("--platform", required=True, choices=sorted(ADAPTERS),
                    metavar="PLATFORM",
                    help="platform layout to collect (choices: %s)"
                         % ", ".join(sorted(ADAPTERS)))
    co.add_argument("--root", required=True,
                    help="platform root: a project root, or the platform's "
                         "home dir (e.g. ~/.claude) for user-scoped files")
    co.add_argument("--out", required=True,
                    help="output directory (must be new or empty)")
    co.set_defaults(func=cmd_collect)

    ins = sub.add_parser("inspect", help="show archive contents")
    ins.add_argument("file", help=".memport file")
    ins.add_argument("--verify", action="store_true",
                     help="verify sha256 checksums of all payloads")
    ins.set_defaults(func=cmd_inspect)

    imp = sub.add_parser("import", help="import a .memport archive")
    imp.add_argument("file", help=".memport file")
    imp.add_argument("--target", required=True, help="directory to import into")
    imp.add_argument("--layout", choices=["preserve", "flat"], default="preserve",
                     help="preserve: keep tree; flat: join dirs with __")
    imp.set_defaults(func=cmd_import)

    di = sub.add_parser("diff", help="compare two .memport archives by content")
    di.add_argument("old", help="older .memport file")
    di.add_argument("new", help="newer .memport file")
    di.add_argument("--verify", action="store_true",
                    help="verify sha256 checksums before comparing")
    di.set_defaults(func=cmd_diff)

    mg = sub.add_parser("merge", help="merge two .memport archives into a directory")
    mg.add_argument("base", help="base .memport file")
    mg.add_argument("other", help="other .memport file")
    mg.add_argument("--into", required=True, help="target directory (must be new or empty)")
    mg.add_argument("--strategy", choices=["keep-base", "keep-other", "both", "report"],
                    default="keep-base",
                    help="conflict resolution (default: keep-base)")
    mg.set_defaults(func=cmd_merge)

    fg = sub.add_parser("forget",
                        help="remove files from a .memport archive (audited)")
    fg.add_argument("file", help=".memport file to forget from")
    fg.add_argument("--path", action="append", default=[], metavar="GLOB",
                    help="archive-relative glob to remove (repeatable; "
                         "matches like export --exclude)")
    fg.add_argument("--reason", default=None,
                    help="reason recorded in FORGETLOG.txt")
    fg.add_argument("--output", default=None,
                    help="write the pruned archive here")
    fg.add_argument("--in-place", action="store_true",
                    help="rewrite the input archive (atomic)")
    fg.add_argument("--dry-run", action="store_true",
                    help="list what would be removed, write nothing")
    fg.set_defaults(func=cmd_forget)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
