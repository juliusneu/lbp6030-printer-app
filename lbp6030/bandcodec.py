"""Band compression: BandEncoder and decode_band. Format: docs/PROTOCOL.md section 5.

The encoder is greedy: at every position it takes the longest copy from the five copy distances, or else a literal
(stash entry, zero byte or plain byte). It only sends what the printer is seen to receive: copies of at least
2 bytes that stay inside their row and the band, and stash entries written earlier in the same band."""
import numpy as np

XOR = 0x43
MAX_COUNT = 127 * 128 + 127          # longest copy: PREFIX 127 + count 127
MIN_COPY = 2
COPIES = ("R0", "R2", "R3", "R4", "R5")
OP = {"R0": (0b0, 1), "RB": (0b10, 2), "BY": (0b1101, 4), "R2": (0b1100, 4), "R3": (0b1110, 4),
      "R4": (0b11110, 5), "R5": (0b111110, 6), "ZB": (0b11111101, 8), "PF": (0b11111100, 8),
      "END": (0b11111110, 8)}
END_BAND, END_PAGE = 0b00, 0b01


class FormatError(ValueError):
    pass


def distances(param, ls):
    """initial copy distances in bytes from the 8-byte parameter block: R0 and R2 are rows * ls + offset"""
    s8 = lambda b: b - 256 if b > 127 else b  # noqa: E731
    return {"R0": param[2] * ls + s8(param[4]), "R2": param[3] * ls + s8(param[5]),
            "R3": param[0], "R4": param[6] | (param[7] << 8), "R5": param[1]}


class BitWriter:
    def __init__(self):
        self.acc = self.n = 0
        self.out = bytearray()

    def put(self, value, n):
        self.acc = (self.acc << n) | value
        self.n += n
        while self.n >= 8:
            self.n -= 8
            self.out.append(((self.acc >> self.n) & 0xFF) ^ XOR)
        self.acc &= (1 << self.n) - 1

    def number(self, v):
        """0..127: k ones, a zero and k+1 bits N = 2**(k+2) - 1 - v (k >= 1); 1 = 00, 2 = 011, 3 = 010; 0 = 111111"""
        if v == 0:
            self.put(0b111111, 6)
        elif v == 1:
            self.put(0b00, 2)
        elif v < 4:
            self.put(0b011 if v == 2 else 0b010, 3)
        else:
            k = v.bit_length() - 2
            self.put(((1 << k) - 1) << 1, k + 1)
            self.put((1 << (k + 2)) - 1 - v, k + 1)

    def pad32(self):
        k = -(len(self.out) * 8 + self.n) % 32
        self.put((1 << k) - 1, k)

    def tobytes(self):
        assert self.n == 0
        return bytes(self.out)


def run_lengths(a, dist, ls):
    """run[p] = number of bytes from p on that equal the bytes `dist` back, within p's row and the band"""
    n = len(a)
    eq = np.zeros(n, dtype=bool)
    if 0 < dist < n:
        eq[dist:] = a[dist:] == a[:n - dist]
    col = np.arange(ls, dtype=np.int32)
    stop = np.where(eq.reshape(-1, ls), ls, col)            # column of the first mismatch at or after p
    return (np.minimum.accumulate(stop[:, ::-1], axis=1)[:, ::-1] - col).ravel()


