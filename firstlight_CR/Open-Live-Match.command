#!/bin/zsh
cd "$(dirname "$0")" || exit 1
exec ".venv-live/bin/python" -m live_web --open
