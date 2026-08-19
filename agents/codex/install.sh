#!/bin/bash
# OpenAI Codex CLI agent. Installs Node.js 20 and the Codex CLI tool,
# which provides an autonomous coding agent that runs inside the container.
#
# Sources:
#   codex   https://github.com/openai/codex   (OpenAI, Apache 2.0)
set -e

curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs
npm install -g @openai/codex@0.144.0

# Codex needs this to run without its own sandbox inside Docker
echo 'export CODEX_UNSAFE_ALLOW_NO_SANDBOX=1' >> /etc/profile
echo 'CODEX_UNSAFE_ALLOW_NO_SANDBOX=1' >> /etc/environment
