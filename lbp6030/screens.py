"""Halftoning: 8-bit gray pages at 600x600 dpi -> printer pixel values.

Pure black (text, rules) is kept sharp; gray areas get a clustered-dot screen and a tone curve; thin gray
features (hairlines, small gray text) are drawn as solid strokes so they stay visible.
"""
import numpy as np

# Nominal ink -> ink sent to the printer. Lifts light grays (otherwise invisible) and keeps white text on
# dark gray readable.
TONE = [(0.0, 0.0), (0.05, 0.12), (0.10, 0.16), (0.25, 0.27), (0.50, 0.47), (0.75, 0.68), (0.90, 0.84),
        (0.95, 0.91), (1.0, 1.0)]
TONER_SAVE_FACTOR = 0.55
# Darkness -> 4-bit pixel level for solid (unscreened) gray strokes.
FLAT = [(0.0, 0.0), (0.02, 3.0), (0.25, 5.0), (0.5, 8.0), (0.75, 10.5), (0.9, 12.5), (1.0, 14.0)]
_LUT = np.array([0, 4, 9, 14], dtype=np.uint8)   # black coverage of a 400-dpi row in half rows -> level


def tone(ink, points=TONE):
    xs, ys = zip(*points)
    return np.interp(ink, xs, ys)


def tone_curve(toner_save=False):
    return [(x, y * TONER_SAVE_FACTOR) for x, y in TONE] if toner_save else TONE


def flat_level(d):
    xs, ys = zip(*FLAT)
    return np.rint(np.interp(d, xs, ys)).astype(np.uint8)


def tiled(t, y0, x0, h, w):
    return t[np.ix_((np.arange(h) + y0) % t.shape[0], (np.arange(w) + x0) % t.shape[1])]


# Threshold tiles: values in (0, 1); a pixel is black where ink > threshold.

def bayer_tile(n=4):
    m = np.array([[0]])
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    return (m.astype(np.float64) + 0.5) / (n * n)


def cluster45_tile(p=8):
    """45-degree clustered dots on square pixels, 600*sqrt(2)/p lpi (p=8: 106 lpi)."""
    y, x = np.mgrid[0:p, 0:p]
    s = np.cos(2 * np.pi * (x + y) / p) + np.cos(2 * np.pi * (x - y) / p)
    order = np.lexsort((x.ravel(), y.ravel(), -np.round(s.ravel(), 9)))
    rank = np.empty(p * p, dtype=np.int64)
    rank[order] = np.arange(p * p)
    return (rank.reshape(p, p) + 0.5) / (p * p)


def native_tile(v1=(5, 1), v2=(-1, 3), size=16):
    """Clustered dots on the printer's 600x400 grid with the dot lattice v1, v2 (in pixels). The default lattice
    gives ~122 lpi, puts the same ink in every row (no stripes) and repeats every 16 px, a distance the band codec
    copies cheaply. Every cell grows in the same order, so there is no slow texture."""
    k, x = np.mgrid[0:size, 0:size]
    (a, b), (c, d) = v1, v2
    det = a * d - b * c
    s = (d * x - c * k) / det
    t = (-b * x + a * k) / det
    ds, dt = s - np.floor(s + 0.5), t - np.floor(t + 0.5)
    spot = np.cos(2 * np.pi * ds) + np.cos(2 * np.pi * dt)
    key = np.round(np.round(-spot, 9) * 1e6 + np.round(np.arctan2(dt, ds), 9) * 10 + np.hypot(ds, dt), 6)
    uniq = np.unique(key.ravel())
    assert len(uniq) == abs(det)
    return (np.searchsorted(uniq, key) + 0.5) / abs(det)


def screen_native(ink, tone_points=TONE, y0=0, x0=0):
    """Ink (0..1) on the 600x400 grid -> 4-bit levels. Each dot grows one pixel at a time; the pixel currently
    growing gets an intermediate level, which gives cell_area * 14 smooth steps while dots stay crisp."""
    ink = tone(ink, tone_points)
    t = tiled(native_tile(), y0, x0, *ink.shape)
    n = int(round(1.0 / (2 * t.min())))
    v = np.clip(ink * n - (t * n - 0.5), 0.0, 1.0)
    v = np.where(ink <= 0, 0.0, np.where(ink >= 1, 1.0, v))
    return np.rint(v * 14).astype(np.uint8)


