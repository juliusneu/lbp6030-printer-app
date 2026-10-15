#!/bin/zsh
# Install the printer app as a LaunchAgent: it starts at login, restarts if it stops, and logs to
# ~/Library/Logs/lbp6030-printer-app.log. It runs inside a small "LBP6030 Printer App" launcher (built here from
# launcher.swift), so macOS shows that name under Login Items and Local Network.
#
# usage: printer-app/install.sh PRINTER_IP ["Printer name"]
#        printer-app/install.sh --uninstall
set -e
LABEL=local.lbp6030-printer-app
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/lbp6030-printer-app.log"
DOMAIN="gui/$(id -u)"
DIR="${0:A:h}"
APP="${DIR:h}/build/LBP6030 Printer App.app"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if [ "$1" = --uninstall ]; then
    rm -rf "$PLIST" "$APP"
    echo "Removed $LABEL"
    exit 0
fi

PRINTER="$1"
NAME="${2:-LBP6030w}"
[ -n "$PRINTER" ] || { echo "usage: $0 PRINTER_IP [\"Printer name\"]   or   $0 --uninstall"; exit 1; }
# launchd starts with a minimal PATH; keep the python3 that has numpy and Pillow installed.
PY="$(command -v python3)"
"$PY" -c "import numpy, PIL" 2>/dev/null || { echo "needs Python 3 with numpy and Pillow: python3 -m pip install numpy pillow"; exit 1; }

mkdir -p "$APP/Contents/MacOS"
swiftc -O "$DIR/launcher.swift" -o "$APP/Contents/MacOS/lbp6030-printer-app"
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>$LABEL</string>
  <key>CFBundleName</key><string>LBP6030 Printer App</string>
  <key>CFBundleExecutable</key><string>lbp6030-printer-app</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSBackgroundOnly</key><true/>
  <key>NSLocalNetworkUsageDescription</key><string>Sends print jobs to the printer on your network.</string>
</dict>
</plist>
EOF
codesign --force --sign - "$APP"

mkdir -p "${PLIST:h}" "${LOG:h}"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP/Contents/MacOS/lbp6030-printer-app</string>
    <string>$DIR/lbp6030-printer-app</string>
    <string>$PRINTER</string>
    <string>$NAME</string>
  </array>
  <key>AssociatedBundleIdentifiers</key><string>$LABEL</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>${PY:h}:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
plutil -lint -s "$PLIST"
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Installed $LABEL: '$NAME' -> $PRINTER. Log: $LOG"
echo "Remove with: $0 --uninstall"
