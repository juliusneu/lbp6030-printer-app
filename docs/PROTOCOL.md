# LBP6030w print data format (raw TCP 9100)

These are interoperability facts about the data the printer accepts, observed in print data for this printer and
confirmed on the printer. The code in this repository was written from these facts. Facts confirmed on paper are
marked **(printer)**; the rest were confirmed offline by rebuilding observed jobs byte for byte and decoding every
band.

## 1. Transport

- Raw TCP to port **9100**, one way. The printer never answers on that socket. Close the sending side when done.
- Status is only available over SNMPv1 (community `public`): Host Resources MIB `hrPrinterStatus`,
  `hrPrinterDetectedErrorState` (bit 0x40 = no paper, 0x08 door open, 0x04 jammed, ...), Printer MIB page counter
  `prtMarkerLifeCount` and toner `prtMarkerSupplies`. **(printer)**
- With an empty tray the printer holds the job, reports no-paper once it tries to feed, and prints after paper is
  loaded and the Paper key is pressed. **(printer)**

## 2. Packets

Every packet: a 20-byte header, big-endian, then the payload.

```
CD CA 10 <flags> <op:u16> 00 01 <payload length:u16> FF FF 00 00 00 00 00 00 00 00   payload...
```
`flags` = 0x02 on page-data packets except the very last data packet (0x00); 0x00 on control packets.
Packets are at most 4096 bytes including the header.

A job is this sequence (values for A4, plain paper, one copy):
```
006B  job description: 00 0a 00 f0 00 01 01 01 30 00 03 03 00 00, then attributes [id:u16][len:u16][value]:
        0004 job name (cut to 32 bytes), 0117 job name (full), 0006 owner   (strings: 03 F2 <len:u8> <bytes>)
        000D 04, 000C 32, 0118 64 bytes (16-byte client id, 16 zero bytes, host name in 16 bytes twice),
        0009 8-byte time (year:12 month:4 day:5 weekday:3, 0:8 | hour:8 minute:8 ms-in-minute:16), 013F computer
0014  00000000
0015  one packet each: 08b3=02 | 082b=<job name, cut to 32 bytes> | 07d7=<copy count u16, see 7> | 07d8=0f |
      07d9=1700000000 | 084a=04 | 07da=00 | 08a5=fefefffe 00x8 03
0017  00000000
0018  one packet each: 002e=830000 | 08b5=02 | 086e=010b0000 |
      07ed=<media type> fe 00 <paper size code> 00x8 03 | 07d7=0001 | 07e0=04 |
      003a=08 0258 0190 (600 x 400 dpi; 08 0258 0258 in the 600 x 600 mode)
001D  option strings "00 00 00 00 00 8f 00 <flag> <len>NAME<len>VALUE", in this order:
      PAPER-SAVE=OFF (flag 83) | CHKPAPERSIZE=OFF (only if paper-size detection is off) |
      PRINT-SP-LEVEL=MODE1 (OFF, MODE2..4) | PRINT-SP-LEVEL2=OFF (MODE1..3) | BARCODE=ON or WHITEGAP=ON (optional) |
      WRINKLE-ESCAPE-MODE=OFF (MODE1, MODE2)            all flags 0xff except PAPER-SAVE
001A  page data: payload = 01 + up to 4075 bytes of the page description (section 3); per page: first the
      session header (first page only) and page header, then an empty data packet (payload 01), then the
      page body. A writer must flush (send the partial packet, then an empty data packet) before the bytes
      written since the last flush would exceed 65536. After the last page: data "12 11", then an empty data
      packet with flags 0x00.
0019  (empty)
0015  01 13 <pages to output:u32>
0016  (empty)
0012  01 13 <pages to output:u32>
0012  07 d6 <total job size in bytes:u32>   (from the first byte of 006B to the end of the 0013 packet)
0013  00
```

## 3. Page description

Token stream: bytes 0xC0..0xFF are attribute tags followed by a value (an immediate 0x00..0x7F, or a type byte
`80`/`A0` + 1 byte, `81`/`A1` + 2, `84`/`A4` + 2, `85`/`A5` + 4; A-types are signed); `9C LL` / `9D LLLL` /
`9E LLLLLLLL` start a data block; other bytes are operators.

