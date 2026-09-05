"""colsplit.py — compress a wide table as several column groups.

WHY THIS EXISTS

Row chunking is the obvious way to parallelise a large file, and it is
the wrong one for tables. Splitting by rows cuts every column in half, so
each piece transposes fewer values and compresses worse: measured on a
real 20 MB export, four row-chunks cost 6.55% of ratio to gain 2.5x the
speed.

Splitting by COLUMN cuts nothing. Each worker gets whole columns for the
entire file, so no column is ever divided, and the pieces are more
homogeneous than the whole - a group of related measurement fields
compresses better on its own than mixed in with timestamps and station
identifiers.

Measured against compressing the whole file as one columnar blob:

    file                 columns   2 groups   4 groups   8 groups
    weather export            34     -1.33%     -7.06%     -9.39%
    weather export            37     -2.10%     -7.31%     -9.43%
    weather export            90     +3.26%     +0.28%     -3.95%
    taxi trips                14    +15.84%    +14.75%    +13.38%
    diamond prices            10     +8.56%     +3.13%     +2.03%

So it is SMALLER and parallel on wide tables, and worse on narrow ones.

WHY NARROW TABLES LOSE

Two reasons, and the second was a surprise.

With ten columns and four groups, each group holds two or three columns
and there is nothing left to match across.

And the narrow files here are exactly the ones where crosscol finds
relationships - dropoff = pickup, total = fare + tip + tolls, y = x,
z = y. Splitting by column separates the two halves of every one of
them. Keeping related columns in the same group recovers part of it -
taxi trips went from +14.75% to +10.72% - but not enough to make
splitting worthwhile there.

WHAT THIS DOES ABOUT IT

Nothing clever. It builds both, measures, and keeps the smaller, which is
the only rule that has survived a day of testing shortcuts. Ranking by
column count would need a threshold, and every threshold tried today has
had an exception.
"""

import struct
import lzma
import bz2
import zlib

MAGIC = b"XCS1"
MIN_COLS = 8            # below this, groups are too thin to help
MAX_GROUPS = 16


def _pack(b):
    """Pack one group, tagging which codec won so it can be undone.

    COMPRESS ONCE, NOT TWICE.
    -------------------------
    The first version took min() of the three results and then looped
    over all three AGAIN to work out which had won - six compressions
    where three would do. It showed up in a trace as this function
    appearing at two line numbers with fourteen calls each, and cost
    throughput 1.66 -> 1.17 MB/s.

    Tagging by index while comparing avoids both that and sniffing the
    output for magic bytes, which is a guess that would corrupt a column
    when wrong."""
    cands = ((0, lzma.compress(b, preset=6)),
             (1, bz2.compress(b, 9)),
             (2, zlib.compress(b, 9)))
    tag, out = min(cands, key=lambda tc: len(tc[1]))
    return bytes([tag]) + out


def _unpack(b):
    tag, payload = b[0], b[1:]
    if tag == 0:
        return lzma.decompress(payload)
    if tag == 1:
        return bz2.decompress(payload)
    return zlib.decompress(payload)


def _encode_group(rows, cols):
    """One group's columns, blanked where a value repeats the last one."""
    out = bytearray()
    for c in cols:
        prev = None
        for r in rows:
            v = r[c] if c < len(r) else b""
            if v == prev:
                out += b"\x01\x00"
            else:
                out += v + b"\x00"
                prev = v
    return bytes(out)


def _decode_group(blob, cols, nrows):
    """Rebuild one group. Mirrors _encode_group exactly."""
    vals = {c: [] for c in cols}
    p = 0
    for c in cols:
        prev = None
        for _ in range(nrows):
            e = blob.index(b"\x00", p)
            v = blob[p:e]
            p = e + 1
            if v == b"\x01":
                v = prev
            else:
                prev = v
            vals[c].append(v)
    return vals


