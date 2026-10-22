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
        header, entries, _blobs, _fl = memport.read_archive(self.arc)
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
        _header, entries, _blobs, _fl = memport.read_archive(self.arc)
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


class TestDiff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "src")
        os.makedirs(self.src)
        make_tree(self.src, SAMPLE)
        self.old = os.path.join(self.tmp.name, "old.memport")
        self.new = os.path.join(self.tmp.name, "new.memport")

    def tearDown(self):
        self.tmp.cleanup()

    def do_export(self, src, out):
        a = Args(source=src, out=out, name="n", exclude=[], redact=[])
        self.assertEqual(memport.cmd_export(a), 0)

    def do_diff(self, **kw):
        a = Args(old=kw.get("old", self.old), new=kw.get("new", self.new),
                 verify=kw.get("verify", False))
        return memport.cmd_diff(a)

    def test_identical_exports_exit_zero(self):
        self.do_export(self.src, self.old)
        self.do_export(self.src, self.new)
        self.assertEqual(self.do_diff(), 0)

    def test_identical_exports_silent_on_reexport(self):
        # Re-export is byte-different (timestamps) but content-identical.
        self.do_export(self.src, self.old)
        make_tree(self.src, {})  # no-op: touch nothing
        self.do_export(self.src, self.new)
        self.assertEqual(self.do_diff(), 0)

    def test_modified_file(self):
        self.do_export(self.src, self.old)
        make_tree(self.src, {"MEMORY.md": "# memory\n- likes tacos\n- new line\n"})
        self.do_export(self.src, self.new)
        self.assertEqual(self.do_diff(), 10)

    def test_added_and_removed(self):
        self.do_export(self.src, self.old)
        os.remove(os.path.join(self.src, "notes.bin"))
        make_tree(self.src, {"extra.md": "brand new\n"})
        self.do_export(self.src, self.new)
        self.assertEqual(self.do_diff(), 10)

    def test_empty_tree_change_still_detected(self):
        # Same file count, one file swapped out: added+removed, not modified.
        self.do_export(self.src, self.old)
        os.remove(os.path.join(self.src, "notes.bin"))
        make_tree(self.src, {"notes.bin": "not binary anymore\n"})
        self.do_export(self.src, self.new)
        self.assertEqual(self.do_diff(), 10)

    def test_diff_with_verify_ok(self):
        self.do_export(self.src, self.old)
        self.do_export(self.src, self.new)
        self.assertEqual(self.do_diff(verify=True), 0)

    def test_diff_unreadable_archive_exit_two(self):
        bogus = os.path.join(self.tmp.name, "bogus.memport")
        with open(bogus, "w") as f:
            f.write("not a tar file at all")
        self.do_export(self.src, self.new)
        self.assertEqual(self.do_diff(old=bogus), 2)

    def test_diff_missing_archive_exit_two(self):
        self.do_export(self.src, self.new)
        self.assertEqual(self.do_diff(old=os.path.join(self.tmp.name, "nope.memport")), 2)


