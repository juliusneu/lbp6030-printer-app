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


def read_pgm(path):
    with open(path, "rb") as f:
        data = f.read()
    assert data[:2] == b"P5", "expect binary PGM (P5)"
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
    i += 1
    w, h, maxval = fields
    assert maxval == 255
    return np.frombuffer(data[i:i + w * h], dtype=np.uint8).reshape(h, w)


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


def render_pdf_gray(pdf, tmpdir, renderer="auto", dpi=600.0):
    """PDF -> 8-bit gray pages at `dpi`, without anti-aliasing (renderer: auto, quartz or poppler)."""
    prefix = os.path.join(tmpdir, "pg")
    if renderer == "auto":
        renderer = "quartz" if quartz_binary() else "gray"
    if renderer == "quartz":
        subprocess.run([quartz_binary(), pdf, prefix, f"{dpi:.4f}"], check=True)
    else:
        subprocess.run(["pdftoppm", "-r", f"{dpi:.4f}", "-gray", "-aa", "no", "-aaVector", "no", pdf, prefix], check=True)
    files = sorted(f for f in os.listdir(tmpdir) if f.startswith("pg") and f.endswith(".pgm"))
    return [read_pgm(os.path.join(tmpdir, f)) for f in files]


def render_pdf_fit(pdf, tmpdir, renderer, paper_w, paper_h, rotate="auto", fit=True):
    """Render and rotate; shrink the document to fit if a page is larger than the paper (never enlarge).
    Returns (pages, scale)."""
    pages = [orient(p, rotate) for p in render_pdf_gray(pdf, tmpdir, renderer)]
    scale = min([1.0] + [min(paper_w / p.shape[1], paper_h / p.shape[0]) for p in pages])
    if not fit or scale >= 0.99:
        return pages, 1.0
    sub = os.path.join(tmpdir, "fit")
    os.makedirs(sub, exist_ok=True)
    pages = [orient(p, rotate) for p in render_pdf_gray(pdf, sub, renderer, dpi=600.0 * scale * 0.999)]
    return pages, scale


# Raster and image input. PWG Raster (PWG 5102.4): "RaS2" + 1796-byte page headers. Apple Raster: "UNIRAST".

def _unpack_lines(data, pos, width, height, bpp_bytes, urf):
    """PackBits-style line coding of PWG Raster and URF: a line-repeat byte (n -> n+1 lines), then
    0..127 = next pixel n+1 times, 129..255 = 257-n literal pixels, 128 = 129 literal pixels (PWG) or white to the
    end of the line (URF)."""
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


def read_pwg(data):
    """PWG Raster bytes -> list of (gray page, xdpi, ydpi)"""
    assert data[:4] == b"RaS2", "not a PWG Raster stream"
    pos, pages = 4, []
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
        pages.append((_to_gray(rows, width, bpp_bytes, white_is_zero=(cspace == 3)), xdpi, ydpi))
    return pages


def read_urf(data):
    """Apple Raster (URF) bytes -> list of (gray page, xdpi, ydpi)"""
    assert data[:8] == b"UNIRAST\x00", "not an Apple Raster (URF) stream"
    count = struct.unpack(">I", data[8:12])[0]
    pos, pages = 12, []
    for _ in range(count):
        bpp, cspace = data[pos], data[pos + 1]
        width, height, dpi = struct.unpack(">III", data[pos + 12:pos + 24])
        pos += 32
        rows, pos = _unpack_lines(data, pos, width, height, bpp // 8, urf=True)
        pages.append((_to_gray(rows, width, bpp // 8), dpi, dpi))
    return pages


def read_image(path):
    """JPEG/PNG -> one gray page; its own pixel size is kept (scaled to the paper later)"""
    from PIL import Image
    im = Image.open(path)
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


def load_document(path, content_type, paper_w, paper_h, tmpdir, renderer="auto", rotate="auto", fit=True):
    """any supported input -> (list of 8-bit gray pages at 600x600 dpi, centre?) for pipeline.build_gray"""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in ("application/pdf", "") and open(path, "rb").read(5) == b"%PDF-":
        pages, scale = render_pdf_fit(path, tmpdir, renderer, paper_w, paper_h, rotate, fit=fit)
        return pages, scale < 1.0
    data = open(path, "rb").read()
    if data[:4] == b"RaS2":
        raw = read_pwg(data)
    elif data[:8] == b"UNIRAST\x00":
        raw = read_urf(data)
    else:
        raw = read_image(path)
    pages, centre = [], False
    for gray, xdpi, ydpi in raw:
        g = to_600(gray, xdpi, ydpi)
        if data[:4] == b"RaS2" or data[:8] == b"UNIRAST\x00":
            g = snap_antialiasing(g)
        g, scaled = fit_to_paper(g, paper_w, paper_h, rotate, fit)
        centre = centre or scaled or not (data[:4] == b"RaS2" or data[:8] == b"UNIRAST\x00")
        pages.append(g)
    return pages, centre
