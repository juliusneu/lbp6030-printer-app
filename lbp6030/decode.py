"""Decode jobs back to page rasters (0 = white) with bandcodec.decode_band, for validation and tests."""
import numpy as np

from . import bandcodec, jobparser


def line_bytes(width, bpp):
    return ((width * bpp + 31) // 32) * 4


def page_split(pdl):
    """yield (page_attrs, bpp, bands) per page: a page = op 0x03 ... op 0x13"""
    pages = []
    cur = None
    bpp = None
    toks = list(jobparser.tokens(pdl))
    for i, (kind, off, b, t, v) in enumerate(toks):
        if kind == "op" and b == 0x03:
            cur = dict(start=off)
        if kind == "attr" and b == 0xE6 and cur is not None:
            bpp = int.from_bytes(v, "big")
        if kind == "op" and b == 0x13 and cur is not None:
            cur["end"] = off
            cur["bpp"] = bpp
            pages.append(cur)
            cur = None
    for p in pages:
        sub = pdl[p["start"]:p["end"] + 1]
        p["bands"] = jobparser.parse_bands(sub)
    return pages


def decode_pdl(pdl):
    res = []
    for p in page_split(pdl):
        bands = p["bands"]
        w = bands[0]["w"]
        bpp = p["bpp"]
        lb = line_bytes(w, bpp)
        raw = bytearray()
        for b in bands:
            assert b["w"] == w and b["y"] == len(raw) // lb, (b["y"], len(raw) // lb)
            raw += bandcodec.decode_band(b["stream"], lb, b["h"], b["param"])
        res.append(dict(w=w, h=len(raw) // lb, bpp=bpp, lb=lb, raw=bytes(raw)))
    return res


def decode_job_bytes(job):
    return decode_pdl(jobparser.pdl_from_job(job))


def to_array(pg):
    """numpy uint8 array (h, w) of pixel values 0..(2**bpp-1)"""
    w, h, lb, bpp = pg["w"], pg["h"], pg["lb"], pg["bpp"]
    a = np.frombuffer(pg["raw"], dtype=np.uint8).reshape(h, lb)
    per = 8 // bpp
    planes = [(a >> (8 - bpp * (k + 1))) & ((1 << bpp) - 1) for k in range(per)]
    px = np.stack(planes, axis=2).reshape(h, lb * per)
    return px[:, :w]
