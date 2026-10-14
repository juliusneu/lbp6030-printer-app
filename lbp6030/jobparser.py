"""Parse jobs: packets, the page description inside them, and band records (docs/PROTOCOL.md)."""
import struct

VALUE_LEN = {0x80: 1, 0xA0: 1, 0x81: 2, 0xA1: 2, 0x84: 2, 0xA4: 2, 0x85: 4, 0xA5: 4}


def packets(buf):
    i = 0
    while i < len(buf):
        if buf[i:i + 2] != b"\xcd\xca":
            raise ValueError(f"bad packet magic at {i:#x}")
        ver, flags, op, unk, ln = struct.unpack(">BBHHH", buf[i + 2:i + 10])
        yield dict(off=i, ver=ver, flags=flags, op=op, unk=unk, hdr=buf[i:i + 20], payload=buf[i + 20:i + 20 + ln])
        i += 20 + ln


def pdl_from_job(buf):
    out = bytearray()
    for p in packets(buf):
        if p["op"] == 0x1A:
            assert p["payload"][:1] == b"\x01"
            out += p["payload"][1:]
    return bytes(out)


def tokens(d):
    i = 0
    while i < len(d):
        b = d[i]
        if b >= 0xC0:
            t = d[i + 1]
            if t < 0x80:
                yield ("attr", i, b, None, t)
                i += 2
            else:
                n = VALUE_LEN.get(t)
                if n is None:
                    raise ValueError(f"unknown value type {t:#x} at {i:#x}")
                raw = d[i + 2:i + 2 + n]
                yield ("attr", i, b, t, raw)
                i += 2 + n
        elif b in (0x9C, 0x9D, 0x9E):
            k = {0x9C: 1, 0x9D: 2, 0x9E: 4}[b]
            n = int.from_bytes(d[i + 1:i + 1 + k], "big")
            yield ("data", i, b, n, d[i + 1 + k:i + 1 + k + n])
            i += 1 + k + n
        else:
            yield ("op", i, b, None, None)
            i += 1


def attr_int(t, raw):
    if t is None:
        return raw
    return int.from_bytes(raw, "big")


def parse_bands(d):
    """Bands: op 0x62 followed by attrs e3 (WxH), e8 (origin), e1 (method), d7 (len), then 9C data."""
    res = []
    toks = list(tokens(d))
    i = 0
    while i < len(toks):
        kind, off, b, t, v = toks[i]
        if kind == "op" and b == 0x62:
            band = dict(off=off)
            j = i + 1
            data = bytearray()
            while j < len(toks) and toks[j][0] in ("attr", "data"):
                k2, o2, b2, t2, v2 = toks[j]
                if k2 == "attr":
                    band[f"a{b2:02x}"] = (t2, v2)
                else:
                    data += v2
                j += 1
            wh = band["ae3"][1]
            band["w"], band["h"] = struct.unpack(">HH", wh)
            t8, v8 = band["ae8"]
            if len(v8) == 2:
                band["x"], band["y"] = 0, int.from_bytes(v8, "big")
            else:
                band["x"], band["y"] = struct.unpack(">HH", v8)
            band["method"] = band["ae1"][1]
            band["len"] = attr_int(*band["ad7"])
            band["data"] = bytes(data)
            assert band["len"] == len(data), (band["len"], len(data))
            d0 = band["data"]
            band["param"] = d0[:8]
            band["flag"] = d0[8]
            band["nn"] = struct.unpack("<I", d0[9:13])[0]
            band["stream"] = d0[13:13 + band["nn"]]
            band["trailer"] = d0[13 + band["nn"]:]
            res.append(band)
            i = j
        else:
            i += 1
    return res
