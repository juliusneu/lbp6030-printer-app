#!/bin/zsh
# Start the LBP6030w Printer Application on this Mac: an IPP Everywhere / AirPrint printer (macOS's own
# ippeveprinter) that Macs and iPhones on the same network find by themselves. Stop it with Ctrl-C.
#
# usage: printer-app/run.sh PRINTER_IP ["Printer name"]        (or set LBP6030_HOST)
# env:   LBP6030_PORT (default 8631)  LBP6030_DRY_RUN_DIR (write jobs there instead of printing)  VERBOSE=1
set -e
DIR="${0:A:h}"
PRINTER="${1:-$LBP6030_HOST}"
[ -n "$PRINTER" ] || { echo "usage: $0 PRINTER_IP [\"Printer name\"]  (or set LBP6030_HOST)"; exit 1; }
NAME="${2:-LBP6030w}"
PORT="${LBP6030_PORT:-8631}"
LOCAL_NAME="$(scutil --get LocalHostName).local"   # what Bonjour clients can resolve (not a VPN/Tailscale name)
SPOOL="${TMPDIR:-/tmp}/lbp6030-printer-app"
mkdir -p "$SPOOL"
python3 -c "import numpy, PIL" 2>/dev/null || { echo "needs Python 3 with numpy and Pillow: python3 -m pip install numpy pillow"; exit 1; }
echo "Printer Application '$NAME' -> socket://$PRINTER:9100, IPP on port $PORT, advertised via Bonjour (AirPrint)"
exec ippeveprinter ${VERBOSE:+-v} -n "$LOCAL_NAME" -p "$PORT" -d "$SPOOL" -r _print,_universal \
    -a "$DIR/lbp6030.conf" -c "$DIR/lbp6030-command" -D "socket://$PRINTER:9100" "$NAME"
