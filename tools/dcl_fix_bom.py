"""Prepend the UTF-8 BOM to every Jomini game file that lacks it (one-shot).

EU5 requires UTF-8 with BOM on .txt/.gui/.yml files; the Edit/Write tools strip
the BOM on every save (AGENTS.md section 4.7), so this script re-prepends it
before deploy and before the static audit. It walks the mod root, prunes
dot-directories and __pycache__, and only touches files whose first three bytes
are not already EF BB BF -- the BOM is never doubled. Binary-suspicious files
(containing NUL bytes or not decodable as UTF-8) are skipped with a warning and
counted as failures so a silent partial run cannot pass unnoticed.

Usage: python tools/dcl_fix_bom.py [mod_root]
Exit 0 when every target file carries the BOM afterwards, 1 when any file was
skipped. Running it again is a no-op (idempotent).
"""

import os
import sys
from pathlib import Path

BOM = b"\xef\xbb\xbf"
EXTS = (".txt", ".gui", ".yml")


def is_binary_suspicious(raw):
    """True for bytes that must not be treated as Jomini text."""
    if b"\x00" in raw:
        return True
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def fix_file(path):
    """Ensure path starts with the UTF-8 BOM. Returns 'added', 'ok' or 'skipped'."""
    raw = path.read_bytes()
    if raw.startswith(BOM):
        return "ok"
    if is_binary_suspicious(raw):
        return "skipped"
    path.write_bytes(BOM + raw)
    return "added"


def iter_targets(root):
    """Yield every .txt/.gui/.yml under root, pruning dot-dirs and __pycache__."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__pycache__")
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() in EXTS:
                yield Path(dirpath) / name


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    root = Path(argv[0]) if argv else Path(__file__).resolve().parents[1]
    if not root.is_dir():
        print(f"mod root is not a directory: {root}", file=sys.stderr)
        return 1
    counts = {"added": 0, "ok": 0, "skipped": 0}
    for path in iter_targets(root):
        result = fix_file(path)
        counts[result] += 1
        if result != "ok":
            rel = path.relative_to(root).as_posix()
            print(f"{result.upper()} {rel}")
    print(f"bom: {counts['added']} added, {counts['ok']} already ok, "
          f"{counts['skipped']} skipped (binary-suspicious)")
    return 1 if counts["skipped"] else 0


if __name__ == "__main__":
    sys.exit(main())
