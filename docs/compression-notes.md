# Band compression: notes and evidence

How each rule in [PROTOCOL.md](PROTOCOL.md) section 5 was established. The data was 1798 bands of synthetic test
pages (1656 at 4 bpp with 2480-byte rows, 142 at 2 bpp with 1240-, 876- or 624-byte rows), each with its decoded
raster. A decoder built on these rules also decodes 616 further bands exactly that were not used while working
them out.

- **[C n]**: confirmed on n bands. A decoder built on the rule reproduces the raster exactly on all n bands, and
  changing the rule breaks some of them.
- **[G]**: a guess. It fits the data, but the data cannot tell it apart from alternatives.

The format is a close relative of the compression in the public CAPT protocol description of the anticapt project
(github.com/agalakhov/anticapt). It has the same opcode alphabet, number code, stash and 32-bit padding, and differs
in four ways:

1. A PREFIX carries its multiplier in the ordinary number code, not in a 2-bit-order code.
2. Copy distances are two-dimensional: a number of rows times the row length, plus a byte offset.
3. The parameters arrive in a block at the start of every band.
4. A page-end word follows the last band of each page.

## 1. Container

`payload = params[8] | more[1] | length[4] | data[length] | 0x80`

| bytes | meaning | status |
|---|---|---|
| 0..7 | parameter block (section 4). One value per bit depth: 4 bpp `08 04 03 01 02 00 50 00`, 2 bpp `04 02 08 04 00 ff 50 00` | [C 1798] |
| 8 | `01` = more bands follow on this page, `00` = last band of the page | [C 1798] |
| 9..12 | `length`, u32 little-endian, always a multiple of 4 | [C 1798] |
| 13.. | compressed data (section 2) | [C 1798] |
| last byte | always `0x80`; meaning unknown | [C 1798] |

## 2. Bit layer

- Every data byte is XORed with `0x43`. The result is read as one bit stream, most significant bit first [C 1798].
- The commands end with END `00`. The stream is then padded with 1-bits to the next 32-bit boundary [C 1798].
- After the last band of a page one more 32-bit word follows: END `01` and 22 one-bits (raw bytes `bd 3c bc bc`)
  [C 21: every last-of-page band, and only those]. END codes `10` and `11` never occur.

## 3. Commands

An opcode is a run of 1-bits ended by a 0-bit, at most eight 1-bits. Some opcodes take one extra bit. The last
column counts each command in all 1798 bands.

| bits | command | effect | uses |
|---|---|---|---|
| `0` + num | LONGREP0 | copy `prefix + num` bytes from distance D0 | 136802 |
| `10` + 4 bits i | REPBYTE | output stash entry `15 - i` and move it to the front | 99878 |
| `110 1` + 8 bits | BYTE | output the byte and push it on the stash | 1904 |
| `110 0` + num | LONGREP2 | copy from D2, **then swap D0 and D2** | 3611 |
| `1110` + num | LONGREP3 | copy from D3 | 25257 |
| `11110` + num | LONGREP4 | copy from D4 | 11537 |
| `111110` + num | LONGREP5 | copy from D5, **then swap D3 and D5** | 7804 |
| `1111110 1` | ZEROBYTE | BYTE `00` | 1798 |
| `1111110 0` + num | PREFIX | `prefix = 128 * num`, used by the next copy only | 115893 |
| `11111110` + 2 bits | END | `00` end of band, `01` end of page | 1798 + 21 |
| `11111111` | filler | only seen as padding | 0 |

All of these are [C 1798].

**Copies**
- A copy goes byte by byte, so its source may overlap the bytes being written. This happens in 22674 copies, for
  example one byte repeated 2476 times from distance 4 [C 1798].
- A PREFIX is always directly followed by a copy, 115893 times out of 115893 [C 1798]. Its multipliers run from 1
  to 19, that is up to row length / 128.
- In all observed jobs, no copy reaches before the first byte of its band and no copy crosses a row boundary.
  Bands are independent of each other.
- No copy is shorter than 2 bytes: 0 of 233,002 copies. This project's encoder keeps to all of these.