```
01  C1=85:10001089  C2=0  D8=84:0258  DD=80:C8  F0=84:0800  [E9=84:0190 in the 600x400 mode only]
02  C3=<paper size code, page description>  C5=0  C6=0
51  F2=0
-- per page --
03  E7=85:<width px at 600 dpi><height px at 600 dpi>  DE=80:00  C8=0  CA=A1:0000  CB=0
61  E6=80:<bits per pixel: 04 or 02>  E5=0
    62 band ... (repeated)
13
-- end of job --
12 11
```
Band record:
```
62  E3=85:<width px><rows>  E8=<0><y> (A1 pair of s8, or A5 pair of s16)  E1=03
    D7=<n> (always typed, A0 or A4)   9C <n> | 9D <n:u16>   then n bytes:
      8 codec parameter bytes, then 01 (00 on the page's last band), then the stream length u32 LE,
      the stream (a multiple of 4 bytes), and 80
```
**Keep every band record below 32768 bytes** (the 16-bit form of D7). A page with larger bands was accepted on
the socket and silently discarded by the printer. **(printer)** Splitting a band into
fewer rows is fine; band records carry their own y and height. **(printer)**

## 4. Raster modes

| Mode | Used for | Raster | Codec parameters |
|---|---|---|---|
| 4 bpp, 600 x 400 dpi | A4, Letter, Legal, Oficio, Foolscap, 16K on plain or lightweight paper or transparency | 4 bits per pixel, 600 dpi across and 400 dpi down; left pixel in the high nibble; 0 = white, 14 = black (15 never used) | `08 04 03 01 02 00 50 00` |
| 2 bpp, 600 x 600 dpi | A5, B5, Executive, postcards, envelopes; heavyweight paper, cardstock, labels; WRINKLE-ESCAPE-MODE=MODE2 | 2 bits per pixel; left pixel in the two high bits; values 0 and 3 only | `04 02 08 04 00 ff 50 00` |

Row length in bytes = ceil(width x bpp / 32) x 4. Pages are cut into chunks of 384 rows at 600 dpi (256 rows in
the 400-dpi mode), each split into bands of floor(131072 / row bytes) rows. No margins are applied by the data;
the printer leaves a ~5 mm border unprinted.

In the 4 bpp mode, a pure black/white 600 x 600 image maps onto the 400-dpi rows by area: each output row covers
1.5 input rows (row k covers input rows [1.5k-0.5, 1.5k+1)), giving the levels 0, 4, 9, 14. The printer also
renders the intermediate levels, which this project uses to grow halftone dots smoothly. **(printer)**

The job has no setting for toner save, brightness or contrast; those are done when rendering the raster.

## 5. Band compression

Worked out from band data of synthetic test pages with their decoded rasters and the public CAPT protocol
description of the anticapt project (github.com/agalakhov/anticapt). A decoder built on it reproduces 2414 test
bands exactly, including 616 not used while working it out. The evidence for each rule is in
[compression-notes.md](compression-notes.md).

The compressed data of a band (section 3) is a bit stream: XOR every byte with `0x43`, then read the bits most
significant first. An opcode is a run of 1-bits ended by a 0-bit (at most eight 1-bits); some take one more bit.

```
0              num   LONGREP0   copy num bytes from distance D0
10       iiii        REPBYTE    output stash entry 15-i and move it to the front of the stash
1101     bbbbbbbb    BYTE       output b and insert it at the front of the stash (16 entries)
1100           num   LONGREP2   copy from D2, then swap D0 and D2
1110           num   LONGREP3   copy from D3
11110          num   LONGREP4   copy from D4
111110         num   LONGREP5   copy from D5, then swap D3 and D5
11111101             ZEROBYTE   BYTE 00
11111100       num   PREFIX     the next copy is 128 * num bytes longer
11111110       ee    END        00 = end of band, 01 = end of page
11111111             filler
```
`num` (0..127): k 1-bits and a 0-bit give the order k (0..5). Order 0: `0` = 1, `11` = 2, `10` = 3. Order k >= 1:
k+1 more bits N, value 2^(k+2) - 1 - N. Six 1-bits (`111111`) mean 0.

