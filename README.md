# lbp6030-printer-app

**Open printer driver for the Canon LBP6030w, with AirPrint and IPP Everywhere support.**

The printer itself supports neither. A Mac on the same network becomes the printer for iPhones, iPads and other
Macs: it runs macOS's own `ippeveprinter`, turns each job into the printer's own data format and sends it to the
printer over the network. No Canon driver needed. A command-line tool prints PDFs directly as well.

Personal project, shared as-is: tested with one LBP6030w (firmware 01.11) on macOS 26. Use it if you have this
printer, or as a starting point for your own.

## Features

- Printing from macOS and iOS (AirPrint), and PDFs, PWG Raster, Apple Raster, JPEG and PNG from the command line.
- Sharp text with real fonts (including bold); grays with a stripe-free halftone that keeps white text on gray
  readable and thin gray lines visible.
- 20 paper sizes, media types (plain, lightweight, heavyweight, cardstock, labels, transparency), collated and
  uncollated copies, toner save, shrink-to-fit, landscape rotation.
- Printer status (paper out, jam, door open, toner level) is passed back while a job runs.

## Requirements

- macOS with Xcode Command Line Tools (`swiftc`; the PDF renderer in `quartz/` is compiled on first use).
- Python 3 with numpy and Pillow: `python3 -m pip install -r requirements.txt`
- The printer at a fixed IP address (DHCP reservation).

## Usage

```bash
python3 -m lbp6030 status --printer 192.168.1.50
python3 -m lbp6030 print document.pdf --printer 192.168.1.50
```
Options: `--media a5|letter|env10|<PWG media name>`, `--media-type stationery-heavyweight|labels|...`,
`--copies N [--no-collate]`, `--toner-save`, `--no-fit`, `--dry-run out.job`.

Printer app for Macs and iPhones on the same network (the Mac has to stay awake):

```bash
printer-app/run.sh 192.168.1.50 "LBP6030w"
```
Then add the printer in System Settings > Printers & Scanners; iPhones see it in the Print dialog.
With `LBP6030_DRY_RUN_DIR=/some/dir` jobs are written there instead of printed.

## How it works

```
IPP client -> ippeveprinter -> printer-app/lbp6030-command
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
- No custom paper sizes. Status updates only while a job runs.

## Tests

```bash
python3 -m unittest discover tests
```

## Legal

Not affiliated with or endorsed by Canon Inc. Canon is a trademark of Canon Inc., used only to name the printer
this works with. The data format was worked out for interoperability from print data observed for the author's own
printer; the band compression was derived from observed data and the public anticapt documentation. This project
contains no Canon code, binaries or firmware. Use at your own risk.

Built with [Claude Code](https://claude.com/claude-code).

License: [MIT](LICENSE)
