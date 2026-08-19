#!/usr/bin/env python3

import hashlib
import json
import os
import subprocess
import tempfile


def evaluate(config, challenge_dir, submission_dir, reasoning_file, agent):
    """Dispatch evaluation based on config['evaluation'].

    return: {
        "correct": bool,
        "additional_info": dict,
    }
    """
    eval_type = config["evaluation"]
    if eval_type == "revdeflate_score":
        return revdeflate_score(config, challenge_dir, submission_dir)
    else:
        raise ValueError(f"unknown evaluation type: {eval_type}")


_revdeflate_grader_tag = None


def _get_revdeflate_grader_image():
    """Build (or retrieve cached) the RevDeflate grader image.

    One challenge-agnostic image bakes the private ground truth (originals +
    manifest.json) plus the stdlib byte-comparison grader (grade.py/score.py). The
    per-variant encoder the agent reverses is NOT needed here: grading only
    byte-compares the recovered /submission tree against the private originals, and
    the challenge set is identical across every variant.
    """
    global _revdeflate_grader_tag
    if _revdeflate_grader_tag is not None:
        return _revdeflate_grader_tag

    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    ctx = os.path.join(root, "binaries", "revdeflate", "_grader")

    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(ctx):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for fn in sorted(filenames):
            if fn.endswith(".pyc"):
                continue
            fp = os.path.join(dirpath, fn)
            h.update(os.path.relpath(fp, ctx).encode())
            with open(fp, "rb") as f:
                while chunk := f.read(8192):
                    h.update(chunk)
    tag = f"revbench-revdeflate-grader-{h.hexdigest()[:12]}"

    result = subprocess.run(["docker", "image", "inspect", tag], capture_output=True)
    if result.returncode == 0:
        print(f"RevDeflate grader image {tag} already exists, skipping build.")
    else:
        print(f"Building RevDeflate grader image {tag}...")
        subprocess.run(["docker", "build", "-t", tag, ctx], check=True)
        print(f"Built RevDeflate grader image {tag}")
    _revdeflate_grader_tag = tag
    return tag


def revdeflate_score(config, challenge_dir, submission_dir):
    """Grade recovered originals against the private ground truth in the grader
    image and read the 0-6 score it writes to /tmp/result.json. The submission is
    inert data (recovered files/trees under /submission/L1..L6); the grader only
    byte-compares it to the baked originals -- it never executes anything from the
    submission and never decompresses. Anti-smuggle: test.sh strips any symlinks
    from /submission before grading so a submission cannot alias the private
    originals. --network none.
    """
    image = _get_revdeflate_grader_image()
    submission_dir = os.path.abspath(submission_dir)

    container_name = f"revbench-revdeflate-{os.getpid()}"
    host_result = tempfile.NamedTemporaryFile(
        prefix="revbench-revdeflate-", suffix=".json", delete=False
    ).name
    cp_returncode = 1
    result = None
    try:
        subprocess.run(
            [
                "docker",
                "create",
                "--name",
                container_name,
                "--network",
                "none",
                "--security-opt",
                "no-new-privileges",
                "--cpus",
                "1",
                "--memory",
                "512m",
                image,
                "/grader/test.sh",
            ],
            check=True,
            capture_output=True,
        )
        # Stream the submission into the grader via a root-privileged tar (the grader
        # image runs as root) so root-owned, restrictive-mode dirs -- which revdeflate
        # legitimately grades on permission bits -- stay readable and keep their exact
        # modes, whatever the harness user's privileges. A host-side `docker cp` tars as
        # the harness user and fails on such trees on a non-root host.
        parent = os.path.dirname(submission_dir)
        base = os.path.basename(submission_dir)
        tar_proc = subprocess.Popen(
            ["docker", "run", "--rm", "-i", "--network", "none",
             "-v", f"{parent}:/w:ro", image,
             "tar", "-C", "/w", "--numeric-owner", "-cf", "-", base],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        cp_in = subprocess.run(
            ["docker", "cp", "-", f"{container_name}:/"],
            stdin=tar_proc.stdout,
            capture_output=True,
        )
        tar_proc.stdout.close()
        tar_proc.wait()
        if cp_in.returncode != 0:
            raise RuntimeError(
                "grader submission load failed: "
                + cp_in.stderr.decode(errors="replace")
            )
        result = subprocess.run(
            ["docker", "start", "-a", container_name],
            capture_output=True,
            text=True,
            timeout=240,
        )
        cp = subprocess.run(
            ["docker", "cp", f"{container_name}:/tmp/result.json", host_result],
            capture_output=True,
        )
        cp_returncode = cp.returncode
    finally:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    score = 0
    max_score = 6
    levels = {}
    if cp_returncode == 0:
        try:
            with open(host_result) as f:
                parsed = json.load(f)
            score = int(parsed.get("score", 0))
            max_score = int(parsed.get("max", 6))
            levels = parsed.get("levels") or {}
        except (ValueError, json.JSONDecodeError, OSError):
            pass
    if os.path.isfile(host_result):
        try:
            os.unlink(host_result)
        except OSError:
            pass

    return {
        "correct": score >= 1,
        "additional_info": {
            "score": score,
            "max": max_score,
            "levels": levels,
            "stdout": result.stdout.strip()[-2000:] if result and result.stdout else "",
            "stderr": result.stderr.strip()[-2000:] if result and result.stderr else "",
        },
    }
