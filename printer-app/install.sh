#!/bin/zsh
# Install the printer app as a LaunchAgent: it starts at login and logs to ~/Library/Logs/lbp6030-printer-app.log.
# It gets its own Python environment (build/venv: Apple's Python from the Xcode Command Line Tools, with numpy and
# Pillow from PyPI) and runs inside a small "LBP6030 Printer App" launcher (launcher.swift), so macOS shows that name
# under Login Items and Local Network.
#
# usage: printer-app/install.sh PRINTER_IP ["Printer name"]
#        printer-app/install.sh --uninstall
set -e
LABEL=local.lbp6030-printer-app
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/lbp6030-printer-app.log"
DOMAIN="gui/$(id -u)"
DIR="${0:A:h}"
BUILD="${DIR:h}/build"
APP="$BUILD/LBP6030 Printer App.app"
VENV="$BUILD/venv"

if [ "$1" = --uninstall ]; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -rf "$PLIST" "$APP" "$VENV"
    echo "Removed $LABEL"
    exit 0
fi

PRINTER="$1"
NAME="${2:-LBP6030w}"
case "$PRINTER" in
    ""|-*) echo "usage: $0 PRINTER_IP [\"Printer name\"]   or   $0 --uninstall"; exit 1 ;;
esac
xcode-select -p >/dev/null 2>&1 || { echo "needs the Xcode Command Line Tools: xcode-select --install"; exit 1; }
xml() { local v="${1//&/&amp;}"; v="${v//</&lt;}"; print -r -- "${v//>/&gt;}"; }   # values inside the plists

# Python environment independent of Homebrew: Apple's Python (or $PYTHON) with numpy and Pillow.
if ! "$VENV/bin/python3" -c "import numpy, PIL" 2>/dev/null; then
    echo "Setting up the Python environment in $VENV (downloads numpy and Pillow) ..."
    rm -rf "$VENV"
    "${PYTHON:-/usr/bin/python3}" -m venv "$VENV"
    "$VENV/bin/python3" -m pip install --quiet --upgrade pip
    "$VENV/bin/python3" -m pip install --quiet numpy pillow
fi
"$VENV/bin/python3" -c "import numpy, PIL"

# The launcher is only rebuilt when launcher.swift changes: a rebuilt app is a new app to macOS, which would ask for
# the Local Network permission again.
if [ ! -x "$APP/Contents/MacOS/lbp6030-printer-app" ] || [ "$DIR/launcher.swift" -nt "$APP/Contents/MacOS/lbp6030-printer-app" ]; then
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
fi

# KeepAlive restarts the job only after a failure: a clean exit (e.g. missing Python packages, reported with a
# notification) stays stopped instead of looping.
mkdir -p "${PLIST:h}" "${LOG:h}"
cat > "$PLIST.new" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml "$APP/Contents/MacOS/lbp6030-printer-app")</string>
    <string>$(xml "$DIR/lbp6030-printer-app")</string>
    <string>$(xml "$PRINTER")</string>
    <string>$(xml "$NAME")</string>
  </array>
  <key>AssociatedBundleIdentifiers</key><string>$LABEL</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(xml "$VENV/bin:/usr/bin:/bin:/usr/sbin:/sbin")</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$(xml "$LOG")</string>
  <key>StandardErrorPath</key><string>$(xml "$LOG")</string>
</dict>
</plist>
EOF
plutil -lint -s "$PLIST.new"          # checked before the running job is stopped
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
mv "$PLIST.new" "$PLIST"
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "Installed $LABEL: '$NAME' -> $PRINTER. Log: $LOG"
echo "Remove with: $0 --uninstall"
