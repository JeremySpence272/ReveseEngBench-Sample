# Compressor Challenge Brief

You are given `binary`, an encoder-only file compressor. It reads a single file (a path argument, or standard input if no path is given) and writes a compressed stream:

    binary [-o OUT] [INPUT]      # INPUT path or stdin -> OUT path or stdout

It has no decompressor -- inverting the format is your task. Compress inputs you choose and study the output to work out how the format is built, then write a decoder that recovers the original bytes.

## Challenges

`challenge_L1.bin` ... `challenge_L6.bin` are six compressed challenges. L1-L2 each compress a single file; L3-L6 each compress a directory tree.

## Submitting

Recover each original under `/workspace/submission/`: `/submission/L1` and `/submission/L2` are the byte-exact recovered files (an `L{n}.bin` name is also accepted); `/submission/L3` ... `/submission/L6` are the recovered directory trees. A level scores only on an exact match (paths, kinds, permission bits, contents, empty dirs). Score is the number of levels recovered, 0-6.
