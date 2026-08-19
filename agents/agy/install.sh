#!/bin/bash
# Antigravity CLI (agy) agent. Installs the `agy` binary, which drives
# Gemini 3.1 Pro on the Vertex/global endpoint (unmoderated, unlike gemini-cli).
#
# Source: https://antigravity.google/cli/install.sh  (Google)
set -e

apt-get update && apt-get install -y curl python3 ca-certificates

# node/npm are required by MCP tool builds bundled in the image (e.g. mcp-gdb).
# The JS-based agents provide these as a side effect; agy (a Go binary) must
# install them explicitly or the bundled tool installs fail with "npm not found".
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs

# Official installer registers `agy` under ~/.local/bin.
# TODO(pin): the installer ships latest; pin to a known-good version once the
# installer exposes a version arg (mirror the claude @2.1.197 discipline).
curl -fsSL https://antigravity.google/cli/install.sh | bash

# Make agy reachable regardless of PATH inside the container.
ln -sf /root/.local/bin/agy /usr/local/bin/agy || true

# Hook dir; the step-budget hook script is written at runtime by agent.auth().
mkdir -p /opt/agy_hooks

agy --version || true
