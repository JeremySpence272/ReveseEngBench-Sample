#!/usr/bin/env python3


AVAILABLE_TOOLS_NOTE = (
    "\n\nThe reverse-engineering tools available in this environment are listed, "
    "with usage notes and which are exposed as MCP servers, in "
    "`/workspace/available_tools.yaml`."
)


REVDEFLATE_FOOTER = """

#### The encoder oracle
There is no live oracle or scoring endpoint for this challenge. The `binary` shipped with this challenge is the compressor itself (encoder only -- it has no decompressor), and you may run it freely to study the format:

```
./binary [-o OUT] [INPUT]      # reads INPUT (or stdin if omitted), writes the compressed stream to OUT (or stdout)
```

Compress inputs you choose and study the output to work out the format. Because the encoder is deterministic, you can verify a decoder you write by compressing a known input and checking your decoder recovers it byte-for-byte.

You are also given six compressed challenge files `auxilliary_information/challenge_L1.bin` ... `challenge_L6.bin`. L1-L2 each compress a single file; L3-L6 each compress a directory tree.

#### Submitting
Recover each original and write it under `/workspace/submission/`, named by level:

- `/submission/L1`, `/submission/L2` -- the recovered byte-exact FILE (a file named `L{n}.bin` is also accepted for these single-file levels).
- `/submission/L3` ... `/submission/L6` -- the recovered DIRECTORY TREE.

Grading is one-shot and strict per level: a level passes only on an EXACT match (all paths, file/dir kinds, permission bits, byte contents, and empty directories). Your score is the number of levels recovered, 0-6. The ground-truth originals are held server-side, so they cannot be guessed or looked up -- only a genuine decoder recovers them.
"""


def build_revdeflate_prompt(base_prompt):
    """Append the encoder-oracle + submission instructions to a revdeflate prompt.

    revdeflate ships the agent a stripped, encoder-only compressor whose output
    looks like a standard format but whose codec is subtly non-standard, so stock
    tools fail and the agent must reverse the deviation. Encoder-oracle +
    /submission/L1..L6 contract, graded 0-6 by the byte-comparison grader. The
    prompt stays format-neutral so recognition is part of what is measured.
    """
    return base_prompt + AVAILABLE_TOOLS_NOTE + REVDEFLATE_FOOTER
