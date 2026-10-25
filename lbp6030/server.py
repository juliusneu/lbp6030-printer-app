"""AirPrint / IPP Everywhere printer: a small IPP server with the operations iOS and macOS use, advertised with
Bonjour, printing through printjob and device. Jobs run one at a time in a worker thread."""
import html
import os
import queue
import re
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import device, ipp, jobwriter, media, printjob

FORMATS = ["application/octet-stream", "image/jpeg", "image/pwg-raster", "image/urf"]
URF = "V1.4,W8,RS600,CP1,PQ3-4-5,MT1-2-3-4-5-6"
MARGIN = 500                              # 5 mm, in 1/100 mm
MAX_COPIES, MAX_SHEETS = 99, 100
MAX_REQUEST = 1 << 30                     # request bodies and decompressed documents
JOB_TIMEOUT = 60                          # multiple-operation-time-out: a created job must get its document by then
COMPRESSIONS = ("none", "gzip", "deflate")
OPERATIONS = {0x0002: "Print-Job", 0x0004: "Validate-Job", 0x0005: "Create-Job", 0x0006: "Send-Document",
              0x0008: "Cancel-Job", 0x0009: "Get-Job-Attributes", 0x000A: "Get-Jobs",
              0x000B: "Get-Printer-Attributes", 0x0039: "Cancel-My-Jobs", 0x003B: "Close-Job",
              0x003C: "Identify-Printer"}
PENDING, PROCESSING, CANCELED, ABORTED, COMPLETED = 3, 5, 7, 8, 9
JOB_STATES = {PENDING: "pending", PROCESSING: "processing", CANCELED: "canceled", ABORTED: "aborted",
              COMPLETED: "completed"}
ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
ICONS = {"icon-sm.png": "icon-48.png", "icon.png": "icon-128.png", "icon-lg.png": "icon-512.png"}   # from icon.svg
# the 120x235 mm "yougata-naga3" envelope has the same size as "chou3"; offering both confuses clients
MEDIA = [m for m in jobwriter.PAPER if m != "om_yougata-naga3_120x235mm"]


def ipp_time(t):
    """RFC 2579 dateTime in UTC"""
    g = time.gmtime(t)
    return struct.pack(">HBBBBBBcBB", g.tm_year, g.tm_mon, g.tm_mday, g.tm_hour, g.tm_min, g.tm_sec, 0, b"+", 0, 0)


def bonjour_host():
    """the name Bonjour clients resolve (<LocalHostName>.local), not the system hostname, which some network
    software changes"""
    try:
        name = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = ""
    return (name or socket.gethostname().split(".")[0]) + ".local"


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, file=sys.stderr, flush=True)


def media_size(name):
    """PWG media name -> (width, height) in 1/100 mm"""
    w, h, unit = re.search(r"_([\d.]+)x([\d.]+)(mm|in)$", name).groups()
    f = 100 if unit == "mm" else 2540
    return int(float(w) * f), int(float(h) * f)


def media_col(name, source=None, mtype=None):
    w, h = media_size(name)
    col = {"media-size": (ipp.BEGIN_COLLECTION, [{"x-dimension": (ipp.INTEGER, [w]),
                                                  "y-dimension": (ipp.INTEGER, [h])}])}
    for side in ("bottom", "left", "right", "top"):
        col[f"media-{side}-margin"] = (ipp.INTEGER, [MARGIN])
    if source:
        col["media-source"] = (ipp.KEYWORD, [source])
    if mtype:
        col["media-type"] = (ipp.KEYWORD, [mtype])
    return col


def paper_for_size(x, y):
    for name in MEDIA:
        w, h = media_size(name)
        if (abs(w - x) < 150 and abs(h - y) < 150) or (abs(w - y) < 150 and abs(h - x) < 150):
            return name
    return None


class Job:
    def __init__(self, job_id, request):
        self.id = job_id
        self.name = jobwriter.short_name(str(request.get("job-name") or f"Job {job_id}"), 255)
        self.user = jobwriter.short_name(str(request.get("requesting-user-name") or "anonymous"), 255)
        self.attrs = request.group(ipp.JOB)
        self.format = request.get("document-format", "application/octet-stream")
        self.state, self.reasons, self.message = PENDING, ["job-incoming"], ""
        self.created, self.processing, self.completed = time.time(), None, None
        self.impressions = 0
        self.document, self.queued = None, False

    def value(self, name, default=None):
        tag_values = self.attrs.get(name)
        return tag_values[1][0] if tag_values and tag_values[1] else default


