"""Command line: python3 -m lbp6030 {print,status,validate,serve} ...

  print FILE      render a PDF / JPEG / PNG / PWG Raster / URF file and print it (or --dry-run to a job file)
  status          read-only printer status over SNMP
  validate JOB    check that a job file is complete and decodes cleanly
  serve PRINTER   run the AirPrint / IPP Everywhere printer (printer-app/ starts this)
The printer address comes from --printer or the LBP6030_HOST environment variable.
"""
import argparse
import os
import sys

from . import device, jobwriter, media, printjob


def paper_arg(name):
    key = media.paper_from_ipp(name, default=None)
    if key is None:
        raise argparse.ArgumentTypeError(f"unknown paper {name!r}; known: {', '.join(sorted(jobwriter.PAPER))}")
    return key


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m lbp6030", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("print")
    p.add_argument("file")
    p.add_argument("--printer", default=os.environ.get("LBP6030_HOST"))
    p.add_argument("--port", type=int, default=9100)
    p.add_argument("--media", default=media.DEFAULT_PAPER, type=paper_arg,
                   help="PWG media name or a4, letter, legal, a5, b5, executive, env10, dl, c5, monarch")
    p.add_argument("--media-type", default=media.DEFAULT_TYPE, choices=sorted(jobwriter.MEDIA_TYPES))
    p.add_argument("--copies", type=int, default=1)
    p.add_argument("--no-collate", action="store_true")
    p.add_argument("--toner-save", action="store_true")
    p.add_argument("--no-fit", action="store_true", help="crop pages larger than the paper instead of shrinking")
    p.add_argument("--content-type", default="application/pdf")
    p.add_argument("--dry-run", metavar="JOB_FILE", help="write the job to this file instead of printing")
    p.add_argument("--max-sheets", type=int, default=10, help="refuse jobs using more sheets (default 10)")
    s = sub.add_parser("status")
    s.add_argument("--printer", default=os.environ.get("LBP6030_HOST"))
    v = sub.add_parser("validate")
    v.add_argument("job")
    sv = sub.add_parser("serve")
    sv.add_argument("printer", nargs="?", default=os.environ.get("LBP6030_HOST"))
    sv.add_argument("--name", default="LBP6030w", help="printer name shown to clients")
    sv.add_argument("--port", type=int, default=8631)
    sv.add_argument("--dry-run", metavar="DIR", help="write jobs to DIR instead of printing")
    sv.add_argument("--no-bonjour", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        if not a.printer:
            ap.error("give the printer address or set LBP6030_HOST")
        from .server import serve
        return serve(a.printer, a.name, a.port, a.dry_run, bonjour=not a.no_bonjour)
    if a.cmd == "validate":
        problems, info = device.validate(open(a.job, "rb").read())
        print(info)
        print("OK" if not problems else "PROBLEMS: " + "; ".join(problems))
        return 0 if not problems else 1
    if a.cmd == "status":
        if not a.printer:
            ap.error("give --printer or set LBP6030_HOST")
        print(device.status(a.printer))
        return 0
    job, n = printjob.make_job(a.file, a.content_type, a.media, a.media_type, a.copies, not a.no_collate,
                               a.toner_save, job_name=os.path.basename(a.file), user=os.environ.get("USER", "user"),
                               fit=not a.no_fit)
    problems, info = device.validate(job, a.max_sheets)
    print(f"job: {len(job)} bytes, {info['pages']} page(s), {info['sheets']} sheet(s), {info.get('sizes')}")
    if problems:
        print("REFUSING: " + "; ".join(problems), file=sys.stderr)
        return 1
    if a.dry_run:
        open(a.dry_run, "wb").write(job)
        print(f"written to {a.dry_run} (not printed)")
        return 0
    if not a.printer:
        ap.error("give --printer or set LBP6030_HOST (or use --dry-run)")
    before = device.status(a.printer)
    if not before["reachable"]:
        print("printer not reachable (off? asleep with Wi-Fi down?)", file=sys.stderr)
        return 1
    device.send(job, a.printer, a.port)
    print("sent; waiting for the pages ...")
    last = []

    def report(st):
        if st["reasons"] != last:
            print("printer:", ", ".join(st["reasons"]) or "ok")
            last[:] = st["reasons"]
    ok = device.wait_printed(a.printer, before["pages"], info["sheets"], report)
    print("done" if ok else "timed out waiting for the pages")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