Copies go byte by byte, so they may overlap their source. Distances count back from the current position. They
come from the parameter bytes b0..b7 of the band record and are reset for every band:

| distance | value | 4 bpp | 2 bpp |
|---|---|---|---|
| D0 | b2 rows + int8(b4) bytes | 3 rows + 2 bytes | 8 rows |
| D2 | b3 rows + int8(b5) bytes | 1 row | 4 rows - 1 byte |
| D3 | b0 | 8 | 4 |
| D5 | b1 | 4 | 2 |
| D4 | b6 + 256 * b7 | 80 | 80 |

After the END of a band the stream is padded with 1-bits to a 32-bit boundary. The last band of a page adds one
more 32-bit word: END `01`, then 1-bits.

**What the printer is seen to receive.** This project's encoder keeps to these forms:
- Copies are at least 2 bytes long; none of 233,002 copies in observed jobs is shorter.
- Copies stay inside their row and inside the band.
- A stash entry is used only after it was written in the same band.
- A PREFIX always comes directly before a copy.

## 6. Paper sizes and media types

| PWG media name | size code (0018 07ed) | size code (page description) | width x height px @600 dpi | mode | media type on plain paper |
|---|---|---|---|---|---|
| iso_a4_210x297mm | 2 | 0 | 4958 x 7016 | 4 bpp | 3 |
| na_letter_8.5x11in | 13 | 100 | 5100 x 6600 | 4 bpp | 3 |
| na_legal_8.5x14in | 12 | 102 | 5100 x 8400 | 4 bpp | 3 |
| om_oficio_216x318mm | 62 | 113 | 5100 x 7508 | 4 bpp | 3 |
| na_foolscap_8.5x13in | 61 | 112 | 5100 x 7791 | 4 bpp | 3 |
| om_mexico-oficio_216x341mm | 210 | 98 | 5100 x 8058 | 4 bpp | 3 |
| om_brazil-oficio_216x356mm | 204 | 96 | 5100 x 8383 | 4 bpp | 3 |
| om_16k_195x270mm | 212 | 120 | 4608 x 6374 | 4 bpp | 3 |
| iso_a5_148x210mm | 3 | 2 | 3499 x 4958 | 2 bpp | 3 |
| jis_b5_182x257mm | 7 | 11 | 4299 x 6074 | 2 bpp | 3 |
| na_executive_7.25x10.5in | 10 | 101 | 4350 x 6300 | 2 bpp | 3 |
| jpn_hagaki_100x148mm | 14 | 20 | 2358 x 3499 | 2 bpp | 3 |
| jpn_oufuku_148x200mm | 15 | 21 | 3499 x 4724 | 2 bpp | 3 |
| om_4up-postcard_200x297mm | 55 | 22 | 4724 x 7016 | 2 bpp | 3 |
| na_number-10_4.125x9.5in | 22 | 35 | 2483 x 5691 | 2 bpp | 6 |
| iso_c5_162x229mm | 21 | 37 | 3824 x 5408 | 2 bpp | 6 |
| iso_dl_110x220mm | 24 | 39 | 2599 x 5199 | 2 bpp | 6 |
| na_monarch_3.875x7.5in | 23 | 36 | 2316 x 4491 | 2 bpp | 6 |
| jpn_chou3_120x235mm | 193 | 34 | 2833 x 5550 | 2 bpp | 6 |
| om_yougata-naga3_120x235mm | 213 | 40 | 2833 x 5550 | 2 bpp | 6 |

Media type codes (0018 07ed, byte 2), by IPP media-type keyword: stationery 3, stationery-lightweight 10,
transparency 4 (4 bpp mode); stationery-heavyweight 8, cardstock 47, labels 7 (2 bpp mode); 6 = envelope (the
envelope sizes on plain paper).

## 7. Copies

- Collated copies: the complete job is sent again for each copy (identical bytes).
- Uncollated copies: one job with `0015 07d7` = the copy count and both page counts = pages x copies; the
  printer repeats each page.
