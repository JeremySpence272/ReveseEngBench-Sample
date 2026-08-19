#!/bin/bash
# Claude Code CLI agent. Installs Node.js 20 and the Claude Code CLI tool,
# which provides an autonomous coding agent that runs inside the container.
#
# Sources:
#   claude   https://github.com/anthropics/claude-code   (Anthropic, Anthropic Commercial ToS)
set -e

curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs
npm install -g @anthropic-ai/claude-code@2.1.197

mkdir -p /workspace
