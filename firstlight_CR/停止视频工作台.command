#!/bin/zsh
set -eu
cd "$(dirname "$0")"
PYTHON="${CR_RENDER_PYTHON:-/opt/anaconda3/bin/python}"
"$PYTHON" -m render_web stop
