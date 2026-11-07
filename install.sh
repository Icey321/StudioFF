#!/usr/bin/env bash
# Creates the UI environment (.venv) and an app launcher entry, the Linux and macOS counterpart of install.ps1.
# FaceFusion v4 itself lives in its own environment and is installed separately; pick it on the app's FaceFusion page.
# Based on tokenwizard's Linux fork, https://github.com/tokenwizard/StudioFF
set -euo pipefail
cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")"

if command -v uv >/dev/null 2>&1; then
	uv venv --python 3.12 .venv
	uv pip install --python .venv/bin/python -r requirements.txt
else
	python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -r requirements.txt
fi

if [ "$(uname)" = "Darwin" ]; then
	echo 'Done. Start StudioFF with ./StudioFF.sh'
	exit 0
fi

# the file name matches setDesktopFileName in __main__.py, so the window gets this entry's icon
data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
mkdir -p "$data_home/applications" "$data_home/icons/hicolor/256x256/apps"
cp ffstudio/assets/icon.png "$data_home/icons/hicolor/256x256/apps/studioff.png"
cat > "$data_home/applications/studioff.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=StudioFF
Comment=Face swapping with FaceFusion v4
Exec="$PWD/StudioFF.sh"
Path=$PWD
Icon=studioff
Terminal=false
Categories=Graphics;
StartupWMClass=studioff
EOF
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$data_home/applications" || true
echo 'Done. Start "StudioFF" from your app launcher, or run ./StudioFF.sh'
