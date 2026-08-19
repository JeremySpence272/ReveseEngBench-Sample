# Compressor Reverse-Engineering Challenge

You are given `binary`, an encoder-only build of a file compressor. It compresses a file (a path argument, or stdin) into a compressed stream and has no decompressor. Your task is to reverse the format and write a decoder. In `auxilliary_information/` you have six compressed challenges `challenge_L1.bin` ... `challenge_L6.bin`. Recover each original: L1-L2 are single files; L3-L6 are directory trees. Use the encoder as an oracle: compress inputs you choose and study the output. See `auxilliary_information/challenge.md` for the CLI.
