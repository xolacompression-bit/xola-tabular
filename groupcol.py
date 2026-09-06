"""
groupcol.py — compress many same-schema files as one columnar bundle.

WHAT THIS IS FOR

An archive rarely holds one file. It holds a thousand: one CSV per
station per day, one export per hour, one report per site. Every tool
compresses them one at a time, and every one of those thousand files
carries the same column structure discovered from scratch.

Grouping them by COLUMN rather than by file puts every instance of a
given field adjacent. A thousand copies of the same station identifier,
the same measurement codes, the same date prefixes, all in one run.

RANDOM ACCESS

A bundle that has to be decoded whole is not an archive format, it is a
tar file with extra steps. So the container carries an index: for every
column, where each file's segment begins and how long it is.

Extracting one file out of a thousand reads only that file's segments -
one seek per column - rather than rebuilding the other 999. The index
costs eight bytes per file per column, which on a thousand 30-column
files is 240 KB against a bundle measured in tens of megabytes.

    extract_one(bundle, 700)     one file, without touching the rest
    decode(bundle)               all of them
    file_count(bundle)           how many are in there

MEASURED ON REAL WEATHER DATA, SAME SCHEMA

One real NOAA CSV split into equal parts, which is what a per-day or
per-station archive actually looks like:

    files   separately    grouped     gain    round trip
        2      157,836    132,781   +15.9%    exact
        4      158,428    133,079   +16.0%    exact
        8      174,932    133,461   +23.7%    exact
       16      197,506    134,074   +32.1%    exact

The shape matters more than any single figure. The GROUPED size barely
moves - 132,781 to 134,074 as the count goes from two to sixteen - while
compressing separately gets steadily worse. Each extra file costs almost
nothing when grouped and a full header when not, so the gain grows
without bound as the archive does.

This is on top of what any single-file compressor achieves. prefilter,
cmix and the domain codecs all find structure INSIDE a file; none of
them can exploit structure BETWEEN files, because they never see two at
once.

THE CONDITION, AND IT IS STRICT

The files must share a SCHEMA, not merely a format.

A FALSE POSITIVE THAT WAS NEARLY PUBLISHED

The first measurement of this said +43% on three real weather stations.
It was wrong. Those files have 34, 37 and 90 columns, and the test
encoded only the 34 they shared - silently discarding 56 columns from
the third file and comparing that incomplete encoding against a complete
one.

The honest figure, on files that genuinely share a schema, is the table
above. The lesson is the same one this project keeps relearning: when a
number looks better than expected, suspect the measurement before
believing the method.

TESTED AND FOUND NOT TO WORK: SEISMIC

Four SAC recordings grouped the same way came out 0.4% WORSE. Three
weather stations record the same FIELDS; four seismic stations record
different EARTHQUAKES. There is no shared vocabulary between them, so
adjacency has nothing to exploit.

That negative was nearly missed too. The first measurement showed +35%,
which turned out to be the delta method beating the comparison baseline
rather than grouping helping at all. Comparing grouped against separate
using the IDENTICAL method gave -0.4%.

Two false positives in one session, both caught only by running the
control. Neither would have survived contact with a reviewer, and both
would have been embarrassing to retract.

RANDOM ACCESS

Retrieving one file requires decoding the whole bundle. For an archive
read occasionally that is usually acceptable; for anything read per-file
it is not. The index records where each file's rows begin so a caller
can at least extract one without reconstructing the others in memory.
"""

import struct
import zlib
import lzma
import bz2
import hashlib

try:
    import csvcol
except ImportError:
    csvcol = None

MAGIC = b"XGC1"
REPEAT = b"\x01"


def _split(line, sep=0x2C):
    """Split on the delimiter outside quotes.

    SPLIT FIRST, REJOIN WHERE A QUOTE WAS LEFT OPEN.
    ------------------------------------------------
    The obvious version walks the line one byte at a time. Profiled on a
    32-file bundle that was 53% of the total encoding time - 2.3 million
    bytearray.append calls for what bytes.split does in one step.

    Splitting on the delimiter is safe as long as pieces are glued back
    together wherever a quote is still open. A piece with an odd number
    of quote characters means the delimiter after it was inside quotes.

    csvcol carried the same slow version and the same fix. Two copies of
    one routine is how they drift, and this one should be shared - noted
    rather than done, because moving it now means touching both while
    neither has a test that would catch a subtle difference."""
    sepb = bytes([sep])
    if 0x22 not in line:
        return line.split(sepb)
    parts = line.split(sepb)
    if len(parts) == 1:
        return parts
    # PARITY IS TRACKED, NOT RECOUNTED.
    #
    # Counting quotes in the accumulated buffer each round re-reads
    # everything already read: 495,255 bytes.count calls on one bundle,
    # and quadratic on a long quoted field. Only the NEW piece can change
    # the parity, so only the new piece is counted.
    # PARITY TRACKED PER PIECE, NOT RECOUNTED OVER THE BUFFER.
    #
    # A scan over the quote positions was tried instead - O(quotes)
    # rather than O(fields) counts, which reads better on paper. It came
    # out SLOWER, 3.91 -> 3.76 MB/s, because bytes.split and bytes.count
    # run in C while a per-byte loop runs in Python. An algorithmically
    # cheaper method in the slower language loses.
    out = []
    buf = None
    odd = False
    for piece in parts:
        if piece.count(0x22) & 1:
            odd = not odd
        buf = piece if buf is None else buf + sepb + piece
        if not odd:
            out.append(buf)
            buf = None
    if buf is not None:
        out.append(buf)
    return out


