#!/usr/bin/env python3
"""Tests for memport.py. Run: python3 test_memport.py"""

import os
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import memport


def make_tree(root, files):
    for rel, content in files.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(full, mode) as f:
            f.write(content)


def read_tree(root):
    out = {}
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            full = os.path.join(dirpath, n)
            with open(full, "rb") as f:
                out[os.path.relpath(full, root).replace(os.sep, "/")] = f.read()
    return out


class Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


SAMPLE = {
    "MEMORY.md": "# memory\n- likes tacos\n",
    "memory/2026-10-21.md": "did things\nsecret-token: abc123\n",
    "memory/people/friend.md": "friend bio\n",
    "notes.bin": b"\xff\xfe\x00binary-not-utf8",
}


class TestMemport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "src")
        os.makedirs(self.src)
        make_tree(self.src, SAMPLE)
        self.arc = os.path.join(self.tmp.name, "snap.memport")

    def tearDown(self):
        self.tmp.cleanup()

    def do_export(self, **kw):
        a = Args(source=self.src, out=self.arc, name="snap",
                 exclude=kw.get("exclude", []), redact=kw.get("redact", []))
        self.assertEqual(memport.cmd_export(a), 0)

    def test_roundtrip_preserve(self):
        self.do_export()
        dst = os.path.join(self.tmp.name, "dst")
        a = Args(file=self.arc, target=dst, layout="preserve")
        self.assertEqual(memport.cmd_import(a), 0)
        got = read_tree(dst)
        expected = {k: (v.encode() if isinstance(v, str) else v)
                    for k, v in SAMPLE.items()}
        self.assertEqual(got, expected)

    def test_roundtrip_flat(self):
        self.do_export()
        dst = os.path.join(self.tmp.name, "flat")
        a = Args(file=self.arc, target=dst, layout="flat")
        self.assertEqual(memport.cmd_import(a), 0)
        got = read_tree(dst)
        self.assertIn("memory__2026-10-21.md", got)
        self.assertIn("memory__people__friend.md", got)
        self.assertIn("MEMORY.md", got)

    def test_inspect_verify_ok(self):
        self.do_export()
        a = Args(file=self.arc, verify=True)
        self.assertEqual(memport.cmd_inspect(a), 0)

    def test_tamper_detected(self):
        self.do_export()
        # Rewrite one payload inside the tar with different content.
        with tarfile.open(self.arc, "r:gz") as tar:
            members = [(m, tar.extractfile(m).read() if m.isfile() else None)
                       for m in tar.getmembers()]
        with tarfile.open(self.arc, "w:gz") as tar:
            for m, data in members:
                if data is None:
                    tar.addfile(m)
                    continue
                if m.name.endswith("MEMORY.md"):
                    data = b"tampered\n"
                m.size = len(data)
                import io
                tar.addfile(m, io.BytesIO(data))
        a = Args(file=self.arc, verify=True)
        self.assertEqual(memport.cmd_inspect(a), 1)  # verify failed
        a2 = Args(file=self.arc, target=os.path.join(self.tmp.name, "x"),
                  layout="preserve")
        self.assertEqual(memport.cmd_import(a2), 1)  # import refused

    def test_redact(self):
        self.do_export(redact=[r"secret-token"])
        dst = os.path.join(self.tmp.name, "dst")
        a = Args(file=self.arc, target=dst, layout="preserve")
        self.assertEqual(memport.cmd_import(a), 0)
        content = read_tree(dst)["memory/2026-10-21.md"].decode()
        self.assertNotIn("abc123", content)
        self.assertIn(memport.REDACT_TOKEN, content)
        # untouched file keeps its line
        self.assertIn("likes tacos", read_tree(dst)["MEMORY.md"].decode())

    def test_redact_skips_binary(self):
        self.do_export(redact=[r".*"])  # matches everything textual
        dst = os.path.join(self.tmp.name, "dst")
        memport.cmd_import(Args(file=self.arc, target=dst, layout="preserve"))
        self.assertEqual(read_tree(dst)["notes.bin"], b"\xff\xfe\x00binary-not-utf8")

    def test_exclude(self):
        self.do_export(exclude=["*.bin", "memory/people/*"])
        header, entries, _blobs = memport.read_archive(self.arc)
        rels = [r for r, _d, _s in entries]
        self.assertNotIn("notes.bin", rels)
        self.assertNotIn("memory/people/friend.md", rels)
        self.assertIn("MEMORY.md", rels)

    def test_import_refuses_overwrite(self):
        self.do_export()
        dst = os.path.join(self.tmp.name, "dst")
        os.makedirs(dst)
        with open(os.path.join(dst, "MEMORY.md"), "w") as f:
            f.write("existing\n")
        a = Args(file=self.arc, target=dst, layout="preserve")
        self.assertEqual(memport.cmd_import(a), 1)
        with open(os.path.join(dst, "MEMORY.md")) as f:
            self.assertEqual(f.read(), "existing\n")  # untouched

    def test_symlinks_not_exported(self):
        os.symlink(os.path.join(self.src, "MEMORY.md"),
                   os.path.join(self.src, "link.md"))
        self.do_export()
        _header, entries, _blobs = memport.read_archive(self.arc)
        rels = [r for r, _d, _s in entries]
        self.assertNotIn("link.md", rels)

    def test_manifest_roundtrip_parse(self):
        entries = [("a/b.md", "0" * 64, 12)]
        text = memport.build_manifest("n", self.src, entries)
        header, parsed = memport.parse_manifest(text)
        self.assertEqual(header["name"], "n")
        self.assertEqual(parsed, entries)

    def test_manifest_rejects_bad_magic(self):
        with self.assertRaises(ValueError):
            memport.parse_manifest("GARBAGE\nfiles:\n")

    def test_manifest_rejects_traversal(self):
        bad = (memport.MAGIC + "\nname: x\ncreated: y\nsource: z\n"
               "exporter: e\nfile-count: 1\nfiles:\n"
               "  ../evil.md sha256:%s size:1\n" % ("0" * 64))
        with self.assertRaises(ValueError):
            memport.parse_manifest(bad)

    def test_export_empty_dir_fails(self):
        empty = os.path.join(self.tmp.name, "empty")
        os.makedirs(empty)
        a = Args(source=empty, out=self.arc, name="e", exclude=[], redact=[])
        self.assertEqual(memport.cmd_export(a), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
