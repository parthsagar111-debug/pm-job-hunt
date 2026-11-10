#!/usr/bin/env bash
# Run the offline test suite with the Linux venv (see setup_venv.sh).
# Extra arguments are passed to pytest, e.g. ./run_tests.sh -k sponsor
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PM_JOB_HUNT_VENV:-$HOME/.venvs/pm-job-hunt}"

if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "No venv at $VENV_DIR. Run ./setup_venv.sh first." >&2
    exit 1
fi

cd "$REPO_DIR"
exec "$VENV_DIR/bin/python" -m pytest tests -q "$@"
