#!/bin/sh
# Stop venus_proxy.py and remove its LaunchAgent. Point clients back at Venus first (README.md: 卸载).
LABEL=com.venus.proxy
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "已停止并移除 $LABEL"
