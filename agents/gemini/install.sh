#!/bin/bash
# Google Gemini CLI agent. Installs Node.js 20 and the Gemini CLI tool,
# which provides an autonomous coding agent that runs inside the container.
#
# Sources:
#   gemini   https://github.com/google-gemini/gemini-cli   (Google, Apache 2.0)
set -e

curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs
npm install -g @google/gemini-cli