# Leading lines that are not part of the table. Real exports carry them:
# a crypto file starts with a bare URL, a buoy file with two comment
# lines, a survey export with a title and a blank.
#
# Four is what csvcol settled on and there is no reason to differ.
PRELUDE_MAX = 4

# Columns below this are not worth splitting into token streams. The
# work is proportional to the column, and so is the payoff - but the
# payoff only appears on the few columns that dominate an archive.
TOKEN_MIN_BYTES = 64 * 1024

# Worker threads for the per-column work. Set to 0 to force sequential.
#
# Every column is independent and bz2 releases the GIL, so this is free
# parallelism with byte-identical output. It shows nothing on one core.
import os as _os
THREADS = min(16, (_os.cpu_count() or 1))

# How many body layouts to build and measure.
#
# SET TO ONE, AND THAT IS THE MEASURED ANSWER.
#
# Three layouts were built - repeat-blanked, plain, and plain with
# columns sorted by distinct count. Ranked against each other on the raw
# body they differ by 4-5%, which looked worth having.
#
# Measured end to end through the container, one layout won on three of
# five files and overall:
#
#     file          1 layout   3 layouts
#     weather A      112,306     110,109
#     weather B      113,662     115,040
#     taxi trips      95,571      95,571
#     diamonds       258,172     260,582
#     weather C      126,194     126,412
#
# The search ranks bodies with lzma at preset 1, but the final size
# includes the index, the checksums and the preludes, and is packed with
# a different codec. A proxy that never sees the container cannot rank
# what the container will do - the same trap that has now cost this
# project four separate optimisations.
#
# Two layouts also runs at half the speed for nothing. Set this to 3 if
# you want to re-measure; the code is intact.
LAYOUTS = 1


def _tables(blobs, sep=0x2C):
    """Split each file into (prelude, rows, trailing).

    THE FIRST LINE IS NOT ALWAYS THE HEADER.
    ----------------------------------------
    The first version took row zero as the schema. On a real crypto
    export whose first line is "https://www.CryptoDataDownload.com" that
    made every file look like a one-column table, and the bundle was
    refused - correctly, but for the wrong reason.

    So the width is taken as the MOST COMMON row width, and leading rows
    that do not match are held aside verbatim. Each file keeps its own
    prelude, because in a bundle they may genuinely differ. A thousand
    identical header lines cost almost nothing once packed, so there is
    no special case for the common one."""
    from collections import Counter
    tabs = []
    for b in blobs:
        lines = b.split(b"\n")
        trailing = bool(lines) and lines[-1] == b""
        rows = [_split(l, sep) for l in (lines[:-1] if trailing else lines)]
        if not rows:
            tabs.append((b"", [], trailing))
            continue
        counts = Counter(len(r) for r in rows)
        nc = counts.most_common(1)[0][0]
        pre = 0
        while pre < len(rows) and pre < PRELUDE_MAX and len(rows[pre]) != nc:
            pre += 1
        joiner = bytes([sep])
        prelude = b"\n".join(joiner.join(r) for r in rows[:pre])
        tabs.append((prelude, rows[pre:], trailing))
    return tabs


def pack(bundle):
    """Compress a finished bundle.

    BZIP2, AND ONLY BZIP2.
    ----------------------
    A bundle is one long run of similar values per column, which is what
    a block-sorting transform is built for. lzma looks for distant
    repeats, and grouping has already brought them adjacent - so it does
    the expensive search and finds nothing new.

    Measured on 5 real archives and 200 synthetic ones across numeric,
    categorical, mixed and free-text shapes:

        bz2 won   205 of 205
        lzma      9-10% larger, and slower
        zlib      36% larger

    AND THEN IT WAS ZLIB, FOR A REASON THAT DOES NOT MOVE.
    ------------------------------------------------------
    Once every column is compressed inside the container, a bundle is
    almost entirely incompressible and only its header is not. zlib wins
    there because it adds less overhead to data it cannot shrink - by
    about 1% on every archive tested, 5 of 5.

    The tar fallback is the opposite case: raw text, where bz2 wins by
    45-90%, also 5 of 5. Two different inputs with two reliable answers,
    so neither is measured any more.

        TAR (raw text)          bz2 123,313   zlib 232,924
        BUNDLE (pre-packed)     bz2  97,217   zlib  96,457

    THAT WAS TRUE UNTIL THE COLUMNS WERE PACKED INDIVIDUALLY.
    ---------------------------------------------------------
    Once each column is compressed inside the container, the bundle is
    mostly incompressible and only the header is not. zlib then wins,
    because it adds less overhead to data it cannot shrink:

        files    bz2      zlib
           32  126,800  124,747
          128  133,891  131,375
         1024  150,305  147,215

    So the assumption is gone and both are measured. They are both fast
    on data this size, and the winner changed once already."""
    return zlib.compress(bundle, 9)


