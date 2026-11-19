#!/usr/bin/env bash
# Create (or update) the Linux virtualenv for this repo.
# The venv lives OUTSIDE the repo so it never clashes with a Windows venv
# created from the same folder when dual-booting. Override with PM_JOB_HUNT_VENV.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PM_JOB_HUNT_VENV:-$HOME/.venvs/pm-job-hunt}"
PYTHON="${PYTHON:-python3}"

if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "Creating venv at $VENV_DIR"
    mkdir -p "$(dirname "$VENV_DIR")"
    "$PYTHON" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install \
    -r "$REPO_DIR/requirements_hosted.txt" \
    -r "$REPO_DIR/requirements_dev.txt"

echo "Done. Activate with: source \"$VENV_DIR/bin/activate\""