class TestMerge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_src = os.path.join(self.tmp.name, "base")
        self.other_src = os.path.join(self.tmp.name, "other")
        os.makedirs(self.base_src)
        os.makedirs(self.other_src)
        # shared identical file, base-only, other-only, one conflict
        make_tree(self.base_src, {
            "shared.md": "same content\n",
            "base-only.md": "only in base\n",
            "conflict.md": "base version\n",
        })
        make_tree(self.other_src, {
            "shared.md": "same content\n",
            "other-only.md": "only in other\n",
            "conflict.md": "other version\n",
        })
        self.base = os.path.join(self.tmp.name, "base.memport")
        self.other = os.path.join(self.tmp.name, "other.memport")
        for src, out in ((self.base_src, self.base),
                         (self.other_src, self.other)):
            a = Args(source=src, out=out, name="n", exclude=[], redact=[])
            self.assertEqual(memport.cmd_export(a), 0)

    def tearDown(self):
        self.tmp.cleanup()

    def do_merge(self, **kw):
        into = kw.get("into", os.path.join(self.tmp.name, "merged"))
        a = Args(base=kw.get("base", self.base),
                 other=kw.get("other", self.other),
                 into=into, strategy=kw.get("strategy", "keep-base"))
        return memport.cmd_merge(a), into

    def test_merge_keep_base(self):
        rc, into = self.do_merge()
        self.assertEqual(rc, 0)
        tree = read_tree(into)
        self.assertEqual(tree["shared.md"], b"same content\n")
        self.assertEqual(tree["base-only.md"], b"only in base\n")
        self.assertEqual(tree["other-only.md"], b"only in other\n")
        self.assertEqual(tree["conflict.md"], b"base version\n")
        self.assertEqual(len(tree), 4)

    def test_merge_keep_other(self):
        rc, into = self.do_merge(strategy="keep-other")
        self.assertEqual(rc, 0)
        self.assertEqual(read_tree(into)["conflict.md"], b"other version\n")

    def test_merge_both(self):
        rc, into = self.do_merge(strategy="both")
        self.assertEqual(rc, 0)
        tree = read_tree(into)
        self.assertEqual(tree["conflict.md"], b"base version\n")
        self.assertEqual(tree["conflict.md.other"], b"other version\n")

    def test_merge_report_is_dry_run(self):
        into = os.path.join(self.tmp.name, "merged")
        rc, _ = self.do_merge(strategy="report", into=into)
        self.assertEqual(rc, 10)
        self.assertFalse(os.path.exists(into))

    def test_merge_report_clean_exits_zero(self):
        self.do_export_same()
        rc, _ = self.do_merge(base=self.same1, other=self.same2,
                              strategy="report",
                              into=os.path.join(self.tmp.name, "m2"))
        self.assertEqual(rc, 0)

    def do_export_same(self):
        self.same1 = os.path.join(self.tmp.name, "s1.memport")
        self.same2 = os.path.join(self.tmp.name, "s2.memport")
        for out in (self.same1, self.same2):
            a = Args(source=self.base_src, out=out, name="n",
                     exclude=[], redact=[])
            self.assertEqual(memport.cmd_export(a), 0)

    def test_merge_refuses_nonempty_target(self):
        into = os.path.join(self.tmp.name, "merged")
        os.makedirs(into)
        with open(os.path.join(into, "precious.txt"), "w") as f:
            f.write("do not touch")
        rc, _ = self.do_merge(into=into)
        self.assertEqual(rc, 1)
        self.assertEqual(read_tree(into), {"precious.txt": b"do not touch"})

    def test_merge_unreadable_archive_exit_two(self):
        bogus = os.path.join(self.tmp.name, "bogus.memport")
        with open(bogus, "w") as f:
            f.write("not a tar file at all")
        rc, _ = self.do_merge(base=bogus)
        self.assertEqual(rc, 2)

    def test_merge_tampered_archive_exit_one(self):
        # rebuild the base archive with a corrupted payload but the
        # original manifest -> manifest checksum no longer matches payload
        import io
        import tarfile
        header, entries, blobs, _fl = memport.read_archive(self.base)
        with open(self.base, "rb") as f:
            raw_manifest = tarfile.open("r:gz", fileobj=io.BytesIO(f.read())
                                        ).extractfile("MANIFEST.txt").read()
        tampered = os.path.join(self.tmp.name, "tampered.memport")
        blobs = dict(blobs)
        blobs["conflict.md"] = b"corrupted payload\n"
        with tarfile.open(tampered, "w:gz") as tar:
            mi = tarfile.TarInfo("MANIFEST.txt")
            mi.size = len(raw_manifest)
            tar.addfile(mi, io.BytesIO(raw_manifest))
            for rel, _digest, _size in entries:
                ti = tarfile.TarInfo("files/" + rel)
                ti.size = len(blobs[rel])
                tar.addfile(ti, io.BytesIO(blobs[rel]))
        rc, _ = self.do_merge(base=tampered,
                              into=os.path.join(self.tmp.name, "m3"))
        self.assertEqual(rc, 1)


