"""Printer I/O: job validation, one-way delivery on TCP 9100, read-only SNMP status.

The printer never answers on port 9100; state, error bits, page counter and toner level come from SNMP. With an
empty tray it holds the job and prints it after paper is loaded and the Paper key is pressed."""
import socket
import struct
import subprocess
import time

from . import decode, jobparser

SNMPGET = "/usr/bin/snmpget"
OID_STATUS = "1.3.6.1.2.1.25.3.5.1.1.1"     # hrPrinterStatus
OID_ERRORS = "1.3.6.1.2.1.25.3.5.1.2.1"     # hrPrinterDetectedErrorState (bit string)
OID_PAGES = "1.3.6.1.2.1.43.10.2.1.4.1.1"   # prtMarkerLifeCount
OID_TONER_NAME = "1.3.6.1.2.1.43.11.1.1.6.1.1"
OID_TONER_MAX = "1.3.6.1.2.1.43.11.1.1.8.1.1"
OID_TONER_LEVEL = "1.3.6.1.2.1.43.11.1.1.9.1.1"
ERRBITS = ["lowPaper", "noPaper", "lowToner", "noToner", "doorOpen", "jammed", "offline", "serviceRequested"]
IPP_REASON = {
    "noPaper": "media-empty-error", "lowPaper": "media-low-warning", "noToner": "toner-empty-error",
    "lowToner": "toner-low-warning", "doorOpen": "door-open-error", "jammed": "media-jam-error",
    "offline": "offline-report", "serviceRequested": "other-error",
}


def snmp_get(host, oids, timeout=2):
    try:
        r = subprocess.run([SNMPGET, "-v1", "-c", "public", "-t", str(timeout), "-r", "1", "-Oqvt", host] + oids,
                           capture_output=True, text=True, timeout=timeout * 3 + 5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    vals = [v.strip() for v in r.stdout.strip().splitlines()]
    return vals if r.returncode == 0 and len(vals) == len(oids) else None


def _error_bits(v):
    v = v.replace('"', "").replace(" ", "")
    if v.startswith("@"):            # net-snmp prints the byte 0x40 as the character '@'
        return 0x40
    try:
        return int(v[:2], 16) if v else 0
    except ValueError:
        return 0


def status(host):
    """read-only printer status: dict(reachable, state, errors, reasons, pages, toner_level, toner_max, toner_name)"""
    v = snmp_get(host, [OID_STATUS, OID_ERRORS, OID_PAGES, OID_TONER_NAME, OID_TONER_MAX, OID_TONER_LEVEL])
    if v is None:
        return dict(reachable=False, state="unreachable", errors=[], reasons=["offline-report"], pages=None,
                    toner_level=None, toner_max=None, toner_name=None)
    bits = _error_bits(v[1])
    errors = [ERRBITS[i] for i in range(8) if bits & (0x80 >> i)]
    state = {"1": "other", "3": "idle", "4": "printing", "5": "warmup"}.get(v[0], v[0])
    num = lambda s: int(s) if s.lstrip("-").isdigit() else None  # noqa: E731
    return dict(reachable=True, state=state, errors=errors, reasons=[IPP_REASON[e] for e in errors],
                pages=num(v[2]), toner_name=v[3].strip('"'), toner_max=num(v[4]), toner_level=num(v[5]))


def split_jobs(buf):
    """a buffer may hold several identical jobs back to back (collated copies)"""
    starts = [p["off"] for p in jobparser.packets(buf) if p["op"] == 0x6B]
    return [buf[x:y] for x, y in zip(starts, starts[1:] + [len(buf)])]


def validate(buf, max_sheets=None):
    """structural checks + full decode of every band with our own decoder.
    Returns (problems, info); info["sheets"] = pages x copy count x collated jobs."""
    problems = []
    pk_all = list(jobparser.packets(buf))
    if not pk_all or pk_all[0]["op"] != 0x6B:
        problems.append("first packet is not a job header (0x006B)")
    jobs = split_jobs(buf)
    if any(j != jobs[0] for j in jobs[1:]):
        problems.append("concatenated jobs differ (only identical collated copies are expected)")
    job = jobs[0]
    pk = list(jobparser.packets(job))
    size_fields = [p for p in pk if p["op"] == 0x12 and p["payload"][:2] == b"\x07\xd6"]
    if len(size_fields) != 1 or struct.unpack(">I", size_fields[0]["payload"][2:6])[0] != len(job):
        problems.append("job size field (07d6) missing or != job size")
    copies = [struct.unpack(">H", p["payload"][2:4])[0] for p in pk
              if p["op"] == 0x15 and p["payload"][:2] == b"\x07\xd7"]
    copies = copies[0] if copies else 1
    counts = {struct.unpack(">I", p["payload"][2:6])[0] for p in pk
              if p["op"] in (0x12, 0x15) and p["payload"][:2] == b"\x01\x13"}
    try:
        pages = decode.decode_job_bytes(job)
    except Exception as e:  # noqa: BLE001
        return problems + [f"band decode failed: {e}"], dict(sheets=0, pages=0)
    if counts != {len(pages) * copies}:
        problems.append(f"page count fields {counts} != pages {len(pages)} x copies {copies}")
    sheets = len(pages) * copies * len(jobs)
    if max_sheets is not None and sheets > max_sheets:
        problems.append(f"{sheets} sheets > limit {max_sheets}")
    return problems, dict(sheets=sheets, pages=len(pages), copies=copies * len(jobs), collated=len(jobs) > 1,
                          sizes=[(p["w"], p["h"], p["bpp"]) for p in pages])


def send(buf, host, port=9100, timeout=30):
    """one-way delivery; returns whatever the printer sent back (normally nothing)"""
    s = socket.create_connection((host, port), timeout=timeout)
    try:
        s.sendall(buf)
        s.shutdown(socket.SHUT_WR)
        s.settimeout(3)
        rx = b""
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                rx += chunk
        except socket.timeout:
            pass
        return rx
    finally:
        s.close()


def wait_printed(host, pages_before, sheets, report=lambda st: None, timeout=900, poll=2.0):
    """poll until the page counter rose by `sheets` (True) or `timeout` seconds passed (False).
    `report(status)` is called on every poll, e.g. to forward paper-out to IPP clients."""
    end = time.time() + timeout
    while time.time() < end:
        st = status(host)
        report(st)
        if st["pages"] is not None and pages_before is not None and st["pages"] >= pages_before + sheets:
            return True
        time.sleep(poll)
    return False