def plan_groups(ncols, related=None, groups=4):
    """Which columns go together.

    Related columns stay in one group. Splitting them costs more than the
    parallelism is worth - crosscol can only see a relationship if both
    sides are in front of it."""
    groups = max(2, min(groups, MAX_GROUPS, ncols // 2))
    out = [[] for _ in range(groups)]
    linked = sorted(related or ())
    if linked:
        out[0] = list(linked)
    free = [c for c in range(ncols) if c not in set(linked)]
    if not free:
        return [g for g in out if g]
    # INTERLEAVED, NOT CONTIGUOUS.
    #
    # Assigning columns 0-8 to one group and 9-17 to the next reads as
    # the natural split and is measurably worse: on a real weather export
    # blocked groups came out +0.5% against one group while interleaving
    # the same columns gave -4.8%.
    #
    # Neighbouring columns in a table tend to be the same KIND of field -
    # a value and its quality flag, a reading and its unit - and putting
    # them in different groups is what creates the homogeneity that makes
    # each group compress well.
    start = 1 if linked else 0
    span = max(1, groups - start)
    for i, c in enumerate(free):
        out[start + (i % span)].append(c)
    return [g for g in out if g]


def encode(rows, ncols, groups=4, related=None):
    """Split into column groups. Returns bytes, or None if not worth trying."""
    if ncols < MIN_COLS or len(rows) < 16:
        return None
    plan = plan_groups(ncols, related, groups)
    if len(plan) < 2:
        return None

    # EACH GROUP IS PACKED SEPARATELY, NOT CONCATENATED AND PACKED ONCE.
    #
    # Concatenating the groups into one blob and compressing that gave
    # 103,416 on a real weather export where packing each group on its
    # own gave 97,969 - five percent, thrown away by joining them.
    #
    # bz2 works in blocks and lzma builds one dictionary; either way a
    # single stream spanning several unrelated column groups is worse
    # than several streams each matched to its own content.
    #
    # And this is what makes the split parallel at all. A concatenated
    # blob has to be compressed by one worker.
    head = bytearray(MAGIC)
    head += struct.pack(">HHI", ncols, len(plan), len(rows))
    body = bytearray()
    for g in plan:
        head += struct.pack(">H", len(g))
        for c in g:
            head += struct.pack(">H", c)
        blob = _pack(_encode_group(rows, g))
        body += struct.pack(">I", len(blob)) + blob

    # THE RAGGED TAIL IS ONLY WRITTEN IF THERE IS ONE.
    #
    # Anything past the common width is kept verbatim per row, so a file
    # with a few odd rows is not disqualified. But the end-of-row marker
    # was written for EVERY row whether or not it had extra columns:
    # two bytes times six thousand rows is twelve kilobytes of pure
    # overhead on a perfectly rectangular table.
    #
    # That was enough to make column splitting look worse than one group
    # - 103,397 against 102,889 - when the same split measured without a
    # container gives 97,969. A container bug reading as a compression
    # result.
    ragged = any(len(r) > ncols for r in rows)
    extra = bytearray()
    if ragged:
        for r in rows:
            for c in range(ncols, len(r)):
                extra += r[c] + b"\x00"
            extra += b"\x02\x00"
    head += bytes([1 if ragged else 0])
    return bytes(head) + struct.pack(">I", len(body)) + bytes(body) + bytes(extra)


def decode(blob):
    """Rebuild the rows exactly."""
    if not blob.startswith(MAGIC):
        raise ValueError("not a column-split container")
    p = len(MAGIC)
    ncols, ngroups, nrows = struct.unpack_from(">HHI", blob, p)
    p += 8
    plan = []
    for _ in range(ngroups):
        n, = struct.unpack_from(">H", blob, p)
        p += 2
        g = []
        for _ in range(n):
            g.append(struct.unpack_from(">H", blob, p)[0])
            p += 2
        plan.append(g)
    ragged = blob[p]
    p += 1
    body_len, = struct.unpack_from(">I", blob, p)
    p += 4
    bp = p
    vals = {}
    for g in plan:
        ln, = struct.unpack_from(">I", blob, bp)
        bp += 4
        vals.update(_decode_group(_unpack(blob[bp:bp + ln]), g, nrows))
        bp += ln
    extra = blob[p + body_len:]

    ep = 0
    rows = []
    for i in range(nrows):
        row = [vals[c][i] for c in range(ncols)]
        if ragged:
            while True:
                e = extra.index(b"\x00", ep)
                v = extra[ep:e]
                ep = e + 1
                if v == b"\x02":
                    break
                row.append(v)
        rows.append(row)
    return rows


def try_encode(rows, ncols, groups=(2, 4, 8), related=None, packer=None):
    """Try several group counts, VERIFY each, and keep the smallest.

    Nothing is returned that has not been decoded and compared. And
    nothing is returned that is not smaller than one group, because on
    narrow tables splitting is worse and there is no threshold that
    reliably predicts which is which."""
    pack = packer or _pack
    try:
        single = _encode_group(rows, list(range(ncols)))
    except Exception:
        return None
    # The container already holds packed groups, so it is compared by its
    # own length rather than by packing it again.
    best = len(pack(single))
    winner = None
    for n in groups:
        try:
            out = encode(rows, ncols, n, related)
        except Exception:
            continue
        if out is None:
            continue
        try:
            back = decode(out)
        except Exception:
            continue
        if back != rows:
            continue
        size = len(out)
        if size < best:
            best, winner = size, out
    return winner
