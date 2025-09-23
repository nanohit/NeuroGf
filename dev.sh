#!/usr/bin/env bash
set -euo pipefail

# Always operate from the repo root (the directory of this script)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/venv310"
ACTIVATE_FILE="$VENV_DIR/bin/activate"

usage() {
  echo "Usage: ./dev.sh [run|install|shell|clean]"
  echo
  echo "Commands:"
  echo "  install  Create venv if needed and install requirements"
  echo "  run      Activate venv and run bot (default)"
  echo "  shell    Drop into an activated shell"
  echo "  clean    Remove the virtual environment"
}

ensure_venv() {
  if [ ! -d "$VENV_DIR" ]; then
    echo "[dev.sh] Creating virtual environment at $VENV_DIR"
    if command -v python3.10 >/dev/null 2>&1; then
      python3.10 -m venv "$VENV_DIR"
    else
      python3 -m venv "$VENV_DIR"
    fi
  fi
}

install_requirements() {
  # shellcheck disable=SC1090
  source "$ACTIVATE_FILE"
  python -m pip install --upgrade pip
  if [ -f requirements.txt ]; then
    pip install -r requirements.txt
  else
    echo "[dev.sh] requirements.txt not found; skipping"
  fi
}

cmd="${1:-run}"
case "$cmd" in
  install)
    ensure_venv
    install_requirements
    ;;
  run)
    ensure_venv
    # shellcheck disable=SC1090
    source "$ACTIVATE_FILE"
    exec python bot.py
    ;;
  shell)
    ensure_venv
    # shellcheck disable=SC1090
    source "$ACTIVATE_FILE"
    exec "$SHELL"
    ;;
  clean)
    rm -rf "$VENV_DIR"
    echo "[dev.sh] Removed $VENV_DIR"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    echo "Unknown command: $cmd" >&2
    usage
    exit 1
    ;;
esac


