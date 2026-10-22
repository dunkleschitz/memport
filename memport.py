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
    memport.py inspect FILE.memport [--verify]
    memport.py import  FILE.memport --target DIR [--layout preserve|flat]
    memport.py diff   OLD.memport NEW.memport [--verify]
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
import time

MAGIC = "MEMPORT/1"
VERSION = "0.2.0"
MANIFEST_NAME = "MANIFEST.txt"
FILES_PREFIX = "files/"
REDACT_TOKEN = "[REDACTED]"


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


def build_manifest(name, source, entries):
    """entries: list of (relpath, sha256, size). Returns manifest text."""
    lines = [MAGIC]
    lines.append("name: %s" % name)
    lines.append("created: %s" % utc_iso_now())
    lines.append("source: %s" % os.path.abspath(source))
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


def cmd_export(args):
    source = args.source
    if not os.path.isdir(source):
        print("error: source is not a directory: %s" % source, file=sys.stderr)
        return 2
    entries = apply_excludes(iter_files(source), args.exclude)
    if not entries:
        print("error: nothing to export (no files after excludes)", file=sys.stderr)
        return 2
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

    manifest = build_manifest(name, source, file_records)
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
    """Open .memport, return (manifest_text, {rel: bytes}). Verifies
    tar structure only; checksums verified by caller when requested."""
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
    return header, entries, blobs


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
            header, entries, blobs = read_archive(path)
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


def cmd_inspect(args):
    try:
        header, entries, blobs = read_archive(args.file)
    except (ValueError, tarfile.TarError, OSError, EOFError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    print("memport archive: %s" % args.file)
    for key in ("name", "created", "source", "exporter", "file-count"):
        if key in header:
            print("  %-11s %s" % (key + ":", header[key]))
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
        header, entries, blobs = read_archive(args.file)
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
    sub = ap.add_subparsers(dest="command", required=True)

    ex = sub.add_parser("export", help="export a memory directory to .memport")
    ex.add_argument("--source", required=True, help="memory directory to export")
    ex.add_argument("--out", required=True, help="output .memport file")
    ex.add_argument("--name", default=None, help="archive name (default: out basename)")
    ex.add_argument("--exclude", action="append", default=[],
                    help="glob to exclude (repeatable)")
    ex.add_argument("--redact", action="append", default=[],
                    help="regex: redact whole lines matching it (repeatable)")
    ex.set_defaults(func=cmd_export)

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

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
