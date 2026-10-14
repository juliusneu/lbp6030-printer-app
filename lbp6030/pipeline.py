"""Gray pages -> printer job: halftoning (screens), band compression (bandcodec), job writing (jobwriter)."""
import numpy as np

from . import bandcodec, jobwriter, screens
from .render import fit_gray

# Band records must stay below 32 KB: the printer silently drops pages with larger bands. Oversized bands are
# split by rows (every band record carries its own position and height).
MAX_BAND_STREAM = 30000


def pack4(vals, ls):
    """(h, w) values 0..15 -> (h, ls) bytes, left pixel in the high nibble"""
    h, w = vals.shape
    out = np.zeros((h, ls * 2), dtype=np.uint8)
    out[:, :w] = vals
    return (out[:, 0::2] << 4) | out[:, 1::2]


def pack2(vals, ls):
    """(h, w) values 0..3 -> (h, ls) bytes, left pixel in the two high bits"""
    h, w = vals.shape
    out = np.zeros((h, ls * 4), dtype=np.uint8)
    out[:, :w] = vals
    return (out[:, 0::4] << 6) | (out[:, 1::4] << 4) | (out[:, 2::4] << 2) | out[:, 3::4]


def encode_page(raster, ls, param=jobwriter.PARAM_4BPP, max_stream=MAX_BAND_STREAM, chunk=256):
    """(rows, ls) raster bytes -> band records (y, height, stream, last)"""
    enc = bandcodec.BandEncoder(param)
    recs = []

    def emit(y, h, last):
        stream = enc.encode(raster[y:y + h].tobytes(), ls, h, last)
        if len(stream) > max_stream and h > 1:
            emit(y, h // 2, False)
            emit(y + h // 2, h - h // 2, last)
        else:
            recs.append((y, h, stream, last))

    heights = jobwriter.band_heights(raster.shape[0], ls, chunk=chunk)
    y = 0
    for i, h in enumerate(heights):
        emit(y, h, i == len(heights) - 1)
        y += h
    return recs


def build_gray(pages_gray600, media="iso_a4_210x297mm", media_type="stationery", copies=1, collate=True, options=None,
               toner_save=False, centre=False, **job_kw):
    """8-bit gray pages at 600x600 dpi -> job bytes. Raster mode (jobwriter.job_bpp): 4 bpp 600x400 for
    full-size sheets on plain paper, 2 bpp 600x600 otherwise."""
    w600, h600 = jobwriter.PAPER[media][2:4]
    bpp = jobwriter.job_bpp(media, media_type, options)
    pages = []
    for gray in pages_gray600:
        page = fit_gray(gray, w600, h600, centre=centre)
        if bpp == 4:
            ls = ((w600 * 4 + 31) // 32) * 4
            raster = pack4(screens.render_native(page, -(-h600 * 2 // 3), toner_save=toner_save), ls)
            param, chunk = jobwriter.PARAM_4BPP, 256
        else:
            ls = ((w600 * 2 + 31) // 32) * 4
            raster = pack2(screens.render_600_2bpp(page, toner_save=toner_save), ls)
            param, chunk = jobwriter.PARAM_2BPP, 384
        pages.append([jobwriter.band_record(w600, h, y, s, last, param=param)
                      for (y, h, s, last) in encode_page(raster, ls, param, chunk=chunk)])
    return jobwriter.build_job(pages, media=media, media_type=media_type, bpp=bpp, copies=copies, collate=collate,
                            options=options, **job_kw)