class TestCollect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "root")
        os.makedirs(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def do_collect(self, platform, files, root=None):
        root = root or self.root
        make_tree(root, files)
        out = os.path.join(self.tmp.name, "out")
        a = Args(platform=platform, root=root, out=out)
        self.assertEqual(memport.cmd_collect(a), 0)
        return read_tree(out)

    def test_claude_code_collects_memory_not_transcripts(self):
        got = self.do_collect("claude-code", {
            "CLAUDE.md": "# project\n",
            "CLAUDE.local.md": "# personal\n",
            ".claude/rules/api.md": "# api rules\n",
            ".claude/projects/proj/session.jsonl": '{"x":1}\n',  # decoy
            ".claude/settings.json": "{}\n",                    # decoy
            "notes.txt": "random\n",                            # decoy
        })
        self.assertIn("claude-code/CLAUDE.md", got)
        self.assertIn("claude-code/CLAUDE.local.md", got)
        self.assertIn("claude-code/.claude/rules/api.md", got)
        self.assertIn("SOURCE.txt", got)
        for rel in got:
            self.assertNotIn("session.jsonl", rel)
            self.assertNotIn("settings.json", rel)
            self.assertNotIn("notes.txt", rel)

    def test_claude_code_home_root(self):
        home = os.path.join(self.tmp.name, "home", ".claude")
        got = self.do_collect("claude-code", {
            "CLAUDE.md": "# user prefs\n",
            "rules/personal.md": "# personal rules\n",
        }, root=home)
        self.assertIn("claude-code/CLAUDE.md", got)
        self.assertIn("claude-code/rules/personal.md", got)

    def test_cursor_collects_mdc_not_md(self):
        got = self.do_collect("cursor", {
            ".cursor/rules/api.mdc": "---\nalwaysApply: true\n---\n",
            ".cursor/rules/draft.md": "ignored by Cursor\n",  # decoy
            ".cursorrules": "legacy\n",
            "AGENTS.md": "# agents\n",
            ".cursor/settings.json": "{}\n",                  # decoy
        })
        self.assertIn("cursor/.cursor/rules/api.mdc", got)
        self.assertIn("cursor/.cursorrules", got)
        self.assertIn("cursor/AGENTS.md", got)
        self.assertNotIn("cursor/.cursor/rules/draft.md", got)
        self.assertNotIn("cursor/.cursor/settings.json", got)

    def test_opencode_collects_markdown_only(self):
        got = self.do_collect("opencode", {
            "AGENTS.md": "# agents\n",
            "sub/AGENTS.md": "# nested\n",
            ".opencode/project.md": "# project memory\n",
            ".opencode/agent/review.md": "# review\n",
            ".opencode/opencode.json": "{}\n",  # decoy: config, not memory
        })
        self.assertIn("opencode/AGENTS.md", got)
        self.assertIn("opencode/sub/AGENTS.md", got)
        self.assertIn("opencode/.opencode/project.md", got)
        self.assertIn("opencode/.opencode/agent/review.md", got)
        self.assertNotIn("opencode/.opencode/opencode.json", got)

    def test_codex_collects_agents_files(self):
        got = self.do_collect("codex", {
            "AGENTS.md": "# agents\n",
            "AGENTS.override.md": "# override\n",
            "src/main.py": "print(1)\n",  # decoy
        })
        self.assertIn("codex/AGENTS.md", got)
        self.assertIn("codex/AGENTS.override.md", got)
        self.assertNotIn("codex/src/main.py", got)

    def test_gemini_collects_gemini_md(self):
        got = self.do_collect("gemini", {
            "GEMINI.md": "# project\n",
            ".gemini/GEMINI.md": "# config dir\n",
            ".gemini/chats/x.json": "{}\n",  # decoy: saved session
        })
        self.assertIn("gemini/GEMINI.md", got)
        self.assertIn("gemini/.gemini/GEMINI.md", got)
        self.assertNotIn("gemini/.gemini/chats/x.json", got)

    def test_generic_collects_memory_tree(self):
        got = self.do_collect("generic", {
            "MEMORY.md": "# memory\n",
            "memory/2026-10-22.md": "notes\n",
            "memory/people/friend.md": "bio\n",
            "AGENTS.md": "# agents\n",
        })
        self.assertIn("generic/MEMORY.md", got)
        self.assertIn("generic/memory/2026-10-22.md", got)
        self.assertIn("generic/memory/people/friend.md", got)

    def test_source_txt_records_provenance(self):
        got = self.do_collect("generic", {"MEMORY.md": "x\n"})
        src = got["SOURCE.txt"].decode("utf-8")
        self.assertIn("platform: generic", src)
        self.assertIn("root: ", src)
        self.assertIn("collector: memport ", src)
        self.assertIn("files: 1", src)

    def test_symlink_not_collected(self):
        make_tree(self.root, {"MEMORY.md": "x\n"})
        os.symlink(os.path.join(self.root, "MEMORY.md"),
                   os.path.join(self.root, "LINK.md"))
        out = os.path.join(self.tmp.name, "out")
        a = Args(platform="generic", root=self.root, out=out)
        self.assertEqual(memport.cmd_collect(a), 0)
        got = read_tree(out)
        self.assertNotIn("generic/LINK.md", got)

    def test_collect_unknown_platform_exit_two(self):
        a = Args(platform="nope", root=self.root,
                 out=os.path.join(self.tmp.name, "out"))
        self.assertEqual(memport.cmd_collect(a), 2)

    def test_collect_missing_root_exit_two(self):
        a = Args(platform="generic", root=os.path.join(self.tmp.name, "nope"),
                 out=os.path.join(self.tmp.name, "out"))
        self.assertEqual(memport.cmd_collect(a), 2)

    def test_collect_refuses_nonempty_out(self):
        out = os.path.join(self.tmp.name, "out")
        os.makedirs(out)
        make_tree(out, {"junk.txt": "x"})
        a = Args(platform="generic", root=self.root, out=out)
        self.assertEqual(memport.cmd_collect(a), 1)

    def test_export_from_end_to_end(self):
        make_tree(self.root, {
            "CLAUDE.md": "# project\n",
            ".claude/projects/x/session.jsonl": "{}\n",  # decoy
        })
        arc = os.path.join(self.tmp.name, "snap.memport")
        a = Args(source=None, from_platform="claude-code", plat_root=self.root,
                 out=arc, name=None, exclude=[], redact=[])
        self.assertEqual(memport.cmd_export(a), 0)
        header, entries, blobs, _fl = memport.read_archive(arc)
        self.assertEqual(header.get("source-platform"), "claude-code")
        self.assertEqual(header.get("source-root"),
                         os.path.abspath(self.root))
        rels = [rel for rel, _d, _s in entries]
        self.assertIn("claude-code/CLAUDE.md", rels)
        self.assertIn("SOURCE.txt", rels)
        self.assertEqual(len(rels), 2)
        # Import roundtrip preserves the collected tree.
        dst = os.path.join(self.tmp.name, "dst")
        a2 = Args(file=arc, target=dst, layout="preserve")
        self.assertEqual(memport.cmd_import(a2), 0)
        got = read_tree(dst)
        self.assertEqual(got["claude-code/CLAUDE.md"], b"# project\n")

    def test_export_from_no_matches_exit_two(self):
        make_tree(self.root, {"random.txt": "x\n"})
        arc = os.path.join(self.tmp.name, "snap.memport")
        a = Args(source=None, from_platform="codex", plat_root=self.root,
                 out=arc, name=None, exclude=[], redact=[])
        self.assertEqual(memport.cmd_export(a), 2)

    def test_export_source_and_from_conflict_exit_two(self):
        arc = os.path.join(self.tmp.name, "snap.memport")
        a = Args(source=self.root, from_platform="codex",
                 plat_root=self.root, out=arc, name=None,
                 exclude=[], redact=[])
        self.assertEqual(memport.cmd_export(a), 2)

    def test_export_neither_source_nor_from_exit_two(self):
        arc = os.path.join(self.tmp.name, "snap.memport")
        a = Args(source=None, from_platform=None, plat_root=None,
                 out=arc, name=None, exclude=[], redact=[])
        self.assertEqual(memport.cmd_export(a), 2)


class TestForget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "src")
        os.makedirs(self.src)
        make_tree(self.src, SAMPLE)
        self.arc = os.path.join(self.tmp.name, "snap.memport")
        a = Args(source=self.src, out=self.arc, name="snap",
                 exclude=[], redact=[])
        self.assertEqual(memport.cmd_export(a), 0)

    def tearDown(self):
        self.tmp.cleanup()

    def do_forget(self, arc=None, out=None, in_place=False, **kw):
        a = Args(file=arc or self.arc, path=kw.get("path", []),
                 reason=kw.get("reason"), output=out,
                 in_place=in_place, dry_run=kw.get("dry_run", False))
        return memport.cmd_forget(a)

    def read(self, arc):
        return memport.read_archive(arc)

    def test_forget_removes_matched(self):
        new = os.path.join(self.tmp.name, "pruned.memport")
        self.assertEqual(self.do_forget(out=new, path=["memory/*"],
                                        reason="stale notes"), 0)
        header, entries, blobs, forgetlog = self.read(new)
        rels = [rel for rel, _d, _s in entries]
        self.assertNotIn("memory/2026-10-21.md", rels)
        self.assertNotIn("memory/people/friend.md", rels)
        self.assertIn("MEMORY.md", rels)
        self.assertIn("notes.bin", rels)
        self.assertEqual(header.get("file-count"), "2")
        self.assertEqual(header.get("forget-operations"), "1")
        # Kept payloads still verify.
        self.assertEqual(memport.verify_blobs(entries, blobs), [])
        # Audit log records the operation.
        self.assertTrue(forgetlog.startswith(memport.FORGET_MAGIC))
        self.assertIn("reason: stale notes", forgetlog)
        self.assertIn("memory/2026-10-21.md", forgetlog)
        self.assertIn("memory/people/friend.md", forgetlog)
        self.assertIn("archive-sha256-before: ", forgetlog)
        # Pruned archive imports cleanly.
        dst = os.path.join(self.tmp.name, "dst")
        a = Args(file=new, target=dst, layout="preserve")
        self.assertEqual(memport.cmd_import(a), 0)
        self.assertEqual(set(read_tree(dst)),
                         {"MEMORY.md", "notes.bin"})

    def test_forget_chain_appends_and_links(self):
        arc1 = os.path.join(self.tmp.name, "f1.memport")
        self.assertEqual(self.do_forget(out=arc1, path=["memory/*"]), 0)
        arc2 = os.path.join(self.tmp.name, "f2.memport")
        self.assertEqual(self.do_forget(arc=arc1, out=arc2,
                                        path=["notes.bin"]), 0)
        header, entries, _blobs, forgetlog = self.read(arc2)
        self.assertEqual(header.get("forget-operations"), "2")
        self.assertEqual([r for r, _d, _s in entries], ["MEMORY.md"])
        ops = [line for line in forgetlog.splitlines()
               if line.strip() == "---"]
        self.assertEqual(len(ops), 2)
        self.assertTrue(forgetlog.startswith(memport.FORGET_MAGIC))
        with open(arc1, "rb") as f:
            arc1_sha = memport.sha256_bytes(f.read())
        # The second operation's block links back to the first archive file.
        second_block = forgetlog.split("---\n")[-1]
        self.assertIn("archive-sha256-before: %s" % arc1_sha, second_block)

    def test_forget_no_match_is_noop(self):
        new = os.path.join(self.tmp.name, "pruned.memport")
        self.assertEqual(self.do_forget(out=new, path=["nope*"]), 0)
        self.assertFalse(os.path.exists(new))
        _h, entries, _b, _f = self.read(self.arc)
        self.assertEqual(len(entries), 4)  # original untouched

    def test_forget_dry_run_writes_nothing(self):
        # --dry-run needs no --output and writes nothing.
        a = Args(file=self.arc, path=["memory/*"], reason=None,
                 output=None, in_place=False, dry_run=True)
        self.assertEqual(memport.cmd_forget(a), 0)
        _h, entries, _b, _f = self.read(self.arc)
        self.assertEqual(len(entries), 4)

    def test_forget_refuses_tampered_archive(self):
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
        new = os.path.join(self.tmp.name, "pruned.memport")
        self.assertEqual(self.do_forget(out=new, path=["memory/*"]), 1)
        self.assertFalse(os.path.exists(new))

    def test_forget_refuses_to_empty_archive(self):
        new = os.path.join(self.tmp.name, "pruned.memport")
        self.assertEqual(self.do_forget(out=new, path=["*"]), 1)
        self.assertFalse(os.path.exists(new))

    def test_forget_protects_source_txt(self):
        make_tree(self.src, {"SOURCE.txt": "provenance\n"})
        a = Args(source=self.src, out=self.arc, name="snap",
                 exclude=[], redact=[])
        self.assertEqual(memport.cmd_export(a), 0)
        new = os.path.join(self.tmp.name, "pruned.memport")
        self.assertEqual(self.do_forget(out=new, path=["*"]), 0)
        _h, entries, blobs, _f = self.read(new)
        self.assertEqual([r for r, _d, _s in entries], ["SOURCE.txt"])
        self.assertEqual(blobs["SOURCE.txt"], b"provenance\n")
        self.assertEqual(memport.verify_blobs(entries, blobs), [])

    def test_forget_in_place(self):
        self.assertEqual(self.do_forget(in_place=True,
                                        path=["notes.bin"]), 0)
        _h, entries, blobs, _f = self.read(self.arc)
        rels = [r for r, _d, _s in entries]
        self.assertNotIn("notes.bin", rels)
        self.assertEqual(len(rels), 3)
        self.assertEqual(memport.verify_blobs(entries, blobs), [])

    def test_forget_needs_exactly_one_output(self):
        a = Args(file=self.arc, path=["x"], reason=None,
                 output=None, in_place=False, dry_run=False)
        self.assertEqual(memport.cmd_forget(a), 2)
        a = Args(file=self.arc, path=["x"], reason=None,
                 output="/tmp/x.memport", in_place=True, dry_run=False)
        self.assertEqual(memport.cmd_forget(a), 2)
        # --dry-run is exempt from the output requirement, but combining
        # it with an output destination is contradictory.
        a = Args(file=self.arc, path=["x"], reason=None,
                 output=None, in_place=False, dry_run=True)
        self.assertEqual(memport.cmd_forget(a), 0)
        a = Args(file=self.arc, path=["x"], reason=None,
                 output="/tmp/x.memport", in_place=False, dry_run=True)
        self.assertEqual(memport.cmd_forget(a), 2)

    def test_forget_usage_errors(self):
        # no --path
        self.assertEqual(self.do_forget(out="/tmp/x.memport"), 2)
        # unreadable archive
        a = Args(file=os.path.join(self.tmp.name, "nope.memport"),
                 path=["x"], reason=None, output="/tmp/x.memport",
                 in_place=False, dry_run=False)
        self.assertEqual(memport.cmd_forget(a), 2)

    def test_inspect_reports_forget_log(self):
        new = os.path.join(self.tmp.name, "pruned.memport")
        self.assertEqual(self.do_forget(out=new, path=["notes.bin"]), 0)
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        a = Args(file=new, verify=True)
        with redirect_stdout(buf):
            self.assertEqual(memport.cmd_inspect(a), 0)
        out = buf.getvalue()
        self.assertIn("forget-operations: 1", out)
        self.assertIn("forget-log:", out)
        self.assertIn("verify: OK", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
