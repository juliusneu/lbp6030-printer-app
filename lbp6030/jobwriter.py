"""Job writer: compressed bands -> a complete print job (packets carrying the page description).
The format is described in docs/PROTOCOL.md."""
import struct
import time

MAX_PACKET = 4096  # including the 20-byte header
HDR = 20

# band codec parameters of the 4 bpp 600x400 and the 2 bpp 600x600 raster modes
PARAM_4BPP = bytes([8, 4, 3, 1, 2, 0]) + struct.pack("<H", 80)
PARAM_2BPP = bytes([4, 2, 8, 4, 0, 0xFF]) + struct.pack("<H", 80)

# PWG media name -> (size code in the job settings, size code in the page description, width and height in pixels
# at 600 dpi, raster mode in bits per pixel, media type code when printed on plain paper)
PAPER = {
    "iso_a4_210x297mm": (2, 0, 4958, 7016, 4, 3),
    "na_letter_8.5x11in": (13, 100, 5100, 6600, 4, 3),
    "na_legal_8.5x14in": (12, 102, 5100, 8400, 4, 3),
    "om_oficio_216x318mm": (62, 113, 5100, 7508, 4, 3),
    "na_foolscap_8.5x13in": (61, 112, 5100, 7791, 4, 3),
    "om_mexico-oficio_216x341mm": (210, 98, 5100, 8058, 4, 3),
    "om_brazil-oficio_216x356mm": (204, 96, 5100, 8383, 4, 3),
    "om_16k_195x270mm": (212, 120, 4608, 6374, 4, 3),
    "iso_a5_148x210mm": (3, 2, 3499, 4958, 2, 3),
    "jis_b5_182x257mm": (7, 11, 4299, 6074, 2, 3),
    "na_executive_7.25x10.5in": (10, 101, 4350, 6300, 2, 3),
    "jpn_hagaki_100x148mm": (14, 20, 2358, 3499, 2, 3),
    "jpn_oufuku_148x200mm": (15, 21, 3499, 4724, 2, 3),
    "om_4up-postcard_200x297mm": (55, 22, 4724, 7016, 2, 3),
    "na_number-10_4.125x9.5in": (22, 35, 2483, 5691, 2, 6),
    "iso_c5_162x229mm": (21, 37, 3824, 5408, 2, 6),
    "iso_dl_110x220mm": (24, 39, 2599, 5199, 2, 6),
    "na_monarch_3.875x7.5in": (23, 36, 2316, 4491, 2, 6),
    "jpn_chou3_120x235mm": (193, 34, 2833, 5550, 2, 6),
    "om_yougata-naga3_120x235mm": (213, 40, 2833, 5550, 2, 6),
}

# IPP media-type keyword -> (media type code, needs the 2 bpp 600x600 mode)
MEDIA_TYPES = {
    "stationery": (3, False),
    "stationery-lightweight": (10, False),
    "transparency": (4, False),
    "stationery-heavyweight": (8, True),
    "cardstock": (47, True),
    "labels": (7, True),
}

# job options -> (name and default value as written in the job, or None if not sent by default), in job order
OPTIONS = {
    "paper_save": ("PAPER-SAVE", "OFF"),
    "paper_size_check": ("CHKPAPERSIZE", None),
    "special_mode": ("PRINT-SP-LEVEL", "MODE1"),
    "special_mode_2": ("PRINT-SP-LEVEL2", "OFF"),
    "barcode": ("BARCODE", None),
    "white_gap": ("WHITEGAP", None),
    "wrinkle_reduction": ("WRINKLE-ESCAPE-MODE", "OFF"),
}


def packet(op, payload=b"", flags=0x00):
    payload = bytes(payload)
    assert len(payload) <= MAX_PACKET - HDR
    return b"\xCD\xCA\x10" + bytes([flags]) + struct.pack(">HHH", op, 1, len(payload)) + b"\xFF\xFF" + b"\x00" * 8 + payload


def text(s):
    b = s.encode("utf-8")
    assert len(b) < 256
    return b"\x03\xF2" + bytes([len(b)]) + b


def timestamp(t=None):
    """8-byte UTC time: year:12 month:4 day:5 weekday:3 0:8 | hour:8 minute:8 ms-in-minute:16"""
    tm = time.gmtime(t)
    ms = int(((t if t is not None else time.time()) % 60) * 1000)
    wday = (tm.tm_wday + 1) % 7  # Sunday = 0
    w0 = (tm.tm_year << 20) | (tm.tm_mon << 16) | (tm.tm_mday << 11) | (wday << 8)
    return struct.pack(">IBBH", w0, tm.tm_hour, tm.tm_min, ms)


def short_name(name, limit=32):
    """the job name as cut to 32 bytes (attribute 0004 and setting 082b; attribute 0117 keeps the full name)"""
    while len(name.encode("utf-8")) > limit:
        name = name[:-1]
    return name


