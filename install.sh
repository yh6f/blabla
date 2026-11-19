#!/usr/bin/env bash
# Installs the requirements for the guns.lol username checker.
cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "Python 3.8+ is required: https://www.python.org/downloads/"
    exit 1
fi

echo "Installing requirements..."
if "$PY" -m pip install -r requirements.txt; then
    echo "Installation complete! Run: $PY gunslol.py"
    exit 0
fi

# Debian/Ubuntu/Homebrew Python refuse system-wide pip installs (PEP 668) -> use a virtualenv.
echo "pip refused to install system-wide. Creating a virtual environment in .venv ..."
if "$PY" -m venv .venv && .venv/bin/python -m pip install -r requirements.txt; then
    echo "Installation complete! Run: .venv/bin/python gunslol.py"
else
    echo "Installation failed. On Debian/Ubuntu you may need: sudo apt install python3-venv"
    exit 1
fi
