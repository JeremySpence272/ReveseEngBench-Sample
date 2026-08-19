#!/bin/bash
# GDB (GNU Debugger) with an MCP bridge. Installs the mcp-gdb Node.js server,
# which exposes GDB's debugging capabilities (breakpoints, stepping, memory
# inspection) as an MCP server.
#
# Sources:
#   GDB       https://www.gnu.org/software/gdb/                (GNU Project, GPL-3.0)
#   mcp-gdb   https://github.com/signal-slot/mcp-gdb           (signal-slot, MIT)
set -e

# Install mcp-gdb 0.1.3
curl -fsSL -o /tmp/mcp-gdb.zip \
    https://github.com/signal-slot/mcp-gdb/archive/refs/tags/v0.1.3.zip
unzip -q /tmp/mcp-gdb.zip -d /opt
mv /opt/mcp-gdb-0.1.3 /opt/mcp-gdb
rm /tmp/mcp-gdb.zip
npm --prefix /opt/mcp-gdb ci
npm --prefix /opt/mcp-gdb run build
