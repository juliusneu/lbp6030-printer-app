"""Input pages -> 8-bit gray pages at 600x600 dpi (255 = white): PDF (macOS Quartz, poppler as fallback), PWG
Raster and Apple Raster (URF) as sent by AirPrint / IPP Everywhere clients, and JPEG/PNG."""
import os
import struct
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
QUARTZ_SRC = os.path.join(HERE, "..", "quartz", "quartz_render.swift")
QUARTZ_BIN = os.path.join(HERE, "..", "build", "quartz_render")


def _pgm_header(data):
    """binary PGM (P5) header -> (width, height, offset of the pixels)"""
    if data[:2] != b"P5":
        raise ValueError("expect binary PGM (P5)")
    fields = []
    i = 2
    while len(fields) < 3:
        while data[i:i + 1].isspace():
            i += 1
        if data[i:i + 1] == b"#":
            while data[i:i + 1] not in (b"\n", b""):
                i += 1
            continue
        j = i
        while not data[j:j + 1].isspace():
            j += 1
        fields.append(int(data[i:j]))
        i = j
    w, h, maxval = fields
    if maxval != 255:
        raise ValueError("expect 8-bit PGM")
    return w, h, i + 1


def read_pgm(path):
    with open(path, "rb") as f:
        data = f.read()
    w, h, i = _pgm_header(data)
    return np.frombuffer(data[i:i + w * h], dtype=np.uint8).reshape(h, w)


def pgm_size(path):
    with open(path, "rb") as f:
        return _pgm_header(f.read(256))[:2]


def orient(bits, mode="auto"):
    """Rotate a landscape page onto the portrait sheet, 90 degrees counter-clockwise.
    auto: only if wider than tall; ccw / cw: always; none: never."""
    if mode == "none" or (mode == "auto" and bits.shape[1] <= bits.shape[0]):
        return bits
    return np.rot90(bits, -1 if mode == "cw" else 1).copy()


def fit_gray(gray, w, h, centre=False):
    """Crop / pad (white) a page to exactly w x h; centre=True centres a smaller page."""
    if centre and gray.shape[0] <= h and gray.shape[1] <= w:
        out = np.full((h, w), 255, dtype=np.uint8)
        y0, x0 = (h - gray.shape[0]) // 2, (w - gray.shape[1]) // 2
        out[y0:y0 + gray.shape[0], x0:x0 + gray.shape[1]] = gray
        return out
    if (gray.shape[0] > h and (gray[h:, :] < 255).any()) or (gray.shape[1] > w and (gray[:, w:] < 255).any()):
        print(f"WARNING: page {gray.shape[1]}x{gray.shape[0]} px is larger than the sheet {w}x{h} px "
              f"and has content outside it; it will be cropped", file=sys.stderr)
    out = np.full((h, w), 255, dtype=np.uint8)
    hh, ww = min(h, gray.shape[0]), min(w, gray.shape[1])
    out[:hh, :ww] = gray[:hh, :ww]
    return out