def job_header(job_name, user, host, client_id, computer, time_bytes):
    """payload of packet 0x006B (job description)"""
    host16 = host.encode("ascii", "replace")[:15].ljust(16, b"\x00")
    attrs = [
        (0x0004, text(short_name(job_name))),
        (0x0117, text(job_name)),
        (0x0006, text(user)),
        (0x000D, b"\x04"),
        (0x0118, bytes(client_id) + b"\x00" * 16 + host16 + host16),
        (0x000C, b"\x32"),  # priority 50
        (0x0009, time_bytes),
        (0x013F, text(computer)),
    ]
    out = bytes.fromhex("000a00f0000101013000030300 00".replace(" ", ""))
    for aid, val in attrs:
        out += struct.pack(">HH", aid, len(val)) + val
    return out


def pdl_int(tag, value, immediate=True):
    """attribute in its smallest form: immediate (0..0x7f, if allowed), a0 (s8), a4 (s16), a5 (s32)"""
    if immediate and 0 <= value < 0x80:
        return bytes([tag, value])
    if -0x80 <= value < 0x80:
        return bytes([tag, 0xA0]) + struct.pack(">b", value)
    if -0x8000 <= value < 0x8000:
        return bytes([tag, 0xA4]) + struct.pack(">h", value)
    return bytes([tag, 0xA5]) + struct.pack(">i", value)


def pdl_pair(tag, x, y):
    """pair attribute: a1 = two s8, a5 = two s16"""
    if -0x80 <= x < 0x80 and -0x80 <= y < 0x80:
        return bytes([tag, 0xA1]) + struct.pack(">bb", x, y)
    return bytes([tag, 0xA5]) + struct.pack(">hh", x, y)


def band_record(width, height, y, stream, last, param=PARAM_4BPP, method=3):
    """band record as (header part, data part); the split matters for the 64 KB flush rule"""
    blk_head = bytes(param) + bytes([0 if last else 1]) + struct.pack("<I", len(stream))
    blk_tail = bytes(stream) + b"\x80"
    n = len(blk_head) + len(blk_tail)
    if n >= 0x8000:
        # the printer drops pages with band records this large; split the band (pipeline.encode_page does)
        raise ValueError(f"band record of {n} bytes is too large (limit 32767); split the band")
    if n < 0x100:
        data_tag = bytes([0x9C, n])
    elif n < 0x10000:
        data_tag = b"\x9D" + struct.pack(">H", n)
    else:
        data_tag = b"\x9E" + struct.pack(">I", n)
    head = (
        b"\x62"
        + b"\xE3\x85" + struct.pack(">HH", width, height)
        + pdl_pair(0xE8, 0, y)
        + bytes([0xE1, method])
        + pdl_int(0xD7, n, immediate=False)
        + data_tag
        + blk_head
    )
    return (head, blk_tail)


def pdl_session_header(paper_code, yres=400):
    return (
        b"\x01"
        + b"\xC1\x85" + struct.pack(">I", 0x10001089)
        + b"\xC2\x00"
        + b"\xD8\x84" + struct.pack(">H", 600)
        + b"\xDD\x80\xC8"
        + b"\xF0\x84" + struct.pack(">H", 2048)
        + (b"\xE9\x84" + struct.pack(">H", yres) if yres != 600 else b"")
        + b"\x02"
        + pdl_int(0xC3, paper_code)
        + b"\xC5\x00\xC6\x00"
        + b"\x51\xF2\x00"
    )


def pdl_page_header(w600, h600):
    return b"\x03" + b"\xE7\x85" + struct.pack(">HH", w600, h600) + b"\xDE\x80\x00\xC8\x00\xCA\xA1\x00\x00\xCB\x00"


def pdl_page_body(band_records, bpp=4):
    parts = [b"\x61\xE6\x80" + bytes([bpp]) + b"\xE5\x00"]
    for rec in band_records:
        parts += list(rec)
    parts.append(b"\x13")
    return parts


def data_packets(pdl, flags=0x02):
    out = []
    step = MAX_PACKET - HDR - 1
    for i in range(0, len(pdl), step):
        out.append(packet(0x1A, b"\x01" + pdl[i:i + step], flags))
    return out


FLUSH_LIMIT = 65536


def data_packets_flushed(parts, limit=FLUSH_LIMIT):
    """page body -> data packets; flush (partial packet + empty packet) before `limit` bytes since the last flush"""
    out = []
    step = MAX_PACKET - HDR - 1
    pending = bytearray()
    count = 0
    for part in parts:
        if count + len(part) > limit:
            if pending:
                out.append(packet(0x1A, b"\x01" + bytes(pending), 0x02))
            out.append(packet(0x1A, b"\x01", 0x02))
            pending = bytearray()
            count = 0
        pending += part
        count += len(part)
        while len(pending) >= step:
            out.append(packet(0x1A, b"\x01" + bytes(pending[:step]), 0x02))
            del pending[:step]
    if pending:
        out.append(packet(0x1A, b"\x01" + bytes(pending), 0x02))
    return out


