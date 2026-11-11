#!/bin/sh
set -eu
python scripts/check_runtime.py
exec "$@"