def _shift(m, dy, dx):
    out = np.zeros_like(m)
    h, w = m.shape
    ys, yd = (slice(dy, h), slice(0, h - dy)) if dy >= 0 else (slice(0, h + dy), slice(-dy, h))
    xs, xd = (slice(dx, w), slice(0, w - dx)) if dx >= 0 else (slice(0, w + dx), slice(-dx, w))
    out[yd, xd] = m[ys, xs]
    return out


def erode(m, ry, rx):
    r = m.copy()
    for dx in range(1, rx + 1):
        r &= _shift(m, 0, dx) & _shift(m, 0, -dx)
    r2 = r.copy()
    for dy in range(1, ry + 1):
        r2 &= _shift(r, dy, 0) & _shift(r, -dy, 0)
    return r2


def dilate(m, ry, rx):
    return ~erode(~m, ry, rx)


def split_gray(gray_mask, thin_r, near_r):
    """-> (large gray areas incl. thin bits close to them, thin gray features standing on white)"""
    big = dilate(erode(gray_mask, *thin_r), *thin_r) & gray_mask
    thin = gray_mask & ~big
    near = thin & dilate(big, *near_r)
    return big | near, thin & ~near


def render_native(gray600, rows400, toner_save=False, thin_boost=0.25, chunk=512):
    """Page for the 4 bpp 600x400 mode -> levels 0..14. Black keeps the area mapping of 1.5 source rows per
    output row (levels 0/4/9/14); gray areas are averaged onto the 400-dpi rows and
    screened where at least half of a pixel is gray; thin gray features are solid, darkened by thin_boost."""
    h600, w = gray600.shape
    tp = tone_curve(toner_save)
    v_text = np.zeros((rows400, w), dtype=np.uint8)
    w_gray = np.zeros((rows400, w), dtype=np.float32)
    ink = np.zeros((rows400, w), dtype=np.float32)

    def rows600(idx):                         # padded index i -> source row i - 1, white outside the page
        r = idx - 1
        res = np.full((len(idx), w), 255, dtype=np.uint8)
        ok = (r >= 0) & (r < h600)
        res[ok] = gray600[r[ok]]
        return res

    for k0 in range(0, rows400, chunk):
        k = np.arange(k0, min(rows400, k0 + chunk))
        m, even = k // 2, (k % 2) == 0
        full = rows600(np.where(even, 3 * m + 1, 3 * m + 2))    # row weighted 1
        half = rows600(np.where(even, 3 * m, 3 * m + 3))        # row weighted 1/2
        sl = slice(k0, k0 + len(k))
        v_text[sl] = _LUT[(full == 0).astype(np.int32) * 2 + (half == 0)]
        fi, hi = (full > 0) & (full < 255), (half > 0) & (half < 255)
        w_gray[sl] = (fi * 2.0 + hi) / 3.0
        s = (np.where(fi, 1.0 - full / 255.0, 0.0) * 2.0 + np.where(hi, 1.0 - half / 255.0, 0.0)) / 3.0
        ink[sl] = np.where(w_gray[sl] > 0, s / np.maximum(w_gray[sl], 1e-6), 0.0)

    areas, thin = split_gray(w_gray >= 0.5, (3, 4), (14, 20))
    out = np.empty((rows400, w), dtype=np.uint8)
    for k0 in range(0, rows400, chunk):
        sl = slice(k0, min(rows400, k0 + chunk))
        i = ink[sl].astype(np.float64)
        d = tone(i, tp)
        v = np.where(areas[sl], screen_native(i, tp, y0=k0), 0)
        v = np.where(thin[sl], flat_level(np.minimum(1.0, d + thin_boost) * (d > 0)), v)
        out[sl] = np.maximum(v_text[sl], v)
    return out


def render_600_2bpp(gray600, toner_save=False, thin_boost=0.25):
    """Page for the 2 bpp 600x600 mode -> values 0 or 3. No row conversion in this mode, so a 45-degree 106 lpi
    clustered screen works without stripes; thin gray features get a fine 4x4 dither of the darkened ink."""
    g = gray600
    gray = (g > 0) & (g < 255)
    d = tone(np.where(gray, 1.0 - g.astype(np.float32) / 255.0, 0.0), tone_curve(toner_save)).astype(np.float32)
    areas, thin = split_gray(gray, (4, 4), (20, 20))
    h, w = g.shape
    v = np.zeros((h, w), dtype=np.uint8)
    v[areas & (d > tiled(cluster45_tile(8), 0, 0, h, w))] = 3
    v[thin & (np.minimum(1.0, d + thin_boost) > tiled(bayer_tile(4), 0, 0, h, w)) & (d > 0)] = 3
    v[g == 0] = 3
    return v
