#!/usr/bin/env python3
"""RevDeflate score wrapper: grade all six levels and emit an integer 0-6.

Uses the vendored byte-comparison grader (grade.py) which never decompresses --
it only compares the recovered /submission tree to the private ground-truth
originals. grade.grade_batch returns only an overall all-pass bool, so this
counts the per-level passes and writes {"score": N, "max": 6, "levels": {...}}.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
import grade  # noqa: E402


def main():
    chdir = sys.argv[1]
    student_dir = sys.argv[2]
    out_path = sys.argv[3]

    manifest = grade._load_manifest(chdir)
    levels = {}
    for entry in manifest.get("levels", []):
        level = entry["level"]
        original_kind = entry.get("original_kind")
        student_path = os.path.join(student_dir, "L%d" % level)
        if not os.path.exists(student_path) and original_kind == "file":
            alt = os.path.join(student_dir, "L%d.bin" % level)
            if os.path.exists(alt):
                student_path = alt
        passed = grade.grade_level(chdir, level, student_path, _print=False)
        levels[str(level)] = bool(passed)

    score = sum(1 for v in levels.values() if v)
    with open(out_path, "w") as f:
        json.dump({"score": score, "max": 6, "levels": levels}, f)
    print("grader: RevDeflate score %d/6" % score, file=sys.stderr)


if __name__ == "__main__":
    main()
