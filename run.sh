#!/usr/bin/env bash
# Personal Finance — one-command launcher for macOS and Linux.
#
#   ./run.sh            create the virtual environment if needed, then launch
#   ./run.sh --test     run the test suite instead of launching
#   ./run.sh --update   reinstall dependencies, then launch
#
# Nothing here touches the network except pip, and only to install the
# libraries listed in requirements.txt.

set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
STAMP="$VENV/.requirements-installed"

find_python() {
  for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        echo "$candidate"; return 0
      fi
    fi
  done
  return 1
}

if [ ! -d "$VENV" ]; then
  PY="$(find_python)" || {
    echo "Python 3.10 or newer is required but was not found." >&2
    echo "Install it from https://www.python.org/downloads/ and run this script again." >&2
    exit 1
  }
  echo "Creating a virtual environment in $VENV using $PY ..."
  "$PY" -m venv "$VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

if [ "${1:-}" = "--update" ] || [ ! -f "$STAMP" ] || [ requirements.txt -nt "$STAMP" ]; then
  echo "Installing dependencies ..."
  python -m pip install --upgrade pip >/dev/null
  python -m pip install -r requirements.txt
  touch "$STAMP"
fi

if [ "${1:-}" = "--test" ]; then
  exec python -m pytest
fi

echo
echo "Starting Personal Finance. It will open in your browser at http://localhost:8501"
echo "Your data stays in the data/ folder next to this script. Press Ctrl+C to stop."
echo
exec python -m streamlit run app.py
