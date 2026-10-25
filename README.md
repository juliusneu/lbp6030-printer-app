# lbp6030-printer-app

**Open printer driver for the Canon LBP6030w, with AirPrint and IPP Everywhere support.**

The printer itself supports neither. A Mac on the same network becomes the printer for iPhones, iPads and other
Macs: a small IPP server (part of this repo) receives each job, turns it into the printer's own data format and
sends it to the printer over the network. No Canon driver and no CUPS needed. A command-line tool prints PDFs
directly as well.

Personal project, shared as-is: tested with one LBP6030w (firmware 01.11) on macOS 26. Use it if you have this
printer, or as a starting point for your own.

## Features

- Printing from macOS and iOS (AirPrint), and PDFs, PWG Raster, Apple Raster, JPEG and PNG from the command line.
- Sharp text with real fonts (including bold); grays with a stripe-free halftone that keeps white text on gray
  readable and thin gray lines visible.
- 20 paper sizes, media types (plain, lightweight, heavyweight, cardstock, labels, transparency), collated and
  uncollated copies, toner save, shrink-to-fit, landscape rotation.
- Printer status (paper out, jam, door open, toner level) is passed back to the Mac and iPhone.

## Requirements

- macOS with the Xcode Command Line Tools (`xcode-select --install`): Apple's Python, and `swiftc` for the PDF
  renderer in `quartz/` and the launcher.
- The printer at a fixed IP address (DHCP reservation).
- For the command line: Python 3 with numpy and Pillow (`python3 -m pip install -r requirements.txt`). The printer
  app's installer sets up its own environment instead (`build/venv`, numpy and Pillow from PyPI).

## Usage

```bash
python3 -m lbp6030 status --printer 192.168.1.50
python3 -m lbp6030 print document.pdf --printer 192.168.1.50
```
Options: `--media a5|letter|env10|<PWG media name>`, `--media-type stationery-heavyweight|labels|...`,
`--copies N [--no-collate]`, `--toner-save`, `--no-fit`, `--dry-run out.job`.

### Printer app (AirPrint / IPP Everywhere)

Run it in a terminal, or install it as a background job that starts at login:

```bash
printer-app/lbp6030-printer-app 192.168.1.50 "LBP6030w"     # in a terminal, stop with Ctrl-C
printer-app/install.sh 192.168.1.50 "LBP6030w"              # background job (LaunchAgent)
printer-app/install.sh --uninstall
```
Then add the printer in System Settings > Printers & Scanners; iPhones see it in the Print dialog. The background
job runs inside a small launcher, "LBP6030 Printer App", which `install.sh` builds into `build/`. It logs to
`~/Library/Logs/lbp6030-printer-app.log` and shows up under System Settings > General > Login Items & Extensions.
If it cannot start (for example when its Python environment is missing), it shows a notification and stays
stopped until `install.sh` is run again. With `LBP6030_DRY_RUN_DIR=/some/dir` jobs are written there instead of
printed.

macOS settings:
- **Local Network permission:** macOS asks once whether the printer app may find devices on your local network;
  allow it. If jobs fail with "No route to host", turn it on under System Settings > Privacy & Security > Local
  Network ("LBP6030 Printer App" for the background job, your terminal app when run in a terminal).
- **Keep the Mac awake:** System Settings > Energy, "Prevent automatic sleeping when the display is off" and "Wake
  for network access".

## How it works

```
IPP client (iPhone, Mac) -> server.py: IPP operations, Bonjour (dns-sd), one job at a time
  ipp.py        IPP message format
  render.py     page -> 8-bit gray at 600 dpi (Quartz for PDF, PWG/Apple Raster readers)
  screens.py    black stays solid, grays get a halftone screen and tone curve
  bandcodec.py  band compression
  jobwriter.py  print job
  device.py     check the job, send it to TCP 9100, follow the printer over SNMP
```
The data format is described in [docs/PROTOCOL.md](docs/PROTOCOL.md); the evidence for the compression rules is
in [docs/compression-notes.md](docs/compression-notes.md).

## Limitations

- Only the LBP6030w is tested.
- A5, B5, postcard and envelope sizes and heavyweight paper use a second raster mode that is verified offline only.
- No custom paper sizes.
- If iPhones don't see the printer, check that devices on your Wi-Fi can reach each other (some routers isolate
  them, or don't forward between devices on the same band).

## Tests

```bash
python3 -m unittest discover tests
```
The printer icon is drawn in `printer-app/icon.svg`; after changing it, regenerate the PNGs the server sends:
`swiftc -O quartz/svg2png.swift -o build/svg2png && build/svg2png printer-app/icon.svg lbp6030/icons 512 128 48`

## Legal

Not affiliated with or endorsed by Canon Inc. Canon is a trademark of Canon Inc., used only to name the printer
this works with. The data format was worked out for interoperability from print data observed for the author's own
printer; the band compression was derived from observed data and the public anticapt documentation. This project
contains no Canon code, binaries or firmware. Use at your own risk.

Built with [Claude Code](https://claude.com/claude-code).

License: [MIT](LICENSE)
