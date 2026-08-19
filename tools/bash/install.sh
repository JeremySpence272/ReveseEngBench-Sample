#!/bin/bash
# Basic command-line RE utilities. Provides binutils (objdump, readelf, strings, nm, etc.),
# file (magic number identification), and xxd (hex dump). These are the baseline tools
# available in tier1 with no MCP servers.
#
# Sources:
#   binutils   https://www.gnu.org/software/binutils/   (GNU Project, GPL-3.0)
#   file       https://github.com/file/file              (Ian Darwin, BSD-2-Clause)
#   xxd        https://github.com/vim/vim                (Bram Moolenaar, Vim License)
set -e

# No additional install steps — all packages handled via apt_packages in manifest.json
