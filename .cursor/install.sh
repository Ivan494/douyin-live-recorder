#!/usr/bin/env bash
# Idempotent bootstrap for the Douyin Live Recorder dev environment.
#
# The app ships as a Windows portable build, but the code, tests, and the
# tkinter GUI all run on Linux for development. This installs the system
# packages the GUI needs (Tk, an X server for headless display, tray/AppIndicator
# libraries) plus ffmpeg for recording, then creates a virtualenv with the
# Python dependencies.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
  python3-tk \
  python3-venv \
  ffmpeg \
  xvfb \
  gir1.2-appindicator3-0.1

echo "==> Creating virtualenv (.venv)"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi

echo "==> Installing Python dependencies"
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

echo "==> Verifying tkinter is importable"
.venv/bin/python -c "import tkinter; print('tkinter', tkinter.TkVersion, 'OK')"

echo "==> Environment ready. Run tests with: PYTHONPATH=src .venv/bin/python -m pytest src/tests"