def quartz_binary():
    """Path of the Quartz renderer (macOS only), built from quartz_render.swift on first use; None if unavailable."""
    exe, src = QUARTZ_BIN, QUARTZ_SRC
    if sys.platform != "darwin":
        return None
    if not os.path.exists(exe) or os.path.getmtime(exe) < os.path.getmtime(src):
        os.makedirs(os.path.dirname(exe), exist_ok=True)
        try:
            subprocess.run(["swiftc", "-O", src, "-o", exe], check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            return None
    return exe


def render_pdf_files(pdf, tmpdir, renderer="auto", dpi=600.0):
    """PDF -> one 8-bit gray PGM file per page at `dpi`, without anti-aliasing (renderer: auto, quartz or poppler)"""
    prefix = os.path.join(tmpdir, "pg")
    if renderer == "auto":
        renderer = "quartz" if quartz_binary() else "gray"
    if renderer == "quartz":
        subprocess.run([quartz_binary(), pdf, prefix, f"{dpi:.4f}"], check=True)
    else:
        subprocess.run(["pdftoppm", "-r", f"{dpi:.4f}", "-gray", "-aa", "no", "-aaVector", "no", pdf, prefix], check=True)
    return [os.path.join(tmpdir, f) for f in sorted(os.listdir(tmpdir)) if f.startswith("pg") and f.endswith(".pgm")]


def render_pdf_gray(pdf, tmpdir, renderer="auto", dpi=600.0):
    return [read_pgm(f) for f in render_pdf_files(pdf, tmpdir, renderer, dpi)]


def iter_pdf(pdf, tmpdir, renderer, paper_w, paper_h, rotate="auto", fit=True):
    """Render, rotate and shrink to fit if a page is larger than the paper (never enlarge). The pages are read
    one at a time. Returns (page count, page generator, scale)."""
    files = render_pdf_files(pdf, tmpdir, renderer)
    sizes = []
    for w, h in map(pgm_size, files):
        turn = rotate in ("ccw", "cw") or (rotate == "auto" and w > h)
        sizes.append((h, w) if turn else (w, h))
    scale = min([1.0] + [min(paper_w / w, paper_h / h) for w, h in sizes])
    if not fit or scale >= 0.99:
        scale = 1.0
    else:
        sub = os.path.join(tmpdir, "fit")
        os.makedirs(sub, exist_ok=True)
        files = render_pdf_files(pdf, sub, renderer, dpi=600.0 * scale * 0.999)
    return len(files), (orient(read_pgm(f), rotate) for f in files), scale


def render_pdf_fit(pdf, tmpdir, renderer, paper_w, paper_h, rotate="auto", fit=True):
    """Returns (pages, scale); see iter_pdf"""
    _, pages, scale = iter_pdf(pdf, tmpdir, renderer, paper_w, paper_h, rotate, fit)
    return list(pages), scale


# Raster and image input. PWG Raster (PWG 5102.4): "RaS2" + 1796-byte page headers. Apple Raster: "UNIRAST".

MAX_PIXELS = 6000 * 10000      # more than any paper size at 600 dpi


def _check_size(width, height):
    """a few bytes of line coding can describe a huge page; refuse sizes no paper needs before allocating"""
    if not (0 < width <= 20000 and 0 < height <= 20000 and width * height <= MAX_PIXELS):
        raise ValueError(f"page of {width} x {height} pixels is too large")


def _unpack_lines(data, pos, width, height, bpp_bytes, urf):
    """PackBits-style line coding of PWG Raster and URF: a line-repeat byte (n -> n+1 lines), then
    0..127 = next pixel n+1 times, 129..255 = 257-n literal pixels, 128 = 129 literal pixels (PWG) or white to the
    end of the line (URF)."""
    _check_size(width, height)
    try:
        return _unpack(data, pos, width, height, bpp_bytes, urf)
    except IndexError:
        raise ValueError("truncated raster data") from None


def _unpack(data, pos, width, height, bpp_bytes, urf):
    line_bytes = width * bpp_bytes
    out = np.empty((height, line_bytes), dtype=np.uint8)
    white = b"\xff" * bpp_bytes
    y = 0
    while y < height:
        repeat = data[pos] + 1
        pos += 1
        line = bytearray()
        while len(line) < line_bytes:
            c = data[pos]
            pos += 1
            if c < 128:
                line += data[pos:pos + bpp_bytes] * (c + 1)
                pos += bpp_bytes
            elif c == 128 and urf:
                line += white * ((line_bytes - len(line)) // bpp_bytes)
            else:
                n = 257 - c
                line += data[pos:pos + n * bpp_bytes]
                pos += n * bpp_bytes
        row = np.frombuffer(bytes(line[:line_bytes]), dtype=np.uint8)
        for _ in range(min(repeat, height - y)):
            out[y] = row
            y += 1
    return out, pos


def _to_gray(rows, width, bpp_bytes, white_is_zero=False):
    if bpp_bytes == 1:
        g = rows.reshape(rows.shape[0], width)
    else:  # RGB: luminance
        rgb = rows.reshape(rows.shape[0], width, bpp_bytes)[:, :, :3].astype(np.float32)
        g = np.rint(rgb[:, :, 0] * 0.299 + rgb[:, :, 1] * 0.587 + rgb[:, :, 2] * 0.114).astype(np.uint8)
    return (255 - g) if white_is_zero else g


def iter_pwg(data):
    """PWG Raster bytes -> (gray page, xdpi, ydpi) per page"""
    if data[:4] != b"RaS2":
        raise ValueError("not a PWG Raster stream")
    pos = 4
    while pos + 1796 <= len(data):
        h = data[pos:pos + 1796]
        pos += 1796
        xdpi, ydpi = struct.unpack(">II", h[276:284])
        width, height = struct.unpack(">II", h[372:380])
        bits_pixel = struct.unpack(">I", h[388:392])[0]
        cspace = struct.unpack(">I", h[400:404])[0]
        bpp_bytes = bits_pixel // 8
        if bits_pixel not in (8, 24, 32):
            raise ValueError(f"unsupported PWG raster depth {bits_pixel}")
        rows, pos = _unpack_lines(data, pos, width, height, bpp_bytes, urf=False)
        # colour space 3 (K) has 0 = white; the gray and RGB spaces have 0 = black
        yield _to_gray(rows, width, bpp_bytes, white_is_zero=(cspace == 3)), xdpi, ydpi


def iter_urf(data):
    """Apple Raster (URF) bytes -> (gray page, xdpi, ydpi) per page; stops at the end of the data"""
    if data[:8] != b"UNIRAST\x00":
        raise ValueError("not an Apple Raster (URF) stream")
    count = struct.unpack(">I", data[8:12])[0]
    pos, k = 12, 0
    while pos + 32 <= len(data) and (count == 0 or k < count):
        bpp = data[pos]
        width, height, dpi = struct.unpack(">III", data[pos + 12:pos + 24])
        pos += 32
        if bpp not in (8, 24, 32):
            raise ValueError(f"unsupported URF depth {bpp}")
        rows, pos = _unpack_lines(data, pos, width, height, bpp // 8, urf=True)
        k += 1
        yield _to_gray(rows, width, bpp // 8), dpi, dpi


def read_pwg(data):
    return list(iter_pwg(data))


def read_urf(data):
    return list(iter_urf(data))


def read_image(path):
    """JPEG/PNG -> one gray page; its own pixel size is kept (scaled to the paper later)"""
    from PIL import Image
    im = Image.open(path)
    _check_size(*im.size)
    im = im.convert("L")
    dpi = im.info.get("dpi", (0, 0))[0] or 300
    return [(np.asarray(im, dtype=np.uint8).copy(), dpi, dpi)]


def to_600(gray, xdpi, ydpi):
    """resample a gray page to 600 x 600 dpi (no-op when it already is)"""
    if (xdpi, ydpi) == (600, 600):
        return gray
    from PIL import Image
    h, w = gray.shape
    size = (max(1, round(w * 600 / xdpi)), max(1, round(h * 600 / ydpi)))
    return np.asarray(Image.fromarray(gray).resize(size, Image.LANCZOS), dtype=np.uint8)


def snap_antialiasing(gray, dark=48, light=232):
    """Anti-aliased text edges from client-rendered raster would be treated as gray fill and screened.
    Snap near-black / near-white to pure values and the thin gray fringe touching black to black or white; real
    gray areas stay untouched."""
    from .screens import dilate, erode
    g = gray.copy()
    g[g <= dark] = 0
    g[g >= light] = 255
    inter = (g > 0) & (g < 255)
    large = dilate(erode(inter, 2, 2), 2, 2) & inter        # gray areas wider than ~5 px stay gray
    fringe = inter & ~large & dilate(g == 0, 1, 1)
    g[fringe & (g < 128)] = 0
    g[fringe & (g >= 128)] = 255
    return g


def fit_to_paper(gray, paper_w, paper_h, rotate="auto", fit=True):
    """orient + shrink-to-fit (never enlarge) for raster/image input; returns (page, scaled?)"""
    from PIL import Image
    g = orient(gray, rotate)
    scale = min(1.0, paper_w / g.shape[1], paper_h / g.shape[0])
    if not fit or scale >= 0.99:
        return g, False
    size = (max(1, int(g.shape[1] * scale)), max(1, int(g.shape[0] * scale)))
    return np.asarray(Image.fromarray(g).resize(size, Image.LANCZOS), dtype=np.uint8), True


def iter_document(path, content_type, paper_w, paper_h, tmpdir, renderer="auto", rotate="auto", fit=True):
    """any supported input -> (page count or None if unknown, generator of 8-bit gray pages at the paper size,
    600x600 dpi). Pages are produced one at a time."""
    with open(path, "rb") as f:
        head = f.read(8)
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in ("application/pdf", "application/octet-stream", "") and head[:5] == b"%PDF-":
        count, pages, scale = iter_pdf(path, tmpdir, renderer, paper_w, paper_h, rotate, fit=fit)
        return count, (fit_gray(p, paper_w, paper_h, centre=scale < 1.0) for p in pages)
    raster = head[:4] == b"RaS2" or head == b"UNIRAST\x00"
    if raster:
        with open(path, "rb") as f:
            data = f.read()
        count = struct.unpack(">I", data[8:12])[0] or None if head == b"UNIRAST\x00" else None
        raw = iter_pwg(data) if head[:4] == b"RaS2" else iter_urf(data)
    else:
        count, raw = 1, read_image(path)

    def pages():
        for gray, xdpi, ydpi in raw:
            g = to_600(gray, xdpi, ydpi)
            if raster:
                g = snap_antialiasing(g)
            g, scaled = fit_to_paper(g, paper_w, paper_h, rotate, fit)
            yield fit_gray(g, paper_w, paper_h, centre=scaled or not raster)
    return count, pages()
