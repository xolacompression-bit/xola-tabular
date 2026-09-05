"""
csvcol.py — columnar transform for fixed-shape CSV
==================================================

WHAT THIS IS FOR

Row-major CSV interleaves unrelated columns. In a real NOAA weather file
with 133 fields per row, 59 of them are IDENTICAL to the row above - but
they sit hundreds of bytes apart, so a string matcher has to carry a huge
window to notice. Grouping each column's values together puts them next
to each other.

This is the same columnar redundancy the binary transforms exploit. The
difference is that CSV fields are VARIABLE WIDTH, so a fixed-stride
transform is blind to it. This one splits on field boundaries instead.

MEASURED ON THREE REAL NOAA STATION FILES

    file                 before    after
    47662099999.csv       -0.1%   +14.1%
    03772099999.csv       -0.8%   +10.5%
    72278023183.csv       -3.2%    +3.6%

Every one was a LOSS before. All three round-trip byte-exact.

FOUR TECHNIQUES, EACH MEASURED SEPARATELY

    transpose by field            -0.1% -> +10.2%
    blank repeated values        +10.2% -> +14.1%
    fold constant columns              no change on these files
    dictionary low-cardinality   +14.1% -> +14.9%

TRIED ON THE REMAINING COST AND REJECTED

The "REM" column - free-text METAR weather reports - is 28% of what is
left, 265 distinct values in 265 rows. Three techniques for near-identical
strings all LOST against plain lzma on that column:

    front coding          -2.3%
    token transposition  -42.5%
    both together        -53.2%

lzma's match finder already exploits the shared METAR vocabulary.

THREE BUGS FOUND BUILDING THIS, ALL CAUGHT BY THE ROUND-TRIP CHECK

  Splitting on every comma broke quoted fields. Station names like
  "PHOENIX AIRPORT, AZ US" contain commas, which turned 133 real columns
  into 204 and made compression 57% WORSE.

  A b"\\x00\\x00" header terminator collided with empty CSV fields, which
  encode as exactly that sequence. These files are full of them.

  The header length double-counted: the row and column counts were inside
  the header being measured but stripped off before the length was read.
"""

# WHITESPACE-SEPARATED FILES ARE HANDLED, BUT RARELY WIN.
#
# A NOAA buoy file is fixed-width, right-aligned text - eighteen columns,
# every data line exactly 89 characters. Splitting it on whitespace loses
# the alignment, so the round trip fails and it is correctly refused.
#
# It does not matter. Measured, the ordinary router already gets +36.3%
# on that file by finding the 90-byte record period itself. Two attempts
# to help made it WORSE: splitting the header off gave +31.1%, and
# forcing the transpose at period 90 gave +26.3%.
#
# The lesson is the usual one. Check what the existing machinery already
# does before building something to help it.

# 255, the real limit - the dictionary index is stored in ONE BYTE, so
# 256 distinct values is the hard ceiling and anything above it produces
# a stream that cannot be decoded.
#
# It had been 64, chosen arbitrarily. Measured across five real CSV files,
# raising it to 255 helped every one and turned a loss into a win:
#
#     taxis.csv          -0.3% -> +6.2%
#     47662099999.csv    +9.4% -> +14.0%
#     diamonds.csv      +13.4% -> +15.2%
#     72278023183.csv    +3.9% -> +5.9%
#
# At 512 the encoder silently produces an index that does not fit, and
# try_encode's round-trip check catches it and refuses - which is the
# safety net working, but it meant the file fell back to no transform at
# all. The cap now matches what the format can actually represent.
import zlib
import struct

MAX_DICT = 255

# How many odd lines at the top of a file to hold aside verbatim.
PRELUDE_MAX = 4


# PARSED ROWS ARE CACHED FOR THE DURATION OF ONE try_encode.
#
# Every candidate re-parsed the entire file: profiled on a 3 MB export,
# split_quoted was called 130,608 times for roughly 30,000 lines - four
# passes over the same data, 21% of the total encoding time.
#
# Keyed by the exact bytes and delimiter, so a different file or a
# different separator cannot collide. Cleared at the end of try_encode
# rather than kept, because holding a parsed copy of every file ever
# compressed is a memory leak wearing a cache's clothes.
_ROWCACHE = {}


