#!/bin/bash
# RevDeflate one-shot grader. Byte-compares the recovered /submission tree to the
# private ground-truth originals baked into the image and writes {"score": N,
# "max": 6}. Fail-closed: any error path leaves a score of 0.
set -u

GRADER_DIR="$(cd "$(dirname "$0")" && pwd)"
CHDIR="${REVDEFLATE_CHALLENGES:-$GRADER_DIR/challenges}"
SUB_DIR="${SUBMISSION_DIR:-/submission}"
OUT="${RESULT_OUT:-/tmp/result.json}"

emit() { printf '{"score": %s, "max": 6}\n' "${1:-0}" > "$OUT"; }
emit 0  # fail-closed default

[ -d "$SUB_DIR" ] || { echo "grader: no submission dir at $SUB_DIR" >&2; exit 0; }

# Anti-smuggle: the submission is inert recovered data, but grade.py opens files
# with open() (which follows symlinks). Strip every symlink so a submission cannot
# alias the private originals baked at /grader/challenges (a symlink L1 -> the
# original would otherwise resolve inside this container and score a free pass).
find "$SUB_DIR" -type l -delete 2>/dev/null || true

python3 "$GRADER_DIR/score.py" "$CHDIR" "$SUB_DIR" "$OUT" >&2 || { emit 0; exit 0; }

exit 0
