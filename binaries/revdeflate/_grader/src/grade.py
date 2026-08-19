#!/usr/bin/env python3
"""RevDeflate challenge grader.

Compares a student's recovered output against the ground-truth original for a challenge,
reporting an exact-match pass/fail with a detailed breakdown.  Stdlib-only; does NOT
decompress anything — it only reads and compares files.

Usage (single level):
    python3 tools/grade.py --challenges CHDIR --level N STUDENT_PATH

    STUDENT_PATH for single-file levels (L1/L2, original_kind="file"):
        the student's recovered file.
    STUDENT_PATH for directory levels (L3-L6, original_kind="dir"):
        the student's recovered directory tree.

Usage (batch, all levels):
    python3 tools/grade.py --challenges CHDIR --batch STUDENT_DIR

    Expected layout under STUDENT_DIR for batch mode:
        L{n}      — a file (single-file levels) or a directory (dir levels)
        L{n}.bin  — accepted as an alternative for single-file levels (L1/L2)

Options:
    --ignore-modes       Compare paths, kinds, and file contents only; skip permission bits.
    --ignore-empty-dirs  Ignore empty directories in both GT and student output.

Exit codes:
    0  all graded levels PASS
    1  one or more levels FAIL
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _snapshot(root: str) -> dict:
    """Map relpath -> (is_dir, perm & 0o777, data-or-None) for every entry under root.
    Mirrors tools.make_challenges._snapshot exactly."""
    out: dict = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            p = os.path.join(dirpath, d)
            out[os.path.relpath(p, root)] = (True, os.stat(p).st_mode & 0o777, None)
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            with open(p, "rb") as f:
                out[os.path.relpath(p, root)] = (False, os.stat(p).st_mode & 0o777, f.read())
    return out


def _remove_empty_dirs(snap: dict) -> dict:
    """Return a copy of snap with all empty-directory entries removed.
    A directory is "empty" if no other path in snap has it as a prefix ancestor."""
    dir_paths = {p for p, (is_dir, _, _) in snap.items() if is_dir}
    non_empty_dirs: set = set()
    for p in snap:
        parts = Path(p).parts
        for i in range(1, len(parts)):
            non_empty_dirs.add(str(Path(*parts[:i])))
    empty_dirs = dir_paths - non_empty_dirs
    return {p: v for p, v in snap.items() if p not in empty_dirs}


def _load_manifest(chdir: str) -> dict:
    path = os.path.join(chdir, "manifest.json")
    if not os.path.isfile(path):
        raise FileNotFoundError("manifest.json not found in %s" % chdir)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _find_level_entry(manifest: dict, level: int):
    for entry in manifest.get("levels", []):
        if entry.get("level") == level:
            return entry
    return None


# ---------------------------------------------------------------------------
# File-level grading.
# ---------------------------------------------------------------------------

def _grade_file(gt_path: str, student_path: str):
    """Compare single-file GT to student_path. Returns (passed: bool, report: str)."""
    lines = []
    if not os.path.exists(student_path):
        return False, "  student file not found: %s" % student_path
    if os.path.isdir(student_path):
        return False, "  student path is a directory, expected a file: %s" % student_path

    with open(gt_path, "rb") as f:
        gt_data = f.read()
    with open(student_path, "rb") as f:
        student_data = f.read()

    gt_sha = _sha256(gt_data)
    student_sha = _sha256(student_data)
    lines.append("  Ground truth : %d bytes, sha256=%s" % (len(gt_data), gt_sha))
    lines.append("  Student      : %d bytes, sha256=%s" % (len(student_data), student_sha))

    if gt_sha == student_sha:
        lines.append("  Content      : MATCH")
        return True, "\n".join(lines)

    lines.append("  Content      : MISMATCH")
    if len(gt_data) != len(student_data):
        lines.append("  Size diff    : GT=%d bytes, student=%d bytes" % (len(gt_data), len(student_data)))
    return False, "\n".join(lines)


# ---------------------------------------------------------------------------
# Directory-level grading.
# ---------------------------------------------------------------------------

def _grade_dir(gt_root: str, student_root: str,
               ignore_modes: bool = False, ignore_empty_dirs: bool = False):
    """Compare directory GT to student_root. Returns (passed: bool, report: str)."""
    lines = []
    if not os.path.exists(student_root):
        return False, "  student directory not found: %s" % student_root
    if os.path.isfile(student_root):
        return False, "  student path is a file, expected a directory: %s" % student_root

    gt_snap = _snapshot(gt_root)
    student_snap = _snapshot(student_root)

    if ignore_empty_dirs:
        gt_snap = _remove_empty_dirs(gt_snap)
        student_snap = _remove_empty_dirs(student_snap)

    gt_paths = set(gt_snap)
    student_paths = set(student_snap)
    missing = sorted(gt_paths - student_paths)
    extra = sorted(student_paths - gt_paths)
    common = gt_paths & student_paths

    kind_mismatches = []
    perm_mismatches = []
    content_mismatches = []
    matched = 0

    for p in sorted(common):
        gt_is_dir, gt_perm, gt_data = gt_snap[p]
        st_is_dir, st_perm, st_data = student_snap[p]

        if gt_is_dir != st_is_dir:
            kind_mismatches.append((p,
                                    "dir" if gt_is_dir else "file",
                                    "dir" if st_is_dir else "file"))
            continue

        perm_ok = ignore_modes or (gt_perm == st_perm)
        content_ok = gt_is_dir or (gt_data == st_data)

        if not perm_ok:
            perm_mismatches.append((p, st_perm, gt_perm))
        if not content_ok:
            gt_size = len(gt_data) if gt_data is not None else 0
            st_size = len(st_data) if st_data is not None else 0
            content_mismatches.append((p, gt_size, st_size))
        if perm_ok and content_ok:
            matched += 1

    lines.append("  Entries in GT      : %d" % len(gt_snap))
    lines.append("  Entries matched    : %d" % matched)

    if missing:
        sample = missing[:5]
        suffix = (" ... +%d more" % (len(missing) - 5)) if len(missing) > 5 else ""
        lines.append("  MISSING (%d)        : %s%s" % (len(missing), ", ".join(sample), suffix))

    if extra:
        sample = extra[:5]
        suffix = (" ... +%d more" % (len(extra) - 5)) if len(extra) > 5 else ""
        lines.append("  EXTRA (%d)          : %s%s" % (len(extra), ", ".join(sample), suffix))

    if kind_mismatches:
        lines.append("  KIND mismatches (%d):" % len(kind_mismatches))
        for p, want, got in kind_mismatches[:3]:
            lines.append("    %s: want=%s, got=%s" % (p, want, got))

    if perm_mismatches:
        lines.append("  PERMISSION mismatches (%d):" % len(perm_mismatches))
        for p, got, want in perm_mismatches[:3]:
            lines.append("    %s: got=0o%03o, want=0o%03o" % (p, got, want))
        if len(perm_mismatches) > 3:
            lines.append("    ... +%d more" % (len(perm_mismatches) - 3))

    if content_mismatches:
        lines.append("  CONTENT mismatches (%d):" % len(content_mismatches))
        for p, gt_size, st_size in content_mismatches[:3]:
            lines.append("    %s: GT size=%d, student size=%d" % (p, gt_size, st_size))
        if len(content_mismatches) > 3:
            lines.append("    ... +%d more" % (len(content_mismatches) - 3))

    if not ignore_empty_dirs:
        # Highlight missing empty directories specifically (a common student mistake).
        gt_empty = [p for p, (is_dir, _, _) in gt_snap.items()
                    if is_dir and not any(o.startswith(p + "/") for o in gt_snap if o != p)]
        missing_empty = [p for p in gt_empty if p in missing]
        if missing_empty:
            lines.append("  MISSING EMPTY DIRS : %s" % ", ".join(missing_empty[:5]))

    passed = (not missing and not extra and not kind_mismatches
              and not perm_mismatches and not content_mismatches)
    return passed, "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API (also used by the test suite directly).
# ---------------------------------------------------------------------------

def grade_level(chdir: str, level: int, student_path: str,
                ignore_modes: bool = False, ignore_empty_dirs: bool = False,
                _print: bool = True) -> bool:
    """Grade one level. Prints results to stdout if _print=True. Returns True on PASS."""
    def out(msg: str) -> None:
        if _print:
            print(msg)

    try:
        manifest = _load_manifest(chdir)
    except FileNotFoundError as e:
        out("FAIL — %s" % e)
        return False

    entry = _find_level_entry(manifest, level)
    if entry is None:
        out("FAIL — level %d not found in manifest" % level)
        return False

    original_kind = entry.get("original_kind")
    orig_root = os.path.join(chdir, "originals", "L%d" % level)

    if not os.path.isdir(orig_root):
        out("FAIL — originals directory not found: %s" % orig_root)
        return False

    out("Level %d (%s)" % (level, original_kind or "unknown"))

    if original_kind == "file":
        original_name = entry.get("original_name")
        if original_name:
            gt_path = os.path.join(orig_root, original_name)
        else:
            files = [fn for fn in os.listdir(orig_root)
                     if os.path.isfile(os.path.join(orig_root, fn))]
            if len(files) != 1:
                out("FAIL — cannot locate ground-truth file (%d files in %s)" % (len(files), orig_root))
                return False
            gt_path = os.path.join(orig_root, files[0])

        if not os.path.isfile(gt_path):
            out("FAIL — ground-truth file missing: %s" % gt_path)
            return False

        passed, report = _grade_file(gt_path, student_path)

    elif original_kind == "dir":
        passed, report = _grade_dir(orig_root, student_path,
                                    ignore_modes=ignore_modes,
                                    ignore_empty_dirs=ignore_empty_dirs)
    else:
        # Infer: a single file in orig_root -> file level; otherwise dir level.
        items = os.listdir(orig_root)
        if len(items) == 1 and os.path.isfile(os.path.join(orig_root, items[0])):
            passed, report = _grade_file(os.path.join(orig_root, items[0]), student_path)
        else:
            passed, report = _grade_dir(orig_root, student_path,
                                        ignore_modes=ignore_modes,
                                        ignore_empty_dirs=ignore_empty_dirs)

    out(report)
    out("PASS" if passed else "FAIL")
    return passed


def grade_batch(chdir: str, student_dir: str,
                ignore_modes: bool = False, ignore_empty_dirs: bool = False,
                _print: bool = True) -> bool:
    """Grade all levels in the manifest. Returns True iff all pass."""
    def out(msg: str) -> None:
        if _print:
            print(msg)

    try:
        manifest = _load_manifest(chdir)
    except FileNotFoundError as e:
        out("FAIL — %s" % e)
        return False

    levels = manifest.get("levels", [])
    if not levels:
        out("FAIL — manifest has no levels")
        return False

    results = []
    for entry in levels:
        level = entry["level"]
        original_kind = entry.get("original_kind")

        student_path = os.path.join(student_dir, "L%d" % level)
        if not os.path.exists(student_path) and original_kind == "file":
            alt = os.path.join(student_dir, "L%d.bin" % level)
            if os.path.exists(alt):
                student_path = alt

        out("=" * 60)
        passed = grade_level(chdir, level, student_path,
                             ignore_modes=ignore_modes,
                             ignore_empty_dirs=ignore_empty_dirs,
                             _print=_print)
        results.append((level, passed))

    out("=" * 60)
    out("Batch summary:")
    for lv, ok in results:
        out("  L%d: %s" % (lv, "PASS" if ok else "FAIL"))
    n_pass = sum(1 for _, ok in results if ok)
    n_total = len(results)
    out("%d/%d passed" % (n_pass, n_total))
    overall = (n_pass == n_total)
    out("PASS" if overall else "FAIL")
    return overall


# ---------------------------------------------------------------------------
# CLI entry point.
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--challenges", required=True, metavar="CHDIR",
                        help="challenge set directory (contains manifest.json + originals/)")
    parser.add_argument("--level", type=int, metavar="N",
                        help="grade a single level N (1-6)")
    parser.add_argument("--batch", metavar="STUDENT_DIR",
                        help="grade all levels; student output under STUDENT_DIR "
                             "(L{n} or L{n}.bin per level)")
    parser.add_argument("--ignore-modes", action="store_true",
                        help="ignore permission bits (compare paths, kinds, file contents only)")
    parser.add_argument("--ignore-empty-dirs", action="store_true",
                        help="ignore empty directories in both GT and student output")
    parser.add_argument("student_path", nargs="?", metavar="STUDENT_PATH",
                        help="recovered file or directory to grade (single-level mode)")
    args = parser.parse_args(argv)

    if args.batch:
        if args.level is not None or args.student_path:
            parser.error("--batch is mutually exclusive with --level / STUDENT_PATH")
        passed = grade_batch(args.challenges, args.batch,
                             ignore_modes=args.ignore_modes,
                             ignore_empty_dirs=args.ignore_empty_dirs)
    elif args.level is not None:
        if not args.student_path:
            parser.error("STUDENT_PATH is required in single-level mode")
        passed = grade_level(args.challenges, args.level, args.student_path,
                             ignore_modes=args.ignore_modes,
                             ignore_empty_dirs=args.ignore_empty_dirs)
    else:
        parser.error("specify either --level N STUDENT_PATH or --batch STUDENT_DIR")

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
