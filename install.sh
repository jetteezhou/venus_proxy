#!/bin/sh
# Install (or reinstall) venus_proxy.py as a login-time LaunchAgent that restarts when it exits. See README.md.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL=com.venus.proxy
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
# bootout returns before the job is gone; bootstrapping too early fails with "5: Input/output error"
for _ in 1 2 3 4 5 6 7 8 9 10; do launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || break; sleep 1; done
sed "s#DIR#$DIR#g" "$DIR/$LABEL.plist" > "$PLIST"
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "已安装 $LABEL：http://127.0.0.1:${VENUS_PROXY_PORT:-18901}（日志 $DIR/proxy.log）"