class Printer:
    def __init__(self, host, name="LBP6030w", port=8631, dry_run=None, spool=None):
        self.host, self.name, self.port, self.dry_run = host, name, port, dry_run
        self.spool = spool or tempfile.mkdtemp(prefix="lbp6030-")
        self.uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"lbp6030-printer-app:{socket.gethostname()}:{name}"))
        # links clients open themselves (web page, icons): by Bonjour name, since the address a client connected to
        # may be an IPv6 link-local one it cannot use in a link
        self.web = f"http://{bonjour_host()}:{port}"
        self.started = time.time()
        self.jobs, self.next_id, self.lock = {}, 1, threading.Lock()
        self.queue = queue.Queue()
        self.status = {"reachable": True, "reasons": [], "toner_level": None, "toner_name": None}
        self.current = None
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._poll_status, daemon=True).start()

    # --- IPP ------------------------------------------------------------------------------------------------------

    def handle(self, body, host):
        try:
            req = ipp.Request(body)
        except ValueError as e:
            log(f"bad IPP request: {e}")
            return ipp.response(0, ipp.BAD_REQUEST, [], message=str(e))
        reply = lambda status, groups=(), message=None: ipp.response(  # noqa: E731
            req.request_id, status, groups, version=req.version if req.version[0] in (1, 2) else (2, 0),
            message=message)
        if req.version[0] not in (1, 2):
            return reply(ipp.VERSION_NOT_SUPPORTED)
        op_attrs = req.groups[0][1] if req.groups and req.groups[0][0] == ipp.OPERATION else {}
        names = list(op_attrs)
        if (req.request_id == 0 or names[:2] != ["attributes-charset", "attributes-natural-language"]
                or not ({"printer-uri", "job-uri"} & set(names))):
            return reply(ipp.BAD_REQUEST, message="Missing required attributes.")
        op = OPERATIONS.get(req.operation)
        if op not in ("Get-Printer-Attributes", "Get-Job-Attributes", "Get-Jobs"):
            log(f"{op or hex(req.operation)} from {req.get('requesting-user-name', '?')}")
        try:
            return self._operation(req, op, host or f"localhost:{self.port}", reply)
        except Exception as e:  # noqa: BLE001
            log(f"{op}: {e!r}")
            return reply(ipp.INTERNAL_ERROR, message=f"Internal error: {e}")

    def _operation(self, req, op, uri_host, reply):
        if op == "Get-Printer-Attributes":
            wanted = set(req.values("requested-attributes")) or {"all"}
            attrs = [a for a in self.printer_attributes(uri_host)
                     if wanted & {"all", "printer-description", "job-template", a[1]}]
            return reply(ipp.OK, [(ipp.PRINTER, attrs)])
        if op in ("Validate-Job", "Create-Job", "Print-Job"):
            fmt = req.get("document-format")
            if fmt and fmt not in FORMATS:
                return reply(ipp.FORMAT_NOT_SUPPORTED, [(ipp.UNSUPPORTED, [(ipp.MIME, "document-format", [fmt])])],
                             message="Unsupported document format.")
            copies = req.get("copies", 1, group=ipp.JOB)
            if not isinstance(copies, int) or not 1 <= copies <= MAX_COPIES:
                return reply(ipp.ATTRIBUTES_NOT_SUPPORTED,
                             [(ipp.UNSUPPORTED, [(ipp.INTEGER, "copies", [copies if isinstance(copies, int) else 0])])],
                             message=f"copies must be 1 to {MAX_COPIES}.")
            bad = self._check_compression(req, reply)
            if bad:
                return bad
            if op == "Validate-Job":
                return reply(ipp.OK)
            job = self._new_job(req)
            if op == "Print-Job":
                error = self._add_document(job, req, last=True)
                if error:
                    return reply(ipp.COMPRESSION_ERROR, [(ipp.JOB, self.job_attributes(job, uri_host, brief=True))],
                                 message=error)
            return reply(ipp.OK, [(ipp.JOB, self.job_attributes(job, uri_host, brief=True))])
        if op in ("Send-Document", "Cancel-Job", "Get-Job-Attributes", "Close-Job"):
            job_id = req.get("job-id")
            if job_id is None and req.get("job-uri"):
                job_id = int(re.sub(r".*/", "", req.get("job-uri")) or 0)
            job = self.jobs.get(job_id)
            if job is None:
                return reply(ipp.NOT_FOUND, message="Job not found.")
            if op == "Send-Document":
                last = bool(req.get("last-document", True))
                fmt = req.get("document-format")
                if fmt and fmt not in FORMATS:
                    return reply(ipp.FORMAT_NOT_SUPPORTED, [(ipp.UNSUPPORTED, [(ipp.MIME, "document-format", [fmt])])],
                                 message="Unsupported document format.")
                bad = self._check_compression(req, reply)
                if bad:
                    return bad
                if job.state != PENDING or job.queued or (job.document is not None and req.data):
                    return reply(ipp.NOT_POSSIBLE, message="Job is not accepting documents (one document per job).")
                if job.document is None and (req.data or last):
                    error = self._add_document(job, req, last)
                    if error:
                        return reply(ipp.COMPRESSION_ERROR,
                                     [(ipp.JOB, self.job_attributes(job, uri_host, brief=True))], message=error)
                elif last:                       # an empty Send-Document that only closes the job
                    self._queue(job)
                return reply(ipp.OK, [(ipp.JOB, self.job_attributes(job, uri_host, brief=True))])
            if op == "Cancel-Job":
                with self.lock:
                    if job.state != PENDING:
                        return reply(ipp.NOT_POSSIBLE, message="Job can no longer be canceled.")
                    self._finish(job, CANCELED, "job-canceled-by-user")
                return reply(ipp.OK)
            if op == "Close-Job":                # like a last Send-Document without data (PWG 5100.11)
                if job.document is not None:
                    self._queue(job)
                return reply(ipp.OK)
            return reply(ipp.OK, [(ipp.JOB, self.job_attributes(job, uri_host))])
        if op == "Get-Jobs":
            which = req.get("which-jobs", "not-completed")
            done = (CANCELED, ABORTED, COMPLETED)
            with self.lock:
                snapshot = sorted(self.jobs.values(), key=lambda j: j.id)
            jobs = [j for j in snapshot if which == "all" or (which == "completed") == (j.state in done)]
            return reply(ipp.OK, [(ipp.JOB, self.job_attributes(j, uri_host, brief=True)) for j in jobs])
        if op == "Cancel-My-Jobs":
            user = jobwriter.short_name(str(req.get("requesting-user-name") or "anonymous"), 255)
            with self.lock:
                for j in list(self.jobs.values()):
                    if j.state == PENDING and j.user == user:
                        self._finish(j, CANCELED, "job-canceled-by-user")
            return reply(ipp.OK)
        if op == "Identify-Printer":
            return reply(ipp.OK)
        return reply(ipp.OPERATION_NOT_SUPPORTED, message="Operation not supported.")

    def _new_job(self, req):
        with self.lock:
            job = Job(self.next_id, req)
            self.jobs[job.id] = job
            self.next_id += 1
            done = [k for k in sorted(self.jobs) if self.jobs[k].state in (CANCELED, ABORTED, COMPLETED)]
            for old in done[:max(0, len(self.jobs) - 50)]:         # keep the last 50 jobs
                self._remove_document(self.jobs.pop(old))
        return job

    def _check_compression(self, req, reply):
        compression = req.get("compression", "none")
        if compression in COMPRESSIONS:
            return None
        return reply(ipp.COMPRESSION_NOT_SUPPORTED, [(ipp.UNSUPPORTED, [(ipp.KEYWORD, "compression", [compression])])],
                     message="Unsupported compression.")

    def _finish(self, job, state, reason, message=""):
        job.state, job.reasons, job.message, job.completed = state, [reason], message, time.time()
        self._remove_document(job)

    def _add_document(self, job, req, last):
        """store the document; bad compressed data aborts the job and returns an error message"""
        data = req.data
        compression = req.get("compression", "none")
        if compression in ("gzip", "deflate"):
            try:
                d = zlib.decompressobj(16 + zlib.MAX_WBITS if compression == "gzip" else -zlib.MAX_WBITS)
                data = d.decompress(data, MAX_REQUEST)
                if d.unconsumed_tail or not d.eof:
                    raise ValueError("truncated, or larger than 1 GB")
            except (zlib.error, ValueError) as e:
                self._finish(job, ABORTED, "compression-error", f"Bad {compression} data: {e}")
                return f"Bad {compression} data."
        job.format = req.get("document-format", job.format)
        os.makedirs(self.spool, exist_ok=True)         # macOS may clean up old temporary folders
        path = os.path.join(self.spool, f"job-{job.id}.data")
        with open(path, "wb") as f:
            f.write(data)
        job.document = path
        if last:
            self._queue(job)
        return None

    def _queue(self, job):
        with self.lock:
            if job.queued or job.state != PENDING:
                return
            job.queued, job.reasons = True, ["job-queued"]
        self.queue.put(job)

    def printer_attributes(self, uri_host):
        st = self.status
        busy = self.current is not None
        reasons = st["reasons"] or ["none"]
        now = time.time()
        base = f"ipp://{uri_host}/ipp/print"
        a = [
            (ipp.URI, "printer-uri-supported", [base]),
            (ipp.KEYWORD, "uri-security-supported", ["none"]),
            (ipp.KEYWORD, "uri-authentication-supported", ["none"]),
            (ipp.NAME, "printer-name", [self.name]),
            (ipp.TEXT, "printer-info", [self.name]),
            (ipp.TEXT, "printer-location", [""]),
            (ipp.UNKNOWN, "printer-geo-location", [None]),
            (ipp.TEXT, "printer-organization", [""]),
            (ipp.TEXT, "printer-organizational-unit", [""]),
            (ipp.URI, "printer-supply-info-uri", [f"{self.web}/"]),
            (ipp.DATETIME, "printer-current-time", [ipp_time(now)]),
            (ipp.DATETIME, "printer-config-change-date-time", [ipp_time(self.started)]),
            (ipp.INTEGER, "printer-config-change-time", [1]),
            (ipp.DATETIME, "printer-state-change-date-time", [ipp_time(self.started)]),
            (ipp.INTEGER, "printer-state-change-time", [1]),
            (ipp.BOOLEAN, "preferred-attributes-supported", [False]),
            (ipp.KEYWORD, "printer-get-attributes-supported", ["document-format"]),
            (ipp.TEXT, "printer-make-and-model", ["Canon LBP6030w (open printer app)"]),
            (ipp.TEXT, "printer-device-id", ["MFG:Canon;MDL:LBP6030w;CMD:PWG,URF;"]),
            (ipp.URI, "printer-uuid", [f"urn:uuid:{self.uuid}"]),
            (ipp.URI, "printer-more-info", [f"{self.web}/"]),
            (ipp.URI, "printer-icons", [f"{self.web}/{n}" for n in ICONS]),
            (ipp.ENUM, "printer-state", [4 if busy else 3]),
            (ipp.KEYWORD, "printer-state-reasons", reasons),
            (ipp.TEXT, "printer-state-message", ["Printing." if busy else "Idle."]),
            (ipp.BOOLEAN, "printer-is-accepting-jobs", [True]),
            (ipp.INTEGER, "queued-job-count", [self.queue.qsize() + (1 if busy else 0)]),
            (ipp.INTEGER, "printer-up-time", [int(now - self.started) + 1]),
            (ipp.ENUM, "operations-supported", sorted(OPERATIONS)),
            (ipp.CHARSET, "charset-configured", ["utf-8"]),
            (ipp.CHARSET, "charset-supported", ["us-ascii", "utf-8"]),
            (ipp.LANGUAGE, "natural-language-configured", ["en"]),
            (ipp.LANGUAGE, "generated-natural-language-supported", ["en"]),
            (ipp.KEYWORD, "ipp-versions-supported", ["1.1", "2.0"]),
            (ipp.KEYWORD, "ipp-features-supported", ["ipp-everywhere"]),
            (ipp.KEYWORD, "compression-supported", ["deflate", "gzip", "none"]),
            (ipp.MIME, "document-format-default", ["application/octet-stream"]),
            (ipp.MIME, "document-format-supported", FORMATS),
            (ipp.KEYWORD, "pdl-override-supported", ["attempted"]),
            (ipp.BOOLEAN, "multiple-document-jobs-supported", [False]),
            (ipp.INTEGER, "multiple-operation-time-out", [60]),
            (ipp.KEYWORD, "multiple-operation-time-out-action", ["abort-job"]),
            (ipp.KEYWORD, "which-jobs-supported", ["completed", "not-completed", "all"]),
            (ipp.BOOLEAN, "job-ids-supported", [True]),
            (ipp.KEYWORD, "job-creation-attributes-supported",
             ["copies", "media", "media-col", "media-type", "multiple-document-handling", "print-quality",
              "print-scaling", "sides", "print-color-mode", "printer-resolution", "orientation-requested"]),
            (ipp.KEYWORD, "media-supported", MEDIA),
            (ipp.KEYWORD, "media-default", ["iso_a4_210x297mm"]),
            (ipp.KEYWORD, "media-ready", ["iso_a4_210x297mm"]),
            (ipp.BEGIN_COLLECTION, "media-size-supported",
             [{"x-dimension": (ipp.INTEGER, [w]), "y-dimension": (ipp.INTEGER, [h])}
              for w, h in map(media_size, MEDIA)]),
            (ipp.BEGIN_COLLECTION, "media-col-database", [media_col(m) for m in MEDIA]),
            (ipp.BEGIN_COLLECTION, "media-col-default", [media_col("iso_a4_210x297mm", "main", "stationery")]),
            (ipp.BEGIN_COLLECTION, "media-col-ready", [media_col("iso_a4_210x297mm", "main", "stationery")]),
            (ipp.KEYWORD, "media-col-supported", ["media-bottom-margin", "media-left-margin", "media-right-margin",
                                                  "media-size", "media-source", "media-top-margin", "media-type"]),
            (ipp.KEYWORD, "media-source-supported", ["auto", "main"]),
            (ipp.KEYWORD, "media-type-supported", list(jobwriter.MEDIA_TYPES) + ["envelope"]),
            (ipp.KEYWORD, "media-type-default", ["stationery"]),
        ] + [(ipp.INTEGER, f"media-{side}-margin-supported", [MARGIN]) for side in ("bottom", "left", "right", "top")]
        a += [
            (ipp.INTEGER, "copies-default", [1]),
            (ipp.RANGE, "copies-supported", [(1, 99)]),
            (ipp.ENUM, "finishings-default", [3]),
            (ipp.ENUM, "finishings-supported", [3]),
            (ipp.ENUM, "orientation-requested-default", [3]),
            (ipp.ENUM, "orientation-requested-supported", [3]),
            (ipp.KEYWORD, "output-bin-default", ["face-down"]),
            (ipp.KEYWORD, "output-bin-supported", ["face-down"]),
            (ipp.BOOLEAN, "color-supported", [False]),
            (ipp.KEYWORD, "print-color-mode-default", ["monochrome"]),
            (ipp.KEYWORD, "print-color-mode-supported", ["monochrome"]),
            (ipp.ENUM, "print-quality-default", [4]),
            (ipp.ENUM, "print-quality-supported", [3, 4, 5]),
            (ipp.KEYWORD, "print-content-optimize-default", ["auto"]),
            (ipp.KEYWORD, "print-content-optimize-supported", ["auto"]),
            (ipp.KEYWORD, "print-rendering-intent-default", ["auto"]),
            (ipp.KEYWORD, "print-rendering-intent-supported", ["auto"]),
            (ipp.KEYWORD, "print-scaling-default", ["auto"]),
            (ipp.KEYWORD, "print-scaling-supported", ["auto", "auto-fit", "fit", "none"]),
            (ipp.KEYWORD, "sides-default", ["one-sided"]),
            (ipp.KEYWORD, "sides-supported", ["one-sided"]),
            (ipp.KEYWORD, "multiple-document-handling-supported",
             ["separate-documents-uncollated-copies", "separate-documents-collated-copies"]),
            (ipp.RESOLUTION, "printer-resolution-default", [(600, 600, 3)]),
            (ipp.RESOLUTION, "printer-resolution-supported", [(600, 600, 3)]),
            (ipp.RESOLUTION, "pwg-raster-document-resolution-supported", [(600, 600, 3)]),
            (ipp.KEYWORD, "pwg-raster-document-type-supported", ["sgray_8"]),
            (ipp.KEYWORD, "pwg-raster-document-sheet-back", ["normal"]),
            (ipp.KEYWORD, "urf-supported", URF.split(",")),
            (ipp.INTEGER, "pages-per-minute", [18]),
            (ipp.KEYWORD, "identify-actions-default", ["display"]),
            (ipp.KEYWORD, "identify-actions-supported", ["display"]),
        ]
        level = -2 if st.get("toner_level") is None else max(0, min(100, st["toner_level"]))   # -2: unknown
        a += [
            (ipp.KEYWORD, "marker-types", ["toner"]),
            (ipp.NAME, "marker-names", [st.get("toner_name") or "Black Toner"]),
            (ipp.NAME, "marker-colors", ["#000000"]),
            (ipp.INTEGER, "marker-levels", [level]),
            (ipp.INTEGER, "marker-low-levels", [5]),
            (ipp.INTEGER, "marker-high-levels", [100]),
            (ipp.OCTETS, "printer-supply", [f"index=1;class=supplyThatIsConsumed;type=toner;unit=percent;"
                                             f"maxcapacity=100;level={level};colorantname=black;".encode()]),
            (ipp.TEXT, "printer-supply-description", [st.get("toner_name") or "Black Toner"]),
        ]
        return a

    def job_attributes(self, job, uri_host, brief=False):
        a = [
            (ipp.INTEGER, "job-id", [job.id]),
            (ipp.URI, "job-uri", [f"ipp://{uri_host}/ipp/print/{job.id}"]),
            (ipp.ENUM, "job-state", [job.state]),
            (ipp.KEYWORD, "job-state-reasons", job.reasons or ["none"]),
        ]
        if job.message:
            a.append((ipp.TEXT, "job-state-message", [job.message]))
        if brief:
            return a
        start = self.started
        a += [
            (ipp.NAME, "job-name", [job.name]),
            (ipp.NAME, "job-originating-user-name", [job.user]),
            (ipp.URI, "job-printer-uri", [f"ipp://{uri_host}/ipp/print"]),
            (ipp.INTEGER, "job-printer-up-time", [int(time.time() - start) + 1]),
            (ipp.INTEGER, "time-at-creation", [int(job.created - start) + 1]),
            (ipp.INTEGER, "job-impressions-completed", [job.impressions if job.state == COMPLETED else 0]),
        ]
        for name, t in (("time-at-processing", job.processing), ("time-at-completed", job.completed)):
            a.append((ipp.INTEGER, name, [int(t - start) + 1]) if t else (ipp.NO_VALUE, name, [None]))
        return a

    # --- jobs and printer status ----------------------------------------------------------------------------------

    def _worker(self):
        while True:
            job = self.queue.get()
            with self.lock:
                start = job.state == PENDING
                if start:
                    job.state, job.reasons, job.processing = PROCESSING, ["job-printing"], time.time()
                    self.current = job
            if not start:
                self._remove_document(job)
                continue
            try:
                ok, message = self._print(job)
                job.state = COMPLETED if ok else ABORTED
                job.reasons = ["job-completed-successfully" if ok else "aborted-by-system"]
                job.message = message
            except Exception as e:  # noqa: BLE001
                job.state, job.reasons, job.message = ABORTED, ["aborted-by-system"], f"Error: {e}"
            job.completed = time.time()
            log(f"job {job.id} '{job.name}': {JOB_STATES[job.state]} {job.message}")
            self.current = None
            self._remove_document(job)

    @staticmethod
    def _remove_document(job):
        if job.document and os.path.exists(job.document):
            os.remove(job.document)

    def _print(self, job):
        col = job.value("media-col") or {}
        size = col.get("media-size", (None, [{}]))[1][0] if col else {}
        paper = None
        if size:
            paper = paper_for_size(size.get("x-dimension", (0, [0]))[1][0], size.get("y-dimension", (0, [0]))[1][0])
        paper = paper or media.paper_from_ipp(job.value("media"))
        mtype = media.media_type_from_ipp((col.get("media-type", (0, [None]))[1][0] if col else None)
                                          or job.value("media-type"))
        copies = min(MAX_COPIES, max(1, int(job.value("copies", 1))))
        collate = job.value("multiple-document-handling", "") != "separate-documents-uncollated-copies"
        toner_save = job.value("print-quality") == 3
        fit = job.value("print-scaling", "auto") != "none"
        log(f"job {job.id}: {job.format} on {paper} ({mtype}), copies {copies}{'' if collate else ' uncollated'}")
        data, _ = printjob.make_job(job.document, job.format, paper, mtype, copies, collate, toner_save,
                                    job_name=job.name, user=job.user, fit=fit, max_sheets=MAX_SHEETS)
        problems, info = device.validate(data, MAX_SHEETS)
        if problems:
            return False, "Job failed validation: " + "; ".join(problems)
        job.impressions = info["sheets"]
        if self.dry_run:
            os.makedirs(self.dry_run, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            with open(os.path.join(self.dry_run, f"{stamp}-job{job.id}.job"), "wb") as f:
                f.write(data)
            shutil.copy(job.document, os.path.join(self.dry_run, f"{stamp}-job{job.id}.input"))
            return True, f"Dry run: {info['sheets']} sheet(s) written, nothing printed."
        st = self._refresh()
        for _ in range(15):                       # give a printer waking from sleep a moment
            if st["reachable"]:
                break
            time.sleep(2)
            st = self._refresh()
        if not st["reachable"]:
            return False, f"Printer {self.host} is not reachable (switched off?)"
        device.send(data, self.host)
        if st["pages"] is None:
            return True, "Sent; the printer reports no page counter, so printing is not confirmed."
        ok = device.wait_printed(self.host, st["pages"], info["sheets"], self._set_status, timeout=1800)
        return ok, "" if ok else "Timed out waiting for the printer."

    def _set_status(self, st):
        if st["reasons"] != self.status.get("reasons"):
            log(f"printer: {', '.join(st['reasons']) or 'ok'}")
        self.status = st

    def _refresh(self):
        st = device.status(self.host)
        self._set_status(st)
        return st

    def _expire_jobs(self):
        now = time.time()
        with self.lock:
            for j in list(self.jobs.values()):
                if j.state == PENDING and not j.queued and now - j.created > JOB_TIMEOUT:
                    self._finish(j, ABORTED, "aborted-by-system", "No document received in time.")

    def _poll_status(self):
        tick = 0
        while True:
            time.sleep(5)
            self._expire_jobs()
            tick += 1
            if tick % 6 == 1 and self.current is None:      # printer status every 30 s while idle
                try:
                    self._refresh()
                except Exception as e:  # noqa: BLE001
                    log(f"status: {e}")


REASONS = {"media-empty-error": "Out of paper", "media-low-warning": "Paper low", "media-jam-error": "Paper jam",
           "door-open-error": "Door open", "toner-empty-error": "Toner empty", "toner-low-warning": "Toner low",
           "offline-report": "Not reachable (switched off?)", "other-error": "Needs attention"}
PAGE_STYLE = """
body { font: 18px/1.5 -apple-system, system-ui, sans-serif; margin: 0 auto; max-width: 36rem; padding: 1.2rem;
       background: #fff; color: #1d1d1f; }
header { display: flex; align-items: center; gap: 1rem; }
header img { width: 96px; height: 96px; }
h1 { font-size: 1.6rem; margin: 0; }
.state { font-size: 1.25rem; font-weight: 600; }
.ok { color: #1a7f37; } .warn { color: #b35900; } .err { color: #c62828; }
.bar { height: 0.8rem; border-radius: 0.4rem; background: #e5e5ea; overflow: hidden; margin: 0.3rem 0 1rem; }
.bar div { height: 100%; background: #3a3a3c; }
ul { list-style: none; padding: 0; } li { padding: 0.6rem 0; border-top: 1px solid #e5e5ea; }
.meta { color: #6e6e73; font-size: 0.95rem; }
@media (prefers-color-scheme: dark) {
  body { background: #1c1c1e; color: #f2f2f7; } .bar, li { background: none; border-color: #3a3a3c; }
  .bar { background: #3a3a3c; } .bar div { background: #d1d1d6; } .meta { color: #a1a1a6; } }
"""


def status_page(p):
    """small status page (printer-more-info / adminurl): state, toner, recent jobs"""
    st = p.status
    if p.current is not None:
        state, cls = "Printing", "ok"
    elif st["reasons"]:
        state = ", ".join(REASONS.get(r, r) for r in st["reasons"])
        cls = "warn" if all(r.endswith("-warning") for r in st["reasons"]) else "err"
    else:
        state, cls = "Ready", "ok"
    toner = ""
    if st.get("toner_level") is not None:
        level = max(0, min(100, st["toner_level"]))
        toner = (f"<p>Toner {level} %</p><div class=bar><div style='width:{level}%'></div></div>")
    with p.lock:
        jobs = sorted(p.jobs.values(), key=lambda j: j.id, reverse=True)[:10]
    rows = "".join(
        f"<li>{html.escape(j.name)}<br><span class=meta>{JOB_STATES[j.state]}, "
        f"{time.strftime('%H:%M', time.localtime(j.completed or j.created))}"
        f"{' - ' + html.escape(j.message) if j.message else ''}</span></li>" for j in jobs) or "<li class=meta>None</li>"
    return (f"<!doctype html><html><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width, initial-scale=1'><title>{html.escape(p.name)}</title>"
            f"<link rel=icon href=/icon-sm.png><style>{PAGE_STYLE}</style></head><body>"
            f"<header><img src=/icon.png alt=''><div><h1>{html.escape(p.name)}</h1>"
            f"<div class='state {cls}'>{state}</div></div></header>"
            f"<p class=meta>Printer at {html.escape(p.host)}</p>{toner}<h2>Recent jobs</h2><ul>{rows}</ul></body></html>")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "lbp6030-printer-app"
    timeout = 60                                   # idle keep-alive connections

    def log_message(self, fmt, *args):
        pass

    def _body(self):
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            data = bytearray()
            while True:
                size = int(self.rfile.readline(1024).split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                        pass
                    return bytes(data)
                data += self.rfile.read(size)
                self.rfile.readline(1024)
                if len(data) > MAX_REQUEST:
                    raise ValueError("request too large")
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 <= length <= MAX_REQUEST:
            raise ValueError("bad Content-Length")
        return self.rfile.read(length)

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            body = self._body()
        except ValueError as e:
            self.close_connection = True
            return self._send(400, "text/plain", f"bad request: {e}\n".encode())
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/ipp":
            return self._send(400, "text/plain", b"expected application/ipp\n")
        self._send(200, "application/ipp", self.server.printer.handle(body, self.headers.get("Host")))

    def do_GET(self):
        log(f"GET {self.path} from {self.client_address[0]}")
        name = self.path.split("?")[0].lstrip("/")
        if name in ("favicon.ico", "apple-touch-icon.png"):
            name = "icon-sm.png" if name == "favicon.ico" else "icon.png"
        if name in ICONS:
            with open(os.path.join(ICON_DIR, ICONS[name]), "rb") as f:
                return self._send(200, "image/png", f.read())
        if name not in ("", "index.html"):
            return self._send(404, "text/plain", b"not found\n")
        self._send(200, "text/html; charset=utf-8", status_page(self.server.printer).encode())


class Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


def advertise(printer):
    """Bonjour: dns-sd on macOS, avahi-publish elsewhere; returns the process (or None)"""
    txt = ["txtvers=1", "qtotal=1", "rp=ipp/print", f"ty={printer.name}", "product=(LBP6030w)", "note=",
           f"adminurl={printer.web}/",
           "pdl=image/pwg-raster,image/urf", f"URF={URF}", "Color=F", "Duplex=F", f"UUID={printer.uuid}",
           "kind=document", "PaperMax=legal-A4", "priority=0"]
    if shutil.which("dns-sd"):
        cmd = ["dns-sd", "-R", printer.name, "_ipp._tcp,_universal,_print", "local", str(printer.port)] + txt
    elif shutil.which("avahi-publish-service"):
        cmd = ["avahi-publish-service", "--subtype=_universal._sub._ipp._tcp", printer.name, "_ipp._tcp",
               str(printer.port)] + txt
    else:
        log("no dns-sd or avahi-publish-service: not advertised via Bonjour")
        return None
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Stopped(Exception):
    """SIGTERM from outside: exit with an error code so that launchd starts the printer app again (a bootout or
    uninstall removes the job, so nothing restarts then)"""


def _stop(*_):
    raise Stopped


class Server4(Server):
    address_family = socket.AF_INET

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


def make_server(port):
    """IPv4 and IPv6 on one socket where possible, IPv4 only otherwise"""
    try:
        return Server(("::", port), Handler)
    except OSError:
        return Server4(("0.0.0.0", port), Handler)


def serve(host, name="LBP6030w", port=8631, dry_run=None, bonjour=True):
    printer = Printer(host, name, port, dry_run)
    httpd = make_server(port)
    httpd.printer = printer
    bonjour_proc = advertise(printer) if bonjour else None
    signal.signal(signal.SIGTERM, _stop)
    log(f"'{name}' on port {port} -> {host}:9100{' (dry run: ' + dry_run + ')' if dry_run else ''}")
    code = 0
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:              # Ctrl-C in a terminal
        pass
    except Stopped:
        log("stopped (SIGTERM)")
        code = 143
    finally:
        if bonjour_proc:
            bonjour_proc.terminate()
        httpd.server_close()
        shutil.rmtree(printer.spool, ignore_errors=True)
    return code