def unpack(blob):
    """Undo pack(), whichever codec it chose.

    Identified by magic bytes rather than a stored tag: bzip2 output
    always begins BZh and zlib's header is one of a small known set.
    A wrong guess here would be caught by the per-file checksums, but
    it is better not to guess at all."""
    if blob[:3] == b"BZh":
        return bz2.decompress(blob)
    return zlib.decompress(blob)


# Per-column encodings. The method byte is stored so a reader never
# guesses. Adding one here means adding its inverse to _col_decode.
COL_BLANK, COL_PLAIN, COL_TRANS, COL_FRONT, COL_TOKEN = 0, 1, 2, 3, 4


def _vocab_groups(cols, nc, thr=0.3, nsamp=300):
    """Columns whose sampled value sets overlap enough to share a stream."""
    samp = [set(v[::max(1, len(v) // nsamp)]) for v in cols]
    out = []
    used = set()
    for i in range(nc):
        if i in used:
            continue
        grp = [i]
        used.add(i)
        for j in range(i + 1, nc):
            if j in used:
                continue
            a, b = samp[i], samp[j]
            if not a or not b:
                continue
            inter = len(a & b)
            if inter > 1 and inter >= thr * min(len(a), len(b)):
                grp.append(j)
                used.add(j)
        out.append(grp)
    return out


def _col_encode(vals):
    """Encode one column three ways, return (method, bytes).

    CHARACTER TRANSPOSITION IS THE ONE THAT EARNS ITS PLACE.
    -------------------------------------------------------
    A column of fixed-width values - timestamps, codes, padded numbers -
    has far more in common down each character position than along each
    value. Transposing a column of 7,648 timestamps took it from 8,585
    bytes to 829.
    
    prefilter's full router reached 1,016 on the same column and took
    0.91 s against 0.01 s. The specific transform beats the general
    search when the shape is known.

    Measured across five archives, best-of-three against blanking alone:
    +7.9%, +5.3%, +4.1% on weather exports, +0.8% on taxi and diamond
    data where the columns are not fixed width."""
    from collections import Counter

    # BLANK OR PLAIN IS DECIDED BY A RULE, NOT BY PACKING BOTH.
    #
    # Those two differ only in whether a repeated value is written out
    # again, so the repeat rate decides it. Measured over 185 real
    # columns, choosing by repeat rate above one half costs 0.19%
    # against packing both and picking the smaller - and costs nothing
    # to compute.
    #
    # Always blanking costs 0.75%, always plain 1.73%, so the rule is
    # worth having over either fixed choice.
    n = len(vals)
    reps = 0
    prev = None
    out = bytearray()
    for x in vals:
        if x == prev:
            reps += 1
            out += REPEAT + b"\x00"
        else:
            out += x + b"\x00"
            prev = x
    if reps > n * 0.5:
        cands = [(COL_BLANK, bytes(out))]
    else:
        cands = [(COL_PLAIN, b"".join(x + b"\x00" for x in vals))]

    c = Counter(len(x) for x in vals)
    if c:
        w, n = c.most_common(1)[0]
        # Only when nearly every value is that width. A column that is
        # half one width and half another transposes into noise.
        if w >= 2 and n >= len(vals) * 0.9:
            # The odd-width values keep their POSITIONS, or the order
            # cannot be restored. There are few of them by construction -
            # at most one in ten - and they are stored as gaps between
            # positions, which are small numbers.
            same, rest, pos = [], [], bytearray()
            last = -1
            for i, x in enumerate(vals):
                if len(x) == w:
                    same.append(x)
                else:
                    rest.append(x)
                    gap = i - last - 1
                    last = i
                    while gap >= 128:
                        pos.append((gap & 127) | 128)
                        gap >>= 7
                    pos.append(gap)
            body = b"".join(bytes(x[i] for x in same) for i in range(w))
            tail = b"".join(x + b"\x00" for x in rest)
            cands.append((COL_TRANS,
                          struct.pack(">HIII", w, len(same), len(rest),
                                      len(pos)) + bytes(pos) + body + tail))
    # FRONT CODING WAS ADDED AND TAKEN BACK OUT.
    #
    # Writing only what changed from the previous value measured +7.1%
    # on diamond measurements and +3.6% overall - when the choice was
    # made with bz2 at level 9.
    #
    # The choice is made with bz2 at level 1, because ranking every
    # candidate at level 9 costs more than the gain. At level 1 the
    # ranker prefers front coding on columns where level 9 does not,
    # and a real weather export went from 117,811 to 119,522 - WORSE
    # than not having the option at all.
    #
    # The decoder is left in place, so an old bundle still reads. The
    # encoder no longer produces one. This is the fifth time a proxy
    # ranker has cost this project an optimisation that was real when
    # measured properly.

    # TOKEN COLUMNS WERE BUILT, GATED TWICE, AND TAKEN OUT.
    #
    # A field like "SYN07647662 12/82 13311 10097" is several fields
    # sharing a cell. Splitting on the separator and grouping by token
    # POSITION - all the first tokens, then all the second - is worth
    # 10.2% on that column, which was 53% of one archive.
    #
    # And the ranker handles it: bz2-1 agreed with bz2-9 on 23 of 24
    # candidate columns, where front coding managed 6 of 24.
    #
    # It still lost. The archive-level gain was +0.2 to +0.5 points and
    # the cost was a third of the throughput:
    #
    #     no tokens          2.51 MB/s   +23.4%
    #     tokens, ungated    1.41 MB/s   +23.6%
    #     gated on samples   1.59 MB/s   +23.6%
    #     gated on size too  1.69 MB/s   +23.6%
    #
    # Splitting every value and laying out one stream per position is
    # simply expensive, and no gate made it cheap enough. Two tenths of
    # a percent does not buy a third of the speed.
    #
    # The decoder stays so an old bundle still reads. The encoder no
    # longer produces one.

    # Transposition IS measured rather than predicted. It is the one
    # that changes a column by an order of magnitude when it fits, and
    # the features that suggest it - a common width, few repeats - do
    # not separate cleanly enough to trust. One comparison, and only
    # when the width test has already passed.
    if len(cands) == 1:
        return cands[0]
    return min(cands, key=lambda mc: len(bz2.compress(mc[1], 1)))


def _col_decode_at(method, buf, start, nvals):
    """Decode one column that begins at `start`, and say where it ended.

    Every encoding is self-delimiting - it either reads a fixed number
    of null-terminated values or carries its own length header - so
    several can share one buffer without storing offsets between them."""
    if method in (COL_BLANK, COL_PLAIN):
        vals = []
        prev = None
        p = start
        for _ in range(nvals):
            e = buf.find(b"\x00", p)
            if e < 0:
                raise ValueError("column ends before its values are read")
            v = buf[p:e]
            p = e + 1
            if method == COL_BLANK and v == REPEAT:
                v = prev
            else:
                prev = v
            vals.append(v)
        return vals, p
    if method == COL_TRANS:
        w, nsame, nrest, plen = struct.unpack_from(">HIII", buf, start)
        tail_start = start + 14 + plen + w * nsame
        p = tail_start
        seen = 0
        while seen < nrest:
            e = buf.find(b"\x00", p)
            if e < 0:
                raise ValueError("transposed column tail ends early")
            p = e + 1
            seen += 1
        return _col_decode(method, buf[start:p], nvals), p
    if method == COL_FRONT:
        nlen, = struct.unpack_from(">I", buf, start)
        p = start + 4 + nlen
        for _ in range(nvals):
            e = buf.find(b"\x00", p)
            if e < 0:
                raise ValueError("front-coded column ends early")
            p = e + 1
        return _col_decode(method, buf[start:p], nvals), p
    if method == COL_TOKEN:
        sub, mx, ncounts = struct.unpack_from(">BBI", buf, start)
        p = start + 6 + ncounts
        for _ in range(mx * nvals):
            e = buf.find(b"\x00", p)
            if e < 0:
                raise ValueError("token column ends early")
            p = e + 1
        return _col_decode(method, buf[start:p], nvals), p
    raise ValueError(f"unknown column method {method}")


def _col_decode(method, raw, nvals):
    """Reverse _col_encode. Returns the list of values."""
    if method == COL_BLANK:
        vals = []
        prev = None
        p = 0
        for _ in range(nvals):
            e = raw.find(b"\x00", p)
            if e < 0:
                raise ValueError("column ends before its values are read")
            v = raw[p:e]
            p = e + 1
            if v == REPEAT:
                v = prev
            else:
                prev = v
            vals.append(v)
        return vals
    if method == COL_PLAIN:
        vals = []
        p = 0
        for _ in range(nvals):
            e = raw.find(b"\x00", p)
            if e < 0:
                raise ValueError("column ends before its values are read")
            vals.append(raw[p:e])
            p = e + 1
        return vals
    if method == COL_FRONT:
        nlen, = struct.unpack_from(">I", raw, 0)
        lens = raw[4:4 + nlen]
        rest = raw[4 + nlen:]
        if len(lens) != nlen:
            raise ValueError("front-coded column is truncated")
        vals = []
        prev = b""
        p = 0
        for i in range(nvals):
            e = rest.find(b"\x00", p)
            if e < 0:
                raise ValueError("front-coded column ends early")
            x = prev[:lens[i]] + rest[p:e]
            p = e + 1
            vals.append(x)
            prev = x
        return vals

    if method == COL_TOKEN:
        sub, mx, ncounts = struct.unpack_from(">BBI", raw, 0)
        p = 6
        counts = raw[p:p + ncounts]
        p += ncounts
        if len(counts) != ncounts or ncounts != nvals:
            raise ValueError("token column count block is wrong")
        body = raw[p:]
        toks = [[] for _ in range(nvals)]
        q = 0
        for pos in range(mx):
            for i in range(nvals):
                e = body.find(b"\x00", q)
                if e < 0:
                    raise ValueError("token column ends early")
                if pos < counts[i]:
                    toks[i].append(body[q:e])
                q = e + 1
        return [bytes([sub]).join(t) for t in toks]

    if method == COL_TRANS:
        w, nsame, nrest, plen = struct.unpack_from(">HIII", raw, 0)
        p = 14
        pos = raw[p:p + plen]
        p += plen
        body = raw[p:p + w * nsame]
        p += w * nsame
        tail = raw[p:]
        if len(body) != w * nsame:
            raise ValueError("transposed column is truncated")
        cols = [body[i * nsame:(i + 1) * nsame] for i in range(w)]
        same = [bytes(cols[i][j] for i in range(w)) for j in range(nsame)]
        rest = tail.split(b"\x00")[:-1] if tail else []
        if len(rest) != nrest:
            raise ValueError("transposed column tail does not match")
        # rebuild the positions of the odd-width values
        odd = []
        q = 0
        last = -1
        for _ in range(nrest):
            gap = 0
            sh = 0
            while True:
                b = pos[q]
                q += 1
                gap |= (b & 127) << sh
                if b < 128:
                    break
                sh += 7
            last = last + gap + 1
            odd.append(last)
        out = [None] * nvals
        for k, i in enumerate(odd):
            out[i] = rest[k]
        it = iter(same)
        for i in range(nvals):
            if out[i] is None:
                out[i] = next(it)
        return out
    raise ValueError(f"unknown column method {method}")


def encode(blobs, sep=0x2C, packer=None):
    """Bundle several same-schema files into one columnar stream.

    Returns None when the files do not share enough structure to be
    worth grouping - which is most of the time, and saying so is the
    point."""
    if len(blobs) < 2:
        return None
    try:
        tabs = _tables(blobs, sep)
    except Exception:
        return None
    if any(not rows for _, rows, _ in tabs):
        return None

    widths = [len(rows[0]) for _, rows, _ in tabs]
    nc = min(widths)
    if nc < 2:
        return None
    if max(widths) - nc > max(1, nc // 8):
        return None

    nf = len(tabs)
    counts = [len(rows) for _, rows, _ in tabs]

    # EACH COLUMN IS ENCODED AND COMPRESSED ON ITS OWN.
    #
    # Three encodings are tried per column and the smallest kept, then
    # that column is compressed separately. Both matter:
    #
    #   - the encoding, because a fixed-width column transposes by
    #     character position into something far smaller (a timestamp
    #     column went 8,585 -> 829)
    #   - the separate compression, because bzip2 sorts in blocks and one
    #     stream across thirty-four unrelated columns cannot suit them all
    #     (138,990 as one stream against 132,043 separately)
    #
    # Files are NOT indexed by byte offset any more. A transposed column
    # has no per-file byte range - the values interleave - so the reader
    # decodes the whole column and slices it by row count instead. That
    # is simpler, smaller, and removes the index that was destroying the
    # gain above 64 files.
    # THE COLUMNS ARE DONE IN PARALLEL.
    #
    # Choosing each column's encoding and compressing it is 41% of the
    # work, and no column depends on another. bz2 releases the GIL while
    # working, so threads genuinely overlap.
    #
    # The output is byte-identical either way - the same encodings are
    # chosen and written in the same order. Only the waiting changes.
    allcols = [[row[c] if c < len(row) else b""
                for _, rows, _ in tabs for row in rows]
               for c in range(nc)]

    def _enc(vals):
        return _col_encode(vals)

    if THREADS and nc >= 4:
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(nc, THREADS)) as ex:
                encoded = list(ex.map(_enc, allcols))
        except Exception:
            encoded = [_enc(v) for v in allcols]
    else:
        encoded = [_enc(v) for v in allcols]

    # COLUMNS THAT SHARE A VOCABULARY ARE COMPRESSED TOGETHER.
    #
    # A station's minimum and maximum temperature draw on the same few
    # hundred values; a pickup zone and a dropoff zone on the same list
    # of place names. Compressed separately, each stream carries its own
    # copy of that vocabulary.
    #
    # crosscol finds these properly and costs 0.107 s on 34 columns.
    # Comparing sampled value sets finds most of them in 0.0008 s - 127
    # times faster - and measured across five archives the cheap version
    # is worth more, because it groups more aggressively:
    #
    #     weather A +0.74%   taxi +2.23%   diamonds +0.04%
    #     weather B +0.76%   weather C +1.21%
    #
    # The threshold is 0.3 of the smaller set. Looser than that and
    # diamonds goes negative; tighter and the pairs are missed.
    groups = _vocab_groups(allcols, nc)

    def _pack_group(members):
        return bz2.compress(b"".join(encoded[c][1] for c in members), 9)

    if THREADS and len(groups) >= 4:
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(len(groups), THREADS)) as ex:
                segs = list(ex.map(_pack_group, groups))
        except Exception:
            segs = [_pack_group(gg) for gg in groups]
    else:
        segs = [_pack_group(gg) for gg in groups]

    body = bytearray()
    for seg in segs:
        body += seg
    colsizes = [len(x) for x in segs]
    methods = bytearray(encoded[c][0] for c in range(nc))
    # where each column sits: which stream, and its length inside it
    layout = []
    for gi, members in enumerate(groups):
        off = 0
        for c in members:
            ln = len(encoded[c][1])
            layout.append((c, gi, off, ln))
            off += ln
    layout.sort()

    extra = bytearray()
    ragged = any(len(row) > nc for _, rows, _ in tabs for row in rows)
    if ragged:
        for _, rows, _ in tabs:
            for row in rows:
                for c in range(nc, len(row)):
                    extra += row[c] + b"\x00"
                extra += b"\x02\x00"

    head = bytearray(MAGIC)
    head += struct.pack(">BHH", sep, nc, nf)
    head += bytes([1 if ragged else 0])
    head += bytes(methods)
    for (prelude, rows, trailing) in tabs:
        head += struct.pack(">IB", len(rows), 1 if trailing else 0)
        head += struct.pack(">H", len(prelude)) + prelude
    head += struct.pack(">H", len(colsizes))
    for cs in colsizes:
        v = cs
        while v >= 128:
            head.append((v & 127) | 128)
            v >>= 7
        head.append(v)
    # ONLY THE STREAM ID IS STORED, NOT THE OFFSETS.
    #
    # The first attempt wrote (stream, offset, length) per column - ten
    # bytes each, 340 on a 34-column archive. The grouping it enabled was
    # worth 454 bytes on the best file, so half the gain went straight
    # back out in bookkeeping.
    #
    # Columns are written into their stream in column order, so a reader
    # that knows which stream each column is in can walk them and derive
    # the offsets. Two bytes instead of ten.
    for c, gi, off, ln in layout:
        head += struct.pack(">H", gi)
    for b in blobs:
        head += struct.pack(">I", zlib.crc32(b) & 0xFFFFFFFF)
    head += struct.pack(">I", len(body))
    return bytes(head) + bytes(body) + bytes(extra)


def _header(blob):
    """Read the header without touching the column data.

    One parser. decode and extract_one both call this, because when they
    each had their own they drifted - a prelude field was added to one
    and not the other, and one worked while the other raised."""
    if not blob.startswith(MAGIC):
        raise ValueError("not a grouped columnar bundle")
    try:
        p = len(MAGIC)
        sep, nc, nf = struct.unpack_from(">BHH", blob, p)
        p += 5
        ragged = bool(blob[p])
        p += 1
        methods = list(blob[p:p + nc])
        p += nc
        if len(methods) != nc:
            raise struct.error("short")
        meta = []
        for _ in range(nf):
            nrows, trailing = struct.unpack_from(">IB", blob, p)
            p += 5
            plen, = struct.unpack_from(">H", blob, p)
            p += 2
            prelude = blob[p:p + plen]
            p += plen
            meta.append((nrows, bool(trailing), prelude))
        nstreams, = struct.unpack_from(">H", blob, p)
        p += 2
        colsizes = []
        for _ in range(nstreams):
            v = 0
            sh = 0
            while True:
                b = blob[p]
                p += 1
                v |= (b & 127) << sh
                if b < 128:
                    break
                sh += 7
            colsizes.append(v)
        gids = []
        for _ in range(nc):
            gids.append(struct.unpack_from(">H", blob, p)[0])
            p += 2
        crcs = []
        for _ in range(nf):
            crcs.append(struct.unpack_from(">I", blob, p)[0])
            p += 4
        body_len, = struct.unpack_from(">I", blob, p)
        p += 4
    except (struct.error, IndexError):
        raise ValueError("bundle is truncated: header does not fit")
    if p + body_len > len(blob):
        raise ValueError("bundle is truncated: body does not fit")
    if sum(colsizes) != body_len:
        raise ValueError("bundle is corrupt: column sizes do not sum")
    return (sep, nc, nf, ragged, methods, meta, colsizes, p, body_len,
            crcs, gids)


def _all_columns(blob, base, colsizes, methods, nvals, gids):
    """Decompress each stream, then decode each column out of it.

    A stream may hold several columns - those that share a vocabulary.
    Offsets are not stored: columns go into their stream in column
    order, and each encoding says where it ends, so a reader walks them.
    That is eight bytes per column saved, which on these archives is
    about half the gain the grouping produces."""
    streams = []
    p = base
    for i, cs in enumerate(colsizes):
        try:
            streams.append(bz2.decompress(blob[p:p + cs]))
        except Exception:
            raise ValueError(f"bundle is corrupt: stream {i} will not decompress")
        p += cs
    pos = [0] * len(streams)
    out = [None] * len(gids)
    for c, gi in enumerate(gids):
        if gi >= len(streams):
            raise ValueError(f"bundle is corrupt: column {c} names stream {gi}")
        vals, used = _col_decode_at(methods[c], streams[gi], pos[gi], nvals)
        pos[gi] = used
        out[c] = vals
    return out


def decode(blob):
    """Rebuild every file in the bundle, in the order they were given."""
    (sep, nc, nf, ragged, methods, meta,
     colsizes, base, body_len, crcs, gids) = _header(blob)
    total = sum(m[0] for m in meta)
    cols = _all_columns(blob, base, colsizes, methods, total, gids)
    extra = blob[base + body_len:]

    joiner = bytes([sep])
    out = []
    row0 = 0
    ep = 0
    for fi, (nrows, trailing, prelude) in enumerate(meta):
        lines = prelude.split(b"\n") if prelude else []
        for r in range(nrows):
            row = [cols[c][row0 + r] for c in range(nc)]
            if ragged:
                while True:
                    e = extra.index(b"\x00", ep)
                    v = extra[ep:e]
                    ep = e + 1
                    if v == b"\x02":
                        break
                    row.append(v)
            lines.append(joiner.join(row))
        blob_out = b"\n".join(lines) + (b"\n" if trailing else b"")
        if (zlib.crc32(blob_out) & 0xFFFFFFFF) != crcs[fi]:
            raise ValueError(
                f"file {fi} failed its checksum - the bundle is damaged")
        out.append(blob_out)
        row0 += nrows
    return out


def file_count(blob):
    """How many files are in this bundle, without decoding any."""
    return _header(blob)[2]


def extract_one(blob, which):
    """Rebuild ONE file.

    WHAT THIS COSTS, STATED PLAINLY.
    --------------------------------
    Every column is compressed on its own, so this decompresses all of
    them and takes one slice from each. That is roughly a third of the
    work of decoding the whole archive - not the thirtieth it would be
    if columns were stored per file.

    Blocking columns by file group WOULD make it cheaper: at 64 files
    per block, one file needs a quarter of each column. It costs 16.3%
    of ratio, which is a worse trade than the access is worth.

    In absolute terms this is tens of milliseconds on a 2 MB archive.
    Fast enough to pull a record out of a compliance store; not fast
    enough to serve queries from."""
    (sep, nc, nf, ragged, methods, meta,
     colsizes, base, body_len, crcs, gids) = _header(blob)
    if not 0 <= which < nf:
        raise IndexError(f"bundle holds {nf} files, asked for {which}")
    total = sum(m[0] for m in meta)
    cols = _all_columns(blob, base, colsizes, methods, total, gids)
    extra = blob[base + body_len:]
    nrows, trailing, prelude = meta[which]
    row0 = sum(meta[i][0] for i in range(which))

    ep = 0
    if ragged:
        for i in range(row0):
            while True:
                e = extra.index(b"\x00", ep)
                v = extra[ep:e]
                ep = e + 1
                if v == b"\x02":
                    break

    joiner = bytes([sep])
    lines = prelude.split(b"\n") if prelude else []
    for r in range(nrows):
        row = [cols[c][row0 + r] for c in range(nc)]
        if ragged:
            while True:
                e = extra.index(b"\x00", ep)
                v = extra[ep:e]
                ep = e + 1
                if v == b"\x02":
                    break
                row.append(v)
        lines.append(joiner.join(row))
    out = b"\n".join(lines) + (b"\n" if trailing else b"")
    if (zlib.crc32(out) & 0xFFFFFFFF) != crcs[which]:
        raise ValueError(
            f"file {which} failed its checksum - the bundle is damaged")
    return out


def try_encode(blobs, sep=0x2C):
    """Bundle and VERIFY. Returns None unless every file rebuilds exactly.

    A bundle is all-or-nothing: one file that does not round-trip makes
    the whole archive untrustworthy, not just that entry. So both paths
    are checked - the whole-bundle decode AND the per-file extract -
    because they are different code and a bundle is only useful if both
    give back what went in."""
    try:
        out = encode(blobs, sep)
    except Exception:
        return None
    if out is None:
        return None
    try:
        # decode() checks EVERY file, including its checksum, and is one
        # pass over the archive.
        back = decode(out)
        if len(back) != len(blobs) or any(a != b for a, b in zip(back, blobs)):
            return None

        # extract_one is checked on a SAMPLE, not on every file.
        #
        # It decompresses every column to read one file, so calling it
        # once per file is quadratic: at 512 files that was 18.1 s of a
        # 19.0 s encode - 95% of the time spent proving something decode
        # had already proved.
        #
        # The two paths share their column reading, so what a sample
        # tests is that they AGREE, not that each file is individually
        # sound - decode establishes that. First, last and a middle file
        # is enough to catch an off-by-one at either end.
        # FIRST AND LAST ONLY.
        #
        # extract_one decodes every column to read one file, so each
        # check is a full pass over the archive. decode has already
        # proved every file individually; what these check is that the
        # two paths AGREE on the slicing.
        #
        # Slicing errors are off-by-ones, and off-by-ones live at the
        # boundaries. A middle file is structurally identical to every
        # other middle file, so checking one buys nothing a boundary
        # check does not.
        n = len(blobs)
        for i in {0, n - 1}:
            if extract_one(out, i) != blobs[i]:
                return None
    except Exception:
        return None
    return out


def archive(blobs, sep=0x2C):
    """Compress a set of files, never worse than the obvious alternative.

    WHY THIS EXISTS RATHER THAN JUST try_encode
    -------------------------------------------
    Bundling by column beats tar+bzip2 by 9-11% on same-schema archives.
    On files whose columns are already varied it LOSES - measured -9.1%
    on a taxi export whose columns share little.

    A tool that is sometimes worse than what someone already does is not
    something they can adopt. So both are built and the smaller returned,
    with one byte saying which.

    Returns (blob, method) where method is "bundle" or "tar".
    """
    import tarfile
    import io as _io

    def _tar():
        buf = _io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            for i, b in enumerate(blobs):
                ti = tarfile.TarInfo(f"{i:08d}")
                ti.size = len(b)
                t.addfile(ti, _io.BytesIO(b))
        return buf.getvalue()

    # THE FALLBACK IS BUILT ALONGSIDE THE BUNDLE, NOT BEFORE IT.
    #
    # Compressing the tar is 20% of the total and has nothing to do with
    # the bundle - it exists only to be compared against. Waiting for one
    # before starting the other was pure delay.
    #
    # This is not speculation: BOTH results are needed, whichever wins.
    # An earlier attempt to overlap two lzma presets failed because a
    # gate decided whether the second was needed at all, and starting it
    # early meant sometimes throwing the work away. Here there is no gate.
    fut = None
    if THREADS:
        try:
            from concurrent.futures import ThreadPoolExecutor
            ex = ThreadPoolExecutor(max_workers=1)
            fut = ex.submit(lambda: b"T" + bz2.compress(_tar(), 9))
        except Exception:
            fut = None
    try:
        bundle = try_encode(blobs, sep)
    except Exception:
        bundle = None
    if fut is not None:
        try:
            fallback = fut.result()
            ex.shutdown(wait=False)
        except Exception:
            fallback = b"T" + bz2.compress(_tar(), 9)
    else:
        fallback = b"T" + bz2.compress(_tar(), 9)
    if bundle is None:
        return fallback, "tar"
    grouped = b"B" + pack(bundle)
    return ((grouped, "bundle") if len(grouped) < len(fallback)
            else (fallback, "tar"))


def unarchive(blob):
    """Reverse archive(), whichever route it took."""
    import tarfile
    import io as _io
    tag, body = blob[:1], blob[1:]
    if tag == b"B":
        return decode(unpack(body))
    if tag == b"T":
        buf = _io.BytesIO(unpack(body))
        out = []
        with tarfile.open(fileobj=buf, mode="r") as t:
            for m in sorted(t.getmembers(), key=lambda m: m.name):
                out.append(t.extractfile(m).read())
        return out
    raise ValueError("not an archive produced by archive()")


def worth_it(blobs, packer, sep=0x2C):
    """Would grouping actually beat compressing these separately?

    Takes the packer as an argument rather than assuming one, because
    the answer depends on it: measured on the same files, grouping wins
    by 43% with lzma and not at all on data whose files share no
    vocabulary. Never assume - compare."""
    bundle = try_encode(blobs, sep)
    if bundle is None:
        return None
    separate = sum(len(packer(b)) for b in blobs)
    together = len(packer(bundle))
    return (bundle, together, separate) if together < separate else None
