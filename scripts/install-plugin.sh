#!/bin/sh
# Installs plugin/rbxforge.lua into the Roblox Studio local plugins directory.
#
# IMPORTANT: Studio skips symlinks in the plugins directory, so this copies a
# real file. After installing, restart Roblox Studio (or use
# PluginDebugService -> "Save and Reload All Plugins in Debugger").
#
# Usage:
#   scripts/install-plugin.sh [PLUGINS_DIR]
#   PLUGINS_DIR=/path/to/plugins scripts/install-plugin.sh
#
# If PLUGINS_DIR is not given, the default Studio plugins directory for the OS
# is used. If you changed Studio's "Plugins Dir" (File > Studio Settings >
# Studio > Directories), pass that directory explicitly.

set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$SCRIPT_DIR/../plugin/rbxforge.lua"

if [ -n "${1:-}" ]; then
	DEST_DIR="$1"
elif [ -n "${PLUGINS_DIR:-}" ]; then
	DEST_DIR="$PLUGINS_DIR"
elif [ "$(uname)" = "Darwin" ]; then
	DEST_DIR="$HOME/Library/Application Support/Roblox/Plugins"
else
	DEST_DIR="${LOCALAPPDATA:-$HOME/AppData/Local}/Roblox/Plugins"
fi

mkdir -p "$DEST_DIR"
cp "$SRC" "$DEST_DIR/rbxforge.lua"

echo "Installed plugin -> $DEST_DIR/rbxforge.lua"
echo "Next: restart Roblox Studio (or reload plugins via PluginDebugService)."