class BandEncoder:
    def __init__(self, param):
        self.param = bytes(param)

    def encode(self, band, ls, lines, last):
        """band: ls * lines raster bytes -> compressed data (without the band record around it)"""
        a = np.frombuffer(bytes(band), dtype=np.uint8)
        if len(a) != ls * lines:
            raise ValueError("band size does not match ls * lines")
        dist = distances(self.param, ls)
        dvals = sorted(set(dist.values()))
        slot = {d: i for i, d in enumerate(dvals)}
        runs = np.minimum(np.stack([run_lengths(a, d, ls) for d in dvals]), MAX_COUNT)
        longest = runs.max(axis=0)
        # bit i set: distance dvals[i] gives the longest copy here. Among those, the first command in COPIES order
        # is the cheapest (COPIES is sorted by opcode length).
        which = sum((runs[i] == longest).astype(np.int32) << i for i in range(len(dvals)))
        longest, which = longest.tolist(), which.tolist()
        data = bytes(band)
        bw = BitWriter()
        stash = []                    # bytes written in this band, most recent first
        p, n = 0, len(data)
        while p < n:
            length = longest[p]
            if length >= MIN_COPY:
                m = which[p]
                cmd = next(c for c in COPIES if m >> slot[dist[c]] & 1)
                k, r = divmod(length, 128)
                if k:
                    bw.put(*OP["PF"])
                    bw.number(k)
                bw.put(*OP[cmd])
                bw.number(r)
                if cmd == "R2":
                    dist["R0"], dist["R2"] = dist["R2"], dist["R0"]
                elif cmd == "R5":
                    dist["R3"], dist["R5"] = dist["R5"], dist["R3"]
                p += length
                continue
            b = data[p]
            if b in stash:
                i = stash.index(b)
                bw.put(*OP["RB"])
                bw.put(15 - i, 4)
                stash.pop(i)
            elif b == 0:
                bw.put(*OP["ZB"])
            else:
                bw.put(*OP["BY"])
                bw.put(b, 8)
            stash.insert(0, b)
            del stash[16:]
            p += 1
        bw.put(*OP["END"])
        bw.put(END_BAND, 2)
        bw.pad32()
        if last:
            bw.put(*OP["END"])
            bw.put(END_PAGE, 2)
            bw.pad32()
        return bw.tobytes()


# Decoder: validates jobs before they are sent, and is used by the tests.

class BitReader:
    def __init__(self, data):
        self.d = bytes(b ^ XOR for b in data)
        self.nbits = len(self.d) * 8
        self.pos = 0

    def bit(self):
        p = self.pos
        if p >= self.nbits:
            raise FormatError("stream ended before the end marker")
        self.pos = p + 1
        return (self.d[p >> 3] >> (7 - (p & 7))) & 1

    def bits(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | self.bit()
        return v

    def ones(self, limit):
        n = 0
        while n < limit and self.bit():
            n += 1
        return n


def read_number(br):
    k = br.ones(6)
    if k == 6:
        return 0
    if k == 0:
        return 1 if not br.bit() else (2 if br.bit() else 3)
    return (1 << (k + 2)) - 1 - br.bits(k + 1)


def decode_band(stream, ls, lines, param):
    """Decode one band to ls*lines raster bytes. Raises FormatError (a ValueError) on malformed data: copies before
    the band start or past its end, stash entries not written in this band, a missing end marker."""
    dist = distances(param, ls)
    total = ls * lines
    out = bytearray()
    br = BitReader(stream)
    stash = []
    prefix = 0

    def copy(cmd):
        nonlocal prefix
        n = prefix + read_number(br)
        prefix = 0
        start = len(out) - dist[cmd]
        if start < 0 or len(out) + n > total:
            raise FormatError("copy outside the band")
        if dist[cmd] >= n:
            out.extend(out[start:start + n])
        else:
            for j in range(n):
                out.append(out[start + j])

    def literal(b):
        out.append(b)
        stash.insert(0, b)
        del stash[16:]

    while True:
        op = br.ones(8)
        if op == 0:
            copy("R0")
        elif op == 1:                                   # REPBYTE: stash entry 15 - i, moved to the front
            k = 15 - br.bits(4)
            if k >= len(stash):
                raise FormatError("stash entry not written in this band")
            b = stash.pop(k)
            stash.insert(0, b)
            out.append(b)
        elif op == 2:
            if br.bit():
                literal(br.bits(8))
            else:
                copy("R2")
                dist["R0"], dist["R2"] = dist["R2"], dist["R0"]
        elif op == 3:
            copy("R3")
        elif op == 4:
            copy("R4")
        elif op == 5:
            copy("R5")
            dist["R3"], dist["R5"] = dist["R5"], dist["R3"]
        elif op == 6:
            if br.bit():
                literal(0)
            else:
                prefix = 128 * read_number(br)
        elif op == 7:                                   # END
            br.bits(2)
            break
        if len(out) > total:
            raise FormatError("band overrun")
    if len(out) != total:
        raise FormatError(f"band decoded to {len(out)} bytes, expected {total}")
    return bytes(out)