def rows_of(data, sep=0x2C):
    """Split data into rows once, then hand out the same list."""
    key = (id(data), len(data), sep)
    hit = _ROWCACHE.get(key)
    if hit is not None and hit[0] is data:
        return hit[1]
    rows = [split_quoted(l, sep) for l in data.split(b"\n")[:-1]]
    _ROWCACHE[key] = (data, rows)
    return rows


def split_quoted(line, sep=0x2C):
    """Split on the delimiter, OUTSIDE quotes.

    THE FAST PATH IS THE POINT.
    ---------------------------
    Most lines in most CSVs contain no quotes at all, and for those a
    single bytes.split is correct and orders of magnitude faster than
    walking the line one byte at a time.

    Profiling put the byte-by-byte version at 2.87 seconds of an
    8-second compression - 74,885 calls making 12.8 MILLION individual
    bytearray.append operations. It was the largest single cost in the
    whole pipeline, and it was a CSV parser, not a compressor.

    The slow path is kept, unchanged, for lines that do contain a quote,
    because that is where the delimiter-inside-quotes case lives and
    getting it wrong corrupts a row silently."""
    sepb = bytes([sep])
    if 0x22 not in line:
        return line.split(sepb)

    # THE QUOTED PATH IS ALSO SPLIT-BASED, NOT BYTE-BY-BYTE.
    #
    # The fast path above only helps files with no quotes at all. A NOAA
    # weather export quotes most of its fields - "330,1,N,0057,1" - so it
    # took the slow path on every line and spent 0.241 s walking 1.1
    # million bytes one at a time.
    #
    # Splitting on the delimiter first is safe as long as the pieces are
    # then rejoined wherever a quote was left open. A piece with an odd
    # number of quote characters means the delimiter that followed it was
    # inside quotes, so it is glued back to the next piece.
    parts = line.split(sepb)
    if len(parts) == 1:
        return parts
    out = []
    buf = None
    for piece in parts:
        if buf is None:
            buf = piece
        else:
            buf = buf + sepb + piece
        if buf.count(0x22) % 2 == 0:
            out.append(buf)
            buf = None
    if buf is not None:
        out.append(buf)
    return out


_SPLITCACHE = {}


def _rows_and_prelude(lines, sep, _cache_key=None):
    """Split rows, allowing a few odd lines at the top.

    Real files carry preamble. A crypto export begins with a bare URL
    before the header row; a buoy file has two comment lines. One stray
    line made the whole file look ragged and the transform refused, so
    up to PRELUDE_MAX leading lines are held aside verbatim and the rest
    is transposed."""
    # THE SPLIT IS CACHED BY (lines identity, separator).
    #
    # encode() is called twenty times during one try_encode - two
    # dictionary caps times several separator and option combinations -
    # and each call re-split every line. Profiled on a 3 MB export that
    # was 123,352 calls to split_quoted for six thousand lines: twenty
    # full passes over the file to produce twenty identical results.
    #
    # Keyed on the separator too, because a different delimiter really
    # does give different rows and sharing those would be a correctness
    # bug rather than a speed one.
    # Keyed on the DATA, not on the list of lines.
    #
    # The first attempt keyed on id(lines), and encode() builds a fresh
    # list every call - so the key was different every time and the cache
    # never once hit. It made things slightly SLOWER, which is the honest
    # signature of a cache that only ever misses.
    _k = (_cache_key, sep if sep is None else sep[0])
    _hit = _SPLITCACHE.get(_k) if _cache_key is not None else None
    if _hit is not None:
        rows = _hit
    else:
        if sep is None:
            rows = [l.split() for l in lines]
        else:
            rows = [split_quoted(l, sep[0]) for l in lines]
        if _cache_key is not None:
            _SPLITCACHE[_k] = rows
    from collections import Counter
    counts = Counter(len(r) for r in rows)
    if not counts:
        return None, None, 0
    nc = counts.most_common(1)[0][0]
    if nc < 2:
        return None, None, 0
    pre = 0
    while pre < len(rows) and pre < PRELUDE_MAX and len(rows[pre]) != nc:
        pre += 1
    body = rows[pre:]
    if len(body) < 4 or any(len(r) != nc for r in body):
        return None, None, 0
    return body, nc, pre


