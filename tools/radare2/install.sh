#!/bin/bash
# radare2 - Open-source reverse engineering framework with an MCP bridge.
# Installs radare2 from prebuilt .deb packages and the r2mcp MCP server
# via r2pm, which exposes radare2's analysis capabilities (disassembly,
# decompilation, strings, xrefs, etc.) as an MCP server over stdio.
#
# Sources:
#   radare2      https://github.com/radareorg/radare2      (radareorg, LGPL-3.0)
#   radare2-mcp  https://github.com/radareorg/radare2-mcp  (radareorg, LGPL-3.0)
set -e

R2_VERSION=6.1.4

# Install radare2 from prebuilt .deb (r2mcp requires >= 6.1.2).
curl -fsSL -o /tmp/radare2.deb \
    "https://github.com/radareorg/radare2/releases/download/${R2_VERSION}/radare2_${R2_VERSION}_amd64.deb"
curl -fsSL -o /tmp/radare2-dev.deb \
    "https://github.com/radareorg/radare2/releases/download/${R2_VERSION}/radare2-dev_${R2_VERSION}_amd64.deb"
dpkg -i /tmp/radare2.deb /tmp/radare2-dev.deb
rm /tmp/radare2.deb /tmp/radare2-dev.deb

# Install r2mcp via r2pm (radare2 package manager).
r2pm -Ui r2mcp