def job_bpp(media="iso_a4_210x297mm", media_type="stationery", options=None):
    """raster mode for this paper / media type / options: 2 (600x600) or 4 (600x400)"""
    if PAPER[media][4] == 2 or MEDIA_TYPES[media_type][1]:
        return 2
    if (options or {}).get("wrinkle_reduction") == "MODE2":
        return 2
    return 4


def build_job(pages, media="iso_a4_210x297mm", job_name="document", user="user", host="lbp6030-printer-app",
              client_id=None, computer="lbp6030-printer-app", time_bytes=None,
              media_type="stationery", bpp=None, copies=1, collate=True, options=None):
    """pages: per page a list of band_record(). Collated copies repeat the whole job; uncollated copies are a
    header field and the printer repeats each page. options: OPTIONS keys with the values written to the job,
    e.g. {"paper_size_check": "OFF"}."""
    settings_size, pdl_size, w600, h600, _, plain_type = PAPER[media]
    if bpp is None:
        bpp = job_bpp(media, media_type, options)
    type_code = plain_type if media_type == "stationery" else MEDIA_TYPES[media_type][0]
    uncollated = copies > 1 and not collate
    if client_id is None:
        import uuid

        client_id = uuid.uuid5(uuid.NAMESPACE_DNS, host).bytes
    if time_bytes is None:
        time_bytes = timestamp()
    pk = [packet(0x6B, job_header(job_name, user, host, client_id, computer, time_bytes))]
    pk.append(packet(0x14, b"\x00\x00\x00\x00"))
    for p in (
        "08b302",
        None,  # job name
        "07d7%04x" % (copies if uncollated else 1),
        "07d80f",
        "07d91700000000",
        "084a04",
        "07da00",
        "08a5fefefffe000000000000000003",
    ):
        pk.append(packet(0x15, b"\x08\x2b" + text(short_name(job_name)) if p is None else bytes.fromhex(p)))
    pk.append(packet(0x17, b"\x00\x00\x00\x00"))
    for p in (
        "002e830000",
        "08b502",
        "086e010b0000",
        "07ed%02xfe00%02x0000000000000000" % (type_code, settings_size) + "03",
        "07d70001",
        "07e004",
        "003a08" + "0258" + ("0190" if bpp == 4 else "0258"),
    ):
        pk.append(packet(0x18, bytes.fromhex(p)))
    unknown = set(options or {}) - set(OPTIONS)
    assert not unknown, f"unknown job option(s) {unknown}"
    for key, (name, default) in OPTIONS.items():
        val = (options or {}).get(key, default)
        if val is None:
            continue
        n, v = name.encode(), val.encode()
        flag = 0x83 if key == "paper_save" else 0xFF
        pk.append(packet(0x1D, bytes([0, 0, 0, 0, 0, 0x8F, 0, flag, len(n)]) + n + bytes([len(v)]) + v))
    for i, bands in enumerate(pages):
        head = (pdl_session_header(pdl_size, yres=400 if bpp == 4 else 600) if i == 0 else b"") \
            + pdl_page_header(w600, h600)
        pk += data_packets(head)
        pk.append(packet(0x1A, b"\x01", 0x02))
        pk += data_packets_flushed(pdl_page_body(bands, bpp=bpp))
    pk += data_packets(b"\x12\x11")
    pk.append(packet(0x1A, b"\x01", 0x00))
    n = len(pages) * (copies if uncollated else 1)   # pages the printer will output
    pk += [
        packet(0x19),
        packet(0x15, b"\x01\x13" + struct.pack(">I", n)),
        packet(0x16),
        packet(0x12, b"\x01\x13" + struct.pack(">I", n)),
    ]
    tail_size = len(packet(0x12, bytes(6))) + len(packet(0x13, b"\x00"))
    total = sum(len(x) for x in pk) + tail_size
    pk.append(packet(0x12, b"\x07\xD6" + struct.pack(">I", total)))
    pk.append(packet(0x13, b"\x00"))
    job = b"".join(pk)
    assert len(job) == total
    if copies > 1 and collate:
        job = job * copies
    return job


def band_heights(page_rows, line_bytes, chunk=256, max_bytes=131072):
    """chunks of `chunk` rows, each split into bands of floor(max_bytes / line_bytes) rows"""
    sub = max_bytes // line_bytes
    out = []
    y = 0
    while y < page_rows:
        c = min(chunk, page_rows - y)
        while c > 0:
            h = min(sub, c)
            out.append(h)
            c -= h
            y += h
    return out