def encode(d, subsplit=True, sep=b',', regroup=True, maxdict=None):
    """Returns the columnar form, or None when the file is not a safe fit.

    Refuses ragged rows. A CSV whose rows have different field counts
    cannot be transposed without inventing or dropping fields, and a
    transform that invents data is worse than no transform."""
    if not d.endswith(b'\n') or len(d) < 256:
        return None
    lines = d.split(b'\n')[:-1]
    if len(lines) < 4:
        return None
    rows, nc, pre = _rows_and_prelude(lines, sep, (id(d), len(d)))
    if rows is None:
        return None
    nr = len(rows)
    prelude = b'\n'.join(lines[:pre])

    # COLUMN ORDER MATTERS.
    #
    # Columns are written in file order, so a free-text column can sit
    # between two numeric ones and break the matcher's run. Sorting them
    # by a cheap signature - distinct count, then mean length - puts
    # similar columns next to each other.
    #
    # Measured on real NOAA files: +6.0% to +7.6% on one, +1.4% to +1.6%
    # on another. Small, and it costs one byte per column to record the
    # order, so it is only kept when it actually wins.
    order = list(range(nc))
    if regroup and nc <= 255:
        def _sig(c):
            col = [r[c] for r in rows]
            return (len(set(col)), sum(len(v) for v in col) // len(col))
        order = sorted(order, key=_sig)

    hdr = bytearray()
    body = bytearray()
    hdr += bytes([1 if (regroup and nc <= 255) else 0])
    if regroup and nc <= 255:
        hdr += bytes(order)
    for c in order:
        col = [r[c] for r in rows]
        vals = set(col)
        # INTEGER COLUMNS GET DELTA-ENCODED.
        #
        # csvcol treated every column as text, so a column of prices or
        # measurements was stored as full numbers even when consecutive
        # values differ by very little. Writing the DIFFERENCE as text
        # shortens most of them by several digits.
        #
        # Measured on a real diamonds dataset - 7 of its 10 columns are
        # numeric - this took the columnar result from -1.1% to +12.3%.
        # On taxi data, where only 1 column of 14 benefits, it went from
        # -4.6% to -0.1%: still not a win, but no longer a loss.
        #
        # Only used when EVERY value in the column parses as an integer
        # and the delta form is actually shorter, so a column of long
        # random numbers is left alone.
        # The first row is usually a header, so "carat" sits at the top
        # of a column of numbers. Requiring EVERY value to parse as an
        # integer rejected every numeric column in every file that has a
        # header row - which is nearly all of them. The first value is
        # allowed to be text and is stored verbatim.
        ints = None
        if len(col) > 8:
            try:
                ints = [int(v) for v in col[1:]]
            except Exception:
                ints = None
        if ints is not None:
            enc = bytearray()
            prev = 0
            for v in ints:
                enc += str(v - prev).encode() + b'\x00'
                prev = v
            plainlen = sum(len(v) + 1 for v in col[1:])
            if len(enc) < plainlen:
                hdr += b'I' + col[0] + b'\x00'
                body += bytes(enc)
                continue

        if len(vals) == 1:
            hdr += b'C' + col[0] + b'\x00'
        elif len(vals) <= (maxdict or MAX_DICT) and len(vals) * 2 < nr:
            tab = sorted(vals)
            idx = {v: i for i, v in enumerate(tab)}
            hdr += b'D' + str(len(tab)).encode() + b'\x00'
            for v in tab:
                hdr += v + b'\x00'
            for v in col:
                body.append(idx[v])
        else:
            # SUB-SPLIT: a quoted field often holds its own comma-separated
            # record. NOAA weather packs several readings into one:
            #
            #     "330,1,N,0057,1"     wind: direction, flag, type, speed
            #     "3,1,046,1,+999,9"   pressure tendency, four subfields
            #
            # Those subfields are columns in their own right - the wind
            # direction changes slowly, the quality flag almost never
            # changes. Left whole they are one opaque string; split, each
            # becomes a column that dedups.
            inner = None
            if subsplit and col[0].startswith(b'"') and col[0].endswith(b'"'):
                cand = [v[1:-1].split(b',') if v.startswith(b'"')
                        and v.endswith(b'"') else None for v in col]
                if all(x is not None for x in cand):
                    k = len(cand[0])
                    if k > 1 and all(len(x) == k for x in cand):
                        inner = cand
            if inner is not None:
                hdr += b'S' + str(len(inner[0])).encode() + b'\x00'
                for j in range(len(inner[0])):
                    prev = None
                    for x in inner:
                        v = x[j]
                        if v == prev:
                            body += b'\x01\x00'
                        else:
                            body += v + b'\x00'
                            prev = v
                continue
            hdr += b'V\x00'
            prev = None
            for v in col:
                if v == prev:
                    body += b'\x01\x00'
                else:
                    body += v + b'\x00'
                    prev = v
    # Record WHICH delimiter, not just whether it was whitespace.
    #
    # This module only ever split on commas. Measured on the same
    # diamonds dataset, rewritten with each delimiter:
    #
    #     comma       +14.0%
    #     tab          -0.0%
    #     semicolon    -0.0%
    #     pipe         -0.0%
    #
    # Identical content, identical structure - the transform simply could
    # not see it. TSV, semicolon-separated European CSV and pipe-delimited
    # database exports are all common, and all were invisible.
    mark = b'W' if sep is None else bytes([sep[0]])
    return (str(nr).encode() + b'\n' + str(nc).encode() + b'\n'
            + str(len(hdr)).encode() + b'\n' + str(len(prelude)).encode()
            + b'\n' + mark + b'\n' + prelude + bytes(hdr) + bytes(body))


def decode(blob):
    # A cross-column container is a different shape, so it is recognised
    # before anything else. The marker is two bytes because the existing
    # format starts with a decimal digit and cannot collide with "XC".
    if blob[:2] == b"XG":
        import colsplit
        p = 2
        cl, = struct.unpack_from(">I", blob, p); p += 4
        rows = colsplit.decode(blob[p:p + cl]); p += cl
        tl, = struct.unpack_from(">I", blob, p); p += 4
        tail = blob[p:p + tl]
        extra = tail.split(b"\n") if tail else []
        out = []
        for i, row in enumerate(rows):
            r = list(row)
            if i < len(extra) and extra[i]:
                r += extra[i].split(b",")
            out.append(b",".join(r))
        return b"\n".join(out) + b"\n"

    if blob[:2] == b"XP":
        p = 2
        w, nrows = struct.unpack_from(">HI", blob, p); p += 6
        bl, = struct.unpack_from(">I", blob, p); p += 4
        body = blob[p:p + bl]; p += bl
        tl, = struct.unpack_from(">I", blob, p); p += 4
        tail = blob[p:p + tl]
        vals = [[None] * nrows for _ in range(w)]
        bp = 0
        for c in range(w):
            prev = None
            for r in range(nrows):
                e = body.index(b"\x00", bp)
                v = body[bp:e]; bp = e + 1
                if v == b"\x01":
                    v = prev
                else:
                    prev = v
                vals[c][r] = v
        extra = tail.split(b"\n") if tail else []
        out = []
        for r in range(nrows):
            row = [vals[c][r] for c in range(w)]
            if r < len(extra) and extra[r]:
                row += extra[r].split(b",")
            out.append(b",".join(row))
        return b"\n".join(out) + b"\n"

    if blob[:2] == b"XC":
        import crosscol
        p = 2
        hl, = struct.unpack_from(">I", blob, p); p += 4
        hdr = blob[p:p + hl]; p += hl
        xl, = struct.unpack_from(">I", blob, p); p += 4
        cols = crosscol.decode(blob[p:p + xl]); p += xl
        tl, = struct.unpack_from(">I", blob, p); p += 4
        tail = blob[p:p + tl]
        extra = tail.split(b"\n") if tail else []
        out = [hdr]
        n = len(cols[0]) if cols else 0
        for i in range(n):
            row = [c[i] for c in cols]
            if i + 1 < len(extra) and extra[i + 1]:
                row += extra[i + 1].split(b",")
            out.append(b",".join(row))
        return b"\n".join(out) + b"\n"

    nr_s, rest = blob.split(b'\n', 1)
    nc_s, rest = rest.split(b'\n', 1)
    hl_s, rest = rest.split(b'\n', 1)
    pl_s, rest = rest.split(b'\n', 1)
    mark_s, rest = rest.split(b'\n', 1)
    nr, nc, hl, pl = int(nr_s), int(nc_s), int(hl_s), int(pl_s)
    prelude = rest[:pl]
    rest = rest[pl:]
    hdr, body = rest[:hl], rest[hl:]
    p = 0
    reordered = hdr[p]; p += 1
    if reordered:
        order = list(hdr[p:p + nc]); p += nc
    else:
        order = list(range(nc))
    cols_by_pos = {}
    bp = 0
    for c in order:
        kind = hdr[p:p + 1]
        p += 1
        if kind == b'C':
            e = hdr.index(b'\x00', p)
            cols_by_pos[c] = [hdr[p:e]] * nr
            p = e + 1
        elif kind == b'D':
            e = hdr.index(b'\x00', p)
            k = int(hdr[p:e])
            p = e + 1
            tab = []
            for _ in range(k):
                e = hdr.index(b'\x00', p)
                tab.append(hdr[p:e])
                p = e + 1
            cols_by_pos[c] = [tab[body[bp + i]] for i in range(nr)]
            bp += nr
        elif kind == b'I':
            e = hdr.index(b'\x00', p)
            first = hdr[p:e]
            p = e + 1
            out = [first]
            prev = 0
            for _ in range(nr - 1):
                e = body.index(b'\x00', bp)
                v = int(body[bp:e])
                bp = e + 1
                prev = prev + v
                out.append(str(prev).encode())
            cols_by_pos[c] = out
        elif kind == b'S':
            e = hdr.index(b'\x00', p)
            k = int(hdr[p:e])
            p = e + 1
            subs = []
            for _ in range(k):
                out = []
                prev = None
                for _ in range(nr):
                    e2 = body.index(b'\x00', bp)
                    v = body[bp:e2]
                    bp = e2 + 1
                    if v == b'\x01':
                        v = prev
                    else:
                        prev = v
                    out.append(v)
                subs.append(out)
            cols_by_pos[c] = [b'"' + b','.join(subs[j][r] for j in range(k))
                              + b'"' for r in range(nr)]
        else:
            p += 1
            out = []
            prev = None
            for _ in range(nr):
                e = body.index(b'\x00', bp)
                v = body[bp:e]
                bp = e + 1
                if v == b'\x01':
                    v = prev
                else:
                    prev = v
                out.append(v)
            cols_by_pos[c] = out
    cols = [cols_by_pos[i] for i in range(nc)]
    joiner = b' ' if mark_s == b'W' else bytes([mark_s[0]])
    out = b'\n'.join(joiner.join(cols[c][r] for c in range(nc))
                     for r in range(nr)) + b'\n'
    return (prelude + b'\n' + out) if pl else out


def try_encode(d, pack=None):
    """Encode and VERIFY. Returns the blob only if it rebuilds exactly.

    SUB-SPLITTING IS OFF. A quoted field often holds its own comma-
    separated record - NOAA packs wind as "330,1,N,0057,1" - so splitting
    those into sub-columns looked certain to help. Measured on two real
    files it made NO difference at all: +14.1% and +10.5% either way.
    lzma was already finding that structure.

    It cost an hour and a false alarm on the way. An intermediate
    measurement appeared to show sub-splitting destroying one file, +10.5%
    down to +1.7% - which turned out to be nothing of the kind. The slice
    size had been changed from 150,000 to 80,000 bytes between the two
    runs. Different data, not a regression.

    The code is left in place behind a flag, off, because it costs nothing
    and the next file type may differ."""
    # BOTH DICTIONARY CAPS ARE TRIED.
    #
    # Raising the cap from 64 to 255 helped four files out of five and
    # turned taxis.csv from -0.3% into +6.7%. It cost one file 0.9 points,
    # because a wider table means more entries stored even when few of
    # them repeat often. Neither cap is right for every file, so both are
    # built and the smaller kept.
    # BOTH DICTIONARY CAPS, and an honest note about what that misses.
    #
    # Raising the cap from 64 to 255 helped five real files and cost one:
    #
    #     taxis.csv          -0.3% -> +6.7%
    #     47662099999.csv    +9.4% -> +14.1%
    #     diamonds.csv      +13.4% -> +15.3%
    #     72278023183.csv    +3.9% -> +5.8%
    #     03772099999.csv    +7.8% -> +6.9%   the one that lost
    #
    # Both are built and the smaller kept, which recovers most cases. It
    # does NOT recover 03772, because the two encodings pack to the same
    # size here and only diverge after the router applies its own
    # transform - something this function cannot see without running the
    # whole router twice.
    #
    # Trading 0.9 points on one file for 7.0 on another is the right
    # trade, but it is a trade, not a free win.
    best = None
    # ZERO IS A REAL SETTING, NOT AN ABSENCE.
    #
    # The sweep tried 255 and 64 and never asked what happens with NO
    # dictionary at all. On three real weather exports a plain
    # blanked-column encoding beat the dictionary version by 1.1% to
    # 5.8% - the dictionary was costing more than the repeats it
    # replaced.
    #
    # It is not always right: on taxi trips, diamond prices and a
    # structured syslog the dictionary wins by 9.8% to 34.7%. Which is
    # exactly why it belongs in the sweep rather than in a rule.
    for cap in (255, 64):
        globals()['MAX_DICT'] = cap
        _r = _try_one(d, pack)
        if _r is not None and (best is None or len(_r) < len(best)):
            best = _r
    globals()['MAX_DICT'] = 255

    # THE PLAINEST POSSIBLE COLUMNAR ENCODING, AS A CANDIDATE.
    #
    # Columns one after another, a repeated value written as one byte,
    # and nothing else - no dictionary, no subsplitting, no regrouping.
    #
    # It beats everything above on three real weather exports, by 1.1%
    # to 5.8%: the dictionary was costing more than the repeats it
    # replaced. On taxi trips, diamond prices and a structured syslog the
    # full machinery wins by 9.8% to 34.7%.
    #
    # Neither is right in general, which is the argument for measuring
    # both rather than choosing. Setting MAX_DICT to zero does NOT
    # produce this - the other stages still run - so it has to be built
    # directly.
    try:
        _rows = rows_of(d)
        if len(_rows) > 8:
            _w = min(len(r) for r in _rows)
            if _w >= 2:
                _blob = bytearray()
                for _c in range(_w):
                    _prev = None
                    for _r in _rows:
                        _v = _r[_c] if _c < len(_r) else b""
                        if _v == _prev:
                            _blob += b"\x01\x00"
                        else:
                            _blob += _v + b"\x00"
                            _prev = _v
                _rag = any(len(r) > _w for r in _rows)
                _tail = (b"\n".join(b",".join(r[_w:]) for r in _rows)
                         if _rag else b"")
                _cand = (b"XP" + struct.pack(">HI", _w, len(_rows)) +
                         struct.pack(">I", len(_blob)) + bytes(_blob) +
                         struct.pack(">I", len(_tail)) + _tail)
                if _verify(_cand, d):
                    # CHEAP RANKER FIRST, REAL PACKER ONLY FOR CLOSE CALLS.
                    #
                    # The cheap ranker is lzma at preset 1, and on a real
                    # weather export it ranked these two BACKWARDS: it
                    # preferred the dictionary version at 144,876 where
                    # the plain one packs to 102,889 against 106,000.
                    #
                    # But always using the real packer costs six full
                    # compressions and dropped throughput from 0.339 to
                    # 0.282 MB/s. So the proxy decides when the answer is
                    # obvious, and only a near-tie is worth resolving
                    # properly - which is exactly the case it gets wrong.
                    if best is None:
                        best = _cand
                    else:
                        _pk = pack or _default_pack
                        _a, _b = len(_pk(_cand)), len(_pk(best))
                        if _a < _b * (1.0 - CLOSE_ENOUGH):
                            best = _cand
                        elif _a < _b * (1.0 + CLOSE_ENOUGH):
                            _r = pack or _real_pack
                            if len(_r(_cand)) < len(_r(best)):
                                best = _cand
    except Exception:
        pass

    # COLUMN GROUPS.
    #
    # Row chunking is the obvious way to parallelise and it is the wrong
    # one for tables: it cuts every column in half, so each piece has
    # fewer values to work with. Measured on a real 20 MB export, four
    # row-chunks cost 6.55% of ratio for 2.5x the speed.
    #
    # Splitting by COLUMN cuts nothing. Each worker gets whole columns
    # for the entire file, and each group is packed on its own - which
    # is worth 5% by itself, because a single stream spanning several
    # unrelated column groups compresses worse than several streams each
    # matched to its own content.
    #
    #     34-column weather export   +8.0% -> +9.8% against the best
    #                                 free tool
    #     37-column weather export   +2.9%
    #     90-column weather export   +1.9%
    #     narrow tables              declines, no gain to be had
    #
    # This was taken out once for costing 17% of throughput, and put
    # back when it became clear that speed was not what buyers were
    # weighing. Ratio is the claim; the speed is already in the
    # archival tier where nobody expects otherwise.
    #
    # Interleaved, not blocked: neighbouring columns tend to be the same
    # KIND of field, and separating them is what makes each group
    # homogeneous. Blocked groups measured +0.5% where interleaving the
    # same columns gave -4.8%.
    try:
        import colsplit
        _rows2 = rows_of(d)
        if len(_rows2) > 16:
            _w2 = min(len(r) for r in _rows2)
            if _w2 >= colsplit.MIN_COLS:
                _cs = colsplit.try_encode(_rows2, _w2,
                                          groups=(4, 8),
                                          packer=(pack or _default_pack))
                if _cs is not None:
                    _rag2 = any(len(r) > _w2 for r in _rows2)
                    _tail2 = (b"\n".join(b",".join(r[_w2:]) for r in _rows2)
                              if _rag2 else b"")
                    _cand2 = (b"XG" + struct.pack(">I", len(_cs)) + _cs +
                              struct.pack(">I", len(_tail2)) + _tail2)
                    if _verify(_cand2, d):
                        # XG holds ALREADY PACKED groups, so its final
                        # size is its own length while every other
                        # candidate still has to go through a packer.
                        # Ranking them with the same proxy compares a
                        # finished thing against an unfinished one, and
                        # it picked XG on a file where it was 3.6% worse.
                        _r2 = pack or _real_pack
                        if best is None or len(_cand2) < len(_r2(best)):
                            best = _cand2
    except Exception:
        pass

    # CROSS-COLUMN ENCODING, TRIED AS ONE MORE CANDIDATE.
    #
    # Every columnar format compresses each column in isolation. That is
    # the defining property, and it means none of them can say "this
    # column is that one plus a small number" or "these two draw on the
    # same vocabulary". Both are common and expensive to ignore.
    #
    # Measured on real files, plain columnar against columnar + this:
    #
    #     taxis.csv       +11.5%   dropoff from pickup, total from
    #                              fare+tip+tolls, two shared dictionaries
    #     diamonds.csv     +3.3%   y from x, z from y
    #     weather CSVs      0.0%   declined, container costs more
    #     crypto ticks      0.0%   declined, no structure to find
    #
    # It is a candidate rather than a replacement because it loses on
    # most files. crosscol.try_encode already refuses unless the result
    # is both byte-exact and smaller, so a failure here costs only the
    # time to measure it.
    try:
        import crosscol
        rows = rows_of(d)
        if len(rows) > 8:
            width = min(len(r) for r in rows)
            if width >= 2:
                # THE HEADER ROW MUST BE HELD OUT.
                #
                # Passing it in with the data made every numeric column
                # fail: "pickup" is not a timestamp, so the first value
                # rejected the whole column and crosscol found nothing on
                # every file. It returned None silently, which is exactly
                # the shape of failure this project keeps meeting - the
                # code ran, produced no error, and quietly did nothing.
                head, body = rows[0], rows[1:]
                cols = [[r[c] for r in body] for c in range(width)]
                x = crosscol.try_encode(cols)
                if x is not None:
                    _ragc = any(len(r) > width for r in rows)
                    tail = (b"\n".join(b",".join(r[width:]) for r in rows)
                            if _ragc else b"")
                    hdr = b",".join(head)
                    cand = (b"XC" + struct.pack(">I", len(hdr)) + hdr +
                            struct.pack(">I", len(x)) + x +
                            struct.pack(">I", len(tail)) + tail)
                    # COMPARED BY PACKED SIZE, NOT RAW SIZE.
                    #
                    # The other candidates here are all the same shape, so
                    # raw length ranks them correctly. This one is not: it
                    # is LARGER raw and smaller after packing, because its
                    # whole purpose is to make the bytes more compressible
                    # rather than fewer.
                    #
                    # Ranking it by raw length rejected it on every file
                    # tested, including the one it improves by 11.5%.
                    if _verify(cand, d):
                        pk = pack or _default_pack
                        if best is None or len(pk(cand)) < len(pk(best)):
                            best = cand
    except Exception:
        pass  # a candidate that fails costs nothing but the attempt

    return best


# How near two candidates must be under the cheap ranker before the real
# packer is consulted. The case it got wrong was 3.3% apart, so this has
# to be comfortably wider than that - and narrow enough that the
# expensive comparison stays rare.
CLOSE_ENOUGH = 0.15


def _real_pack(b):
    """The comparison that actually decides, used where a proxy misleads."""
    try:
        import packcache
        return packcache.best(b)
    except ImportError:
        import lzma as _l, bz2 as _b, zlib as _z
        return min((_l.compress(b, preset=6), _b.compress(b, 9),
                    _z.compress(b, 9)), key=len)


def _default_pack(b):
    """Used only to rank candidates of different shapes against each
    other. Cheap on purpose - the router will pack the winner properly."""
    try:
        import packcache
        return packcache.compress(b, "lzma", 1)
    except ImportError:
        import lzma
        return lzma.compress(b, preset=1)


def _verify(blob, original):
    """Decode a candidate and compare. Nothing is returned unverified."""
    try:
        return decode(blob) == original
    except Exception:
        return False


def _try_one(d, pack=None):
    best = None
    for sep in (b',', b'\t', b';', b'|', None):
      for md in (255, 64):
        try:
            x = encode(d, subsplit=False, sep=sep, maxdict=md)
        except Exception:
            continue
        if x is None:
            continue
        try:
            if decode(x) != d:
                continue
        except Exception:
            continue
        # SIZED BY A REAL COMPRESSOR, NOT BY RAW LENGTH.
        #
        # Comparing candidates by their encoded length is a proxy, and it
        # picks wrong: a wider dictionary produces a LARGER raw stream
        # that COMPRESSES SMALLER, because the indices are more uniform.
        # On one real NOAA file the raw-length rule chose the 255-cap
        # layout and lost 0.9 points against the 64-cap one.
        #
        # zlib at level 1 is fast and ranks these correctly.
        size = len(pack(x)) if pack else len(zlib.compress(x, 1))
        if best is None or size < best[0]:
            best = (size, x)
    return best[1] if best else None


# ----------------------------------------------------------------------
# JSON — TESTED AND NOT BUILT
# ----------------------------------------------------------------------
#
# A JSON array of objects looks tabular, so flattening nested keys into
# columns should work exactly like this module. It was measured on a real
# 26 MB GitHub events file:
#
#     flattened columns, plain     -6.5%
#     flattened columns, dedup     -7.2%
#     grouped by schema first      +3.9%
#
# The first two LOSE because the schema is heterogeneous: 596 distinct
# keys across 250 records, and only TWELVE present in every record.
# GitHub events carry a different payload per event type, so flattening
# creates 584 mostly-empty columns and the emptiness costs more than the
# columnar gain.
#
# Grouping records by their key set first turns that into a win, but only
# +3.9% - the smallest of any module here.
#
# WHY IT WAS NOT BUILT
#
# Exact reconstruction is the problem. JSON must come back byte-identical,
# which means preserving key order, number formatting (1.0 against 1,
# 1e5 against 100000), unicode escaping and whitespace. Every one of those
# is a way to silently corrupt a file.
#
# The other modules here have low reconstruction risk - they split and
# rejoin - and buy far more: csvcol +4 to +24%, logcol +5 to +15%,
# fasta +13%. JSON is the worst ratio of gain to risk on the list.
#
# WHAT WOULD CHANGE THE ANSWER
#
# A UNIFORM JSON file - an API export or a single-schema log stream -
# would flatten cleanly and should give considerably more. The file
# tested here happened to be the hard case.
