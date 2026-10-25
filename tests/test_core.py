"""Self-contained tests (no printer, no vendor software): python3 -m unittest discover tests"""
import http.client
import os
import struct
import sys
import tempfile
import threading
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lbp6030 import bandcodec, decode, device, ipp, jobwriter, media, pipeline, render, server  # noqa: E402


class Codec(unittest.TestCase):
    def test_roundtrip(self):
        rng = np.random.default_rng(7)
        for trial in range(40):
            ls = int(rng.choice([8, 40, 620, 2480]))
            lines = int(rng.integers(1, 25))
            kind = trial % 4
            if kind == 0:
                band = rng.integers(0, 256, ls * lines, dtype=np.uint8)
            elif kind == 1:
                band = np.tile(rng.integers(0, 16, ls, dtype=np.uint8), lines)
            elif kind == 2:
                band = np.zeros(ls * lines, np.uint8)
                band[::7] = 0xEE
            else:
                band = np.tile(np.array([0x0E, 0xE0, 0, 0x44], np.uint8), ls * lines // 4 + 1)[:ls * lines]
            for param in (jobwriter.PARAM_4BPP, jobwriter.PARAM_2BPP):
                s = bandcodec.BandEncoder(param).encode(band.tobytes(), ls, lines, trial % 2 == 0)
                self.assertEqual(bandcodec.decode_band(s, ls, lines, param), band.tobytes())


def page_with_content(w, h):
    g = np.full((h, w), 255, np.uint8)
    g[600:1200, 600:1200] = 0            # 1-inch black square
    g[1500:1800, 600:3000] = 128         # 50 % gray bar
    g[1600:1700, 700:1500] = 255         # white "text" knocked out of the bar
    g[2000:2006, 600:3000] = 217         # thin light rule
    return g


class Jobs(unittest.TestCase):
    def build(self, media="iso_a4_210x297mm", **kw):
        w, h = jobwriter.PAPER[media][2:4]
        job = pipeline.build_gray([page_with_content(w, h)], media=media, job_name="test", user="test", **kw)
        return job

    def test_a4_plain_4bpp(self):
        problems, info = device.validate(self.build())
        self.assertEqual(problems, [])
        self.assertEqual(info["sizes"], [(4958, 4678, 4)])

    def test_a5_and_heavy_use_2bpp(self):
        for media, mtype in (("iso_a5_148x210mm", "stationery"), ("iso_a4_210x297mm", "stationery-heavyweight"),
                             ("na_number-10_4.125x9.5in", "stationery")):
            problems, info = device.validate(self.build(media, media_type=mtype))
            self.assertEqual(problems, [])
            self.assertEqual(info["sizes"][0][2], 2)

    def test_copies(self):
        _, info = device.validate(self.build(copies=3))
        self.assertEqual((info["sheets"], info["collated"]), (3, True))
        _, info = device.validate(self.build(copies=3, collate=False))
        self.assertEqual((info["sheets"], info["collated"]), (3, False))

    def test_text_stays_solid_black(self):
        pg = decode.decode_job_bytes(self.build())[0]
        a = decode.to_array(pg)
        self.assertTrue((a[400 + 20:800 - 20, 600 + 20:1200 - 20] == 14).all())   # square at 600 dpi rows 600..1200

    def test_band_size_limit(self):
        for parts in pipeline.encode_page(np.random.default_rng(1).integers(0, 256, (200, 2480), dtype=np.uint8), 2480):
            self.assertLessEqual(len(parts[2]), pipeline.MAX_BAND_STREAM)


class Raster(unittest.TestCase):
    @staticmethod
    def pack_line(row):
        out = bytearray()
        i = 0
        while i < len(row):
            j = i
            while j + 1 < len(row) and row[j + 1] == row[i] and j - i < 127:
                j += 1
            if j > i:
                out += bytes([j - i, row[i]])
                i = j + 1
            else:
                out += bytes([0, row[i]])
                i += 1
        return out

    def sample(self):
        w, h = 37, 5
        g = (np.arange(w * h).reshape(h, w) * 7 % 256).astype(np.uint8)
        body = bytearray()
        for y in range(h):
            body += bytes([0]) + self.pack_line(g[y].tobytes())
        return g, body

    def test_pwg(self):
        g, body = self.sample()
        hdr = bytearray(1796)
        struct.pack_into(">II", hdr, 276, 600, 600)
        struct.pack_into(">II", hdr, 372, g.shape[1], g.shape[0])
        struct.pack_into(">I", hdr, 388, 8)
        struct.pack_into(">I", hdr, 400, 18)
        self.assertTrue(np.array_equal(render.read_pwg(b"RaS2" + bytes(hdr) + bytes(body))[0][0], g))

    def test_urf(self):
        g, body = self.sample()
        hdr = bytearray(32)
        hdr[0] = 8
        struct.pack_into(">III", hdr, 12, g.shape[1], g.shape[0], 600)
        data = b"UNIRAST\x00" + struct.pack(">I", 1) + bytes(hdr) + bytes(body)
        self.assertTrue(np.array_equal(render.read_urf(data)[0][0], g))


class Media(unittest.TestCase):
    def test_names(self):
        self.assertEqual(media.paper_from_ipp("iso_a4_210x297mm,tray-1"), "iso_a4_210x297mm")
        self.assertEqual(media.paper_from_ipp("Env10"), "na_number-10_4.125x9.5in")
        self.assertEqual(media.media_type_from_ipp("envelope"), "stationery")
        self.assertLessEqual(set(server.MEDIA), set(jobwriter.PAPER))
        self.assertEqual(server.paper_for_size(21000, 29700), "iso_a4_210x297mm")


class Ipp(unittest.TestCase):
    def test_roundtrip(self):
        size = {"x-dimension": (ipp.INTEGER, [21000]), "y-dimension": (ipp.INTEGER, [29700])}
        col = {"media-size": (ipp.BEGIN_COLLECTION, [size]), "media-type": (ipp.KEYWORD, ["stationery"])}
        msg = ipp.response(7, 0x0002, [(ipp.JOB, [(ipp.BEGIN_COLLECTION, "media-col", [col]),
                                                  (ipp.KEYWORD, "requested-attributes", ["a", "b"])])])
        req = ipp.Request(msg + b"DATA")
        self.assertEqual((req.operation, req.request_id, req.data), (2, 7, b"DATA"))
        self.assertEqual(req.values("requested-attributes"), ["a", "b"])
        self.assertEqual(req.get("media-col")["media-size"][1][0]["x-dimension"][1], [21000])


class Server(unittest.TestCase):
    def call(self, port, op, attrs, data=b""):
        ops = [(ipp.CHARSET, "attributes-charset", ["utf-8"]), (ipp.LANGUAGE, "attributes-natural-language", ["en"]),
               (ipp.URI, "printer-uri", [f"ipp://localhost:{port}/ipp/print"])] + attrs
        body = ipp.response(1, op, [])[:-1]      # header + charset/language group without the end tag
        body = bytes(body[:8]) + bytes([ipp.OPERATION]) + b"".join(ipp.encode_attr(*a) for a in ops) + bytes([ipp.END])
        conn = http.client.HTTPConnection("localhost", port, timeout=10)
        conn.request("POST", "/ipp/print", body + data, {"Content-Type": "application/ipp"})
        return ipp.Request(conn.getresponse().read())

    def test_create_job_two_step_send(self):
        """CUPS clients send the document with last-document=false, then an empty Send-Document"""
        with tempfile.TemporaryDirectory() as td:
            httpd, port = self.start(td)
            try:
                job_id = self.call(port, 0x0005, []).get("job-id")
                one = self.call(port, 0x0006, [(ipp.INTEGER, "job-id", [job_id]), (ipp.BOOLEAN, "last-document", [False]),
                                               (ipp.MIME, "document-format", ["image/urf"])], self.urf())
                two = self.call(port, 0x0006, [(ipp.INTEGER, "job-id", [job_id]), (ipp.BOOLEAN, "last-document", [True])])
                self.assertEqual((one.operation, two.operation), (ipp.OK, ipp.OK))
                self.assertEqual(self.wait(port, job_id), server.COMPLETED)
            finally:
                httpd.shutdown()
                httpd.server_close()

    def start(self, dry_run):
        printer = server.Printer("127.0.0.1", "test", 0, dry_run=dry_run)
        httpd = server.make_server(0)
        httpd.printer = printer
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, httpd.server_address[1]

    @staticmethod
    def urf():
        g, body = Raster().sample()
        hdr = bytearray(32)
        hdr[0] = 8
        struct.pack_into(">III", hdr, 12, g.shape[1], g.shape[0], 600)
        return b"UNIRAST\x00" + struct.pack(">I", 1) + bytes(hdr) + bytes(body)

    def wait(self, port, job_id):
        for _ in range(100):
            state = self.call(port, 0x0009, [(ipp.INTEGER, "job-id", [job_id])]).get("job-state")
            if state in (server.COMPLETED, server.ABORTED):
                return state
            time.sleep(0.2)
        return state

    def test_print_job_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            httpd, port = self.start(td)
            try:
                attrs = self.call(port, 0x000B, [])
                self.assertEqual(attrs.operation, ipp.OK)
                self.assertIn("image/urf", attrs.values("document-format-supported"))
                job = self.call(port, 0x0002, [(ipp.MIME, "document-format", ["image/urf"])], self.urf())
                self.assertEqual(job.operation, ipp.OK)
                self.assertEqual(self.wait(port, job.get("job-id")), server.COMPLETED)
                jobs = [f for f in os.listdir(td) if f.endswith(".job")]
                self.assertEqual(len(jobs), 1)
                problems, _ = device.validate(open(os.path.join(td, jobs[0]), "rb").read())
                self.assertEqual(problems, [])
            finally:
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    unittest.main()
