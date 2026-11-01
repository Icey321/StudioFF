#!/usr/bin/env bash
cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")"
if [ ! -x .venv/bin/python ]; then
	echo 'The UI environment is missing. Run ./install.sh first.'
	exit 1
fi
exec .venv/bin/python -m ffstudio "$@"
