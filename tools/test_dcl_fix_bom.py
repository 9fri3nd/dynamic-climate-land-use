import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dcl_fix_bom as fix  # noqa: E402

BOM = b"\xef\xbb\xbf"


class FixFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, name, raw):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def test_adds_bom_when_missing(self):
        path = self.write("a.txt", b"key = value\n")
        self.assertEqual(fix.fix_file(path), "added")
        self.assertEqual(path.read_bytes(), BOM + b"key = value\n")

    def test_never_doubles_existing_bom(self):
        original = BOM + b"key = value\n"
        path = self.write("a.gui", original)
        self.assertEqual(fix.fix_file(path), "ok")
        self.assertEqual(path.read_bytes(), original)

    def test_skips_binary_suspicious_files(self):
        for raw in (b"\x00\x01\x02", b"\xff\xfe\xfd not utf-8"):
            path = self.write("bad.txt", raw)
            self.assertEqual(fix.fix_file(path), "skipped")
            self.assertEqual(path.read_bytes(), raw)

    def test_iter_targets_prunes_non_targets(self):
        wanted = [self.write(n, b"") for n in
                  ("in_game/common/x.txt", "main_menu/y.gui", "main_menu/loc/z.yml")]
        for ignored in ("in_game/common/x.py", "notes.md", ".metadata/metadata.json",
                        "tools/__pycache__/cached.txt", ".hidden/secret.txt", "data.csv"):
            self.write(ignored, b"")
        self.assertEqual(sorted(fix.iter_targets(self.root)), sorted(wanted))

    def test_main_is_idempotent(self):
        path = self.write("in_game/common/x.txt", b"key = value\n")
        self.assertEqual(fix.main([str(self.root)]), 0)
        first = path.read_bytes()
        self.assertTrue(first.startswith(BOM))
        self.assertEqual(fix.main([str(self.root)]), 0)
        self.assertEqual(path.read_bytes(), first)

    def test_main_reports_skips_as_failure(self):
        self.write("bad.yml", b"\x00binary")
        self.assertEqual(fix.main([str(self.root)]), 1)

    def test_main_rejects_missing_root(self):
        self.assertEqual(fix.main([str(self.root / "nope")]), 1)


if __name__ == "__main__":
    unittest.main()
