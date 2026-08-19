#!/bin/bash
# PyGhidra - Python library providing direct access to Ghidra's API via JPype.
# Enables headless binary analysis from native CPython scripts without the Ghidra GUI.
# The agent uses pyghidra by writing Python scripts and running them via bash.
#
# Sources:
#   Ghidra     https://github.com/NationalSecurityAgency/ghidra  (NSA, Apache 2.0)
#   pyghidra   https://pypi.org/project/pyghidra/                (NSA, Apache 2.0)
set -e

# Install Ghidra 12.0.2 if not already present (may be shared with ghidra tool)
if [ ! -d /opt/ghidra ]; then
    curl -fsSL -o /tmp/ghidra.zip \
        https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_12.0.2_build/ghidra_12.0.2_PUBLIC_20260129.zip
    unzip -q /tmp/ghidra.zip -d /opt
    mv /opt/ghidra_12.0.2_PUBLIC /opt/ghidra
    rm /tmp/ghidra.zip
fi

# Set GHIDRA_INSTALL_DIR so pyghidra can find the installation
echo 'export GHIDRA_INSTALL_DIR=/opt/ghidra' >> /etc/environment
echo 'export GHIDRA_INSTALL_DIR=/opt/ghidra' >> /root/.bashrc