**Stash**
- The stash is a move-to-front list of 16 addressable entries [C 1798]. A literal (BYTE or ZEROBYTE) goes to the
  front. REPBYTE `i` addresses entry `15 - i`, so `i = 15` is the most recent byte.
- Other rules break the decoding: with "leave in place" instead of "move to front" only 1466 bands decode exactly,
  and reading entry `i` instead of `15 - i` gives 1361.
- No band writes more than 16 literals, so the depth beyond 16 is never exercised. The decoder drops the oldest
  entry [G].
- A literal whose value is already in the stash never occurs [G: duplicated].
- No band reads an entry that was not written earlier in the same band. The initial contents are therefore
  unknown, and this project's decoder rejects such a read.

### Number code (`num`, 0..127)

- k one-bits and a 0-bit give the order k (0..5).
- Order 0: one more bit `0` gives 1. Otherwise two more bits follow: `11` gives 2, `10` gives 3.
- Order k ≥ 1: k+1 more bits N, and the value is `2^(k+2) - 1 - N`. Order 1 covers 4..7 and order 5 covers 64..127.
- Six one-bits (`111111`) mean 0. This occurs 274 times, always as the count after a PREFIX.

This is the anticapt description's code, and orders 0..5 and the zero code all occur [C 1798]. The difference is
that PREFIX uses the same code for its multiplier. For example, `10 11` = 4, `110 110` = 9 and `1110 1100` = 19
[C 1798].

## 4. Parameter block and copy distances

The parameter bytes are `b0 .. b7`. Distances count back from the current output position, and they reset at the
start of every band [C 1798].

| distance | value | 4 bpp | 2 bpp | status |
|---|---|---|---|---|
| D0 | `b2` rows + `int8(b4)` bytes | 3 rows + 2 bytes | 8 rows | 4 bpp [C 1656]; 2 bpp [G-strong] |
| D2 | `b3` rows + `int8(b5)` bytes | 1 row | 4 rows - 1 byte | 4 bpp [C 1656]; 2 bpp rows [C 142], offset [G] |
| D3 | `b0` | 8 bytes (16 px) | 4 bytes (16 px) | 4 bpp [C 1656] |
| D5 | `b1` | 4 bytes (8 px) | 2 bytes (8 px) | 4 bpp [C 1656] |
| D4 | `b6 + 256 * b7` | 80 | 80 | 4 bpp [C 1656] |

**The swaps.** Both are needed. Without the D0/D2 swap only 1236 of the 4 bpp bands and 128 of the 2 bpp bands
decode exactly. Without the D3/D5 swap the counts are 1381 and 128.

**4 bpp.** Every parameter is pinned: changing any one of them breaks some bands.

| change | exact bands (of 1656) |
|---|---|
| b2 = 1, 2 or 4 | 1563, 1586, 1585 |
| b4 = 0, 1, 3 or 4 | 1569 to 1584 |
| b3 = 2 or 3 | 1236 |
| b5 = -2..2 except 0 | 1236 |
| D4 = 160 | 1492 |
| b0 = 4, 7, 9 or 16 | 1495, 1495, 1586, 1585 |
| b1 = 2, 3, 5 or 8 | 1434, 1434, 1372, 1284 |

**2 bpp.** Only some parameters are pinned; where a value is not, the header byte is taken in the same role as in
4 bpp [G-strong].

| parameter | pinned? | evidence (exact bands of 142) |
|---|---|---|
| b3 = 4 | yes | 1, 2 or 3 give 141; 5 or more give 128 |
| b4 = 0 | yes | -1, 1 or 2 give 128 |
| b5 | no | -1 and 0 both give 142 |
| b0 | no | every band has exactly one LONGREP5, so D3 is never read in its initial state |
| b1 | partly | 3 or 4 give 128, but 1 also gives 142 |
| D4 | partly | 40 or 79 give 140, but 81 and 160 also give 142 |
| b2 | no | 1..8 all give 142 |

The observed jobs support b2 = 8: in blank 2 bpp bands, rows 1-7 are copied with LONGREP3, and LONGREP0 first
appears at row 8.
