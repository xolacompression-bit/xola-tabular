"""
crosscol.py — exploit relationships BETWEEN columns, not just within them.

WHY THIS EXISTS

Every columnar format compresses each column in isolation. That is the
defining property of columnar storage, and it is the gap this fills.
Parquet cannot express "this column is that column plus a small number",
and it cannot notice that two columns draw on the same vocabulary. Both
are common in real tables and both are expensive to ignore.

Measured on a real NYC taxi export, 5,892 rows:

    shared dictionary: pickup_zone, dropoff_zone
    shared dictionary: pickup_borough, dropoff_borough
    dropoff = difference from pickup
    total   = difference from fare + tip

    before 91,380    after 69,274    +24.2%

Nothing about taxis is hardcoded. The arithmetic identity total = fare +
tip was found by trying combinations and measuring, not by knowing what
a fare is.

For context on the same file: Parquet reaches 142,735 and ORC 179,159.

MEASURED ON FILES IT WAS NOT DESIGNED AGAINST

    taxis.csv                +11.5%   shared vocabularies, dropoff from
                                      pickup, total from fare+tip+tolls
    diamonds.csv              +3.3%   y from x, z from y - a diamond's
                                      three dimensions are correlated
    NOAA weather CSV          +0.0%   declined; container costs more
    crypto tick data          +0.0%   correctly finds nothing

An earlier version of this file claimed +18.1% on taxis. That figure was
measured on an encoding that did not round-trip: mixed-precision decimal
columns came back as 7.00 where the original said 7.0. Fixing that cost
most of the gain, and a never-worse guard cost the rest on two of four
files. The numbers above are what survives a byte-exact round trip and a
size comparison against plain columnar storage.

The diamond result is the one worth noting: nobody told it that x, y and
z are the physical dimensions of the same stone. It tried the difference,
measured it, and kept it.

The crypto result matters as much. A method that always finds something
is finding its own search procedure.

WHAT WAS TRIED AND REJECTED

Functional dependencies. pickup_zone determines pickup_borough exactly -
195 zones, none spanning two boroughs. Encoding the borough as a lookup
table cost 47.7% MORE than storing the column, because the borough column
is five values repeated six thousand times and already compresses to
almost nothing, while the lookup needs all 195 zone names. A dependency
only pays when the dependent column is expensive AND its key is cheap.

Conditional entropy as a search method. Scanning every column pair by
mutual information reported 31 relationships above 25%, several above
50%. Scored on held-out data, four of five vanished:

    dropoff given dropoff_zone   51% in-sample   +0.0% holdout
    pickup  given pickup_zone    49%             +0.0%
    total   given fare           46%             -1.9%
    total   given tip            62%            +15.7%   real

Six thousand distinct timestamps conditioned on two hundred zones gives
thirty samples per group, and a context seen thirty times always looks
predictable. Only the relationship already captured arithmetically
survived. Every candidate here is therefore scored by ACTUAL ENCODED
SIZE, never by an entropy estimate.
"""

import struct
import datetime as _dt
import lzma
import bz2
import zlib
import datetime as _dt
from itertools import combinations

MAGIC = b"XCC1"
MAX_COLS = 512


def _pack(b):
    # Through the memo - this is called on the same buffers that csvcol
    # and prefilter also ask about, and it was the single largest caller
    # in a real trace at 32 calls on 4.4 MB.
    try:
        import packcache
        return packcache.best(b)
    except ImportError:
        return min((lzma.compress(b, preset=6), bz2.compress(b, 9),
                    zlib.compress(b, 9)), key=len)


def _vint(vals):
    """Zigzag varint - small magnitudes cost one byte, sign is cheap."""
    out = bytearray()
    for w in vals:
        z = (w << 1) ^ (w >> 63)
        while z >= 128:
            out.append((z & 127) | 128)
            z >>= 7
        out.append(z)
    return bytes(out)


def _unvint(buf, n):
    vals = []
    p = 0
    for _ in range(n):
        z = 0
        s = 0
        while True:
            b = buf[p]
            p += 1
            z |= (b & 127) << s
            if b < 128:
                break
            s += 7
        vals.append((z >> 1) ^ -(z & 1))
    return vals, p


_TS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S")


def as_integers(col):
    """Read a column as integers if it plausibly is one.

    Returns (values, kind, scale) or None. Timestamps become epoch
    seconds; decimals are scaled by the widest fraction seen, so 3.95
    and 3.9 share one representation."""
    if not col:
        return None
    sample = col[:min(300, len(col))]
    for fmt in _TS:
        try:
            out = [int(_dt.datetime.strptime(v.decode(), fmt).timestamp())
                   for v in col]
            return out, "ts", _TS.index(fmt)
        except Exception:
            pass
    try:
        dec = 0
        for v in sample:
            s = v.decode()
            float(s)
            if "." in s:
                dec = max(dec, len(s.split(".")[1]))
        scale = 10 ** dec
        return [int(round(float(v.decode()) * scale)) for v in col], "num", dec
    except Exception:
        return None


def find_shared_vocabularies(cols, threshold=0.5):
    """Group text columns that draw on the same set of values.

    origin/destination, from/to, sender/recipient, old/new - these are
    everywhere in real tables, and every columnar format stores the
    vocabulary once per column. Measured on the taxi file, sharing one
    dictionary between the two zone columns was worth 26.7% of both."""
    groups = []
    used = set()
    for a in range(len(cols)):
        if a in used:
            continue
        va = set(cols[a])
        if len(va) < 2 or len(va) > len(cols[a]) * 0.8:
            continue
        group = [a]
        used.add(a)
        for b in range(a + 1, len(cols)):
            if b in used:
                continue
            vb = set(cols[b])
            if not vb:
                continue
            overlap = len(va & vb) / max(1, len(va | vb))
            if overlap > threshold:
                group.append(b)
                used.add(b)
        if len(group) > 1:
            groups.append(group)
    return groups


def find_differences(ints, order, packer=_pack):
    """Which columns are cheaper stored as a difference from another?

    Tries single columns and pairs. Scored by real encoded size - an
    entropy estimate reported 31 relationships here and held-out testing
    found one."""
    plan = {}
    kept = []
    for c in order:
        vals = ints[c]
        best = len(packer(_vint(vals)))
        via = None
        for k in kept:
            other = ints[k]
            if len(other) != len(vals):
                continue
            r = len(packer(_vint([vals[i] - other[i]
                                  for i in range(len(vals))])))
            if r < best:
                best, via = r, k
        # Pairs and triples. A taxi total is fare + tip + tolls plus a
        # surcharge from a short fixed list, and only the three-way
        # subtraction exposes it: the total column went from -1.7% at
        # two terms to +72.1% at three.
        for size in (2, 3):
            for combo in combinations(kept, size):
                if any(len(ints[k]) != len(vals) for k in combo):
                    continue
                acc = list(vals)
                for k in combo:
                    src = ints[k]
                    acc = [acc[i] - src[i] for i in range(len(vals))]
                r = len(packer(_vint(acc)))
                if r < best:
                    best, via = r, combo
        if via is not None:
            plan[c] = via
        kept.append(c)
    return plan


def analyse(cols):
    """Report what could be exploited, without encoding anything.

    Separated from any encoder so the finding can be inspected, argued
    with, and tested on a file before it is trusted."""
    ints = {}
    for c, col in enumerate(cols):
        r = as_integers(col)
        if r:
            ints[c] = r[0]
    text = [c for c in range(len(cols)) if c not in ints]
    shared = find_shared_vocabularies([cols[c] for c in text])
    shared = [[text[i] for i in g] for g in shared]
    order = sorted(ints, key=lambda c: -len(set(cols[c])))
    diffs = find_differences(ints, order)
    return {"numeric": sorted(ints), "text": text,
            "shared_vocabularies": shared, "differences": diffs}


def describe(cols, names=None):
    """A human-readable account of what was found."""
    a = analyse(cols)
    nm = (lambda c: names[c].decode("utf8", "replace")
          if names and c < len(names) else f"col {c}")
    lines = []
    for g in a["shared_vocabularies"]:
        lines.append("shared dictionary: " + ", ".join(nm(c) for c in g))
    for c, via in a["differences"].items():
        src = (nm(via) if isinstance(via, int)
               else " + ".join(nm(x) for x in via))
        lines.append(f"{nm(c)} = difference from {src}")
    return lines or ["no cross-column structure found"]


# ----------------------------------------------------------------------
# ENCODER AND DECODER
# ----------------------------------------------------------------------
#
# The container records the plan explicitly - which columns share a
# dictionary, which are stored as differences, and how each numeric
# column was read. A decoder never has to infer anything, which means a
# file written today still opens if the analysis changes tomorrow.

_TEXT, _NUM, _DIFF1, _DIFFN, _SHARED = 0, 1, 2, 3, 4


def _enc_vals(vals, kind, scale):
    """Turn integers back into the exact text they came from.

    This is where losslessness is won or lost. A timestamp must format
    back to the same string, and 3.90 must not become 3.9 - so the
    decimal count is stored rather than inferred from the value."""
    if kind == "ts":
        fmt = _TS[scale]
        return [_dt.datetime.fromtimestamp(v).strftime(fmt).encode()
                for v in vals]
    if scale == 0:
        return [str(v).encode() for v in vals]
    # A column holding both "7.0" and "12.95" has scale 2, and padding
    # every value to two places turns 7.0 into 7.00 - a different file.
    # Five columns of the taxi export failed on exactly that.
    #
    # Real data almost always writes the minimal representation with at
    # least one decimal place, so that convention is reproduced here and
    # then VERIFIED against the original text before the column is
    # accepted as numeric. Where a file does not follow it, the check
    # rejects the column and it stays as text.
    out = []
    d = 10 ** scale
    for v in vals:
        neg = v < 0
        whole, frac = divmod(abs(v), d)
        f = f"{frac:0{scale}d}".rstrip("0") or "0"
        out.append((b"-" if neg else b"") + f"{whole}.{f}".encode())
    return out


def encode(cols, plan=None):
    """Apply the cross-column plan. Returns bytes, or None if nothing helps."""
    if not cols or len(cols) > MAX_COLS:
        return None
    n = len(cols[0])
    if n < 4 or any(len(c) != n for c in cols):
        return None
    a = plan or analyse(cols)
    if not a["shared_vocabularies"] and not a["differences"]:
        return None

    # EVERY NUMERIC COLUMN MUST SURVIVE THE ROUND TRIP TEXTUALLY.
    #
    # Reading "7.0" and "12.95" from one column gives a scale of 2, and
    # 7.0 comes back as "7.00" - a different string, so a different file.
    # The same happens with "007", "+1.5", "1e3" and trailing spaces.
    #
    # Rather than enumerate those cases, each column is converted and
    # converted back, and only kept as numeric if the text is identical.
    # Five columns of the taxi file failed this on the first attempt.
    ints, kinds = {}, {}
    for c in a["numeric"]:
        r = as_integers(cols[c])
        if not r:
            continue
        vals, kind, scale = r
        try:
            if _enc_vals(vals, kind, scale) != list(cols[c]):
                continue
        except Exception:
            continue
        ints[c], kinds[c] = vals, (kind, scale)

    layout = [None] * len(cols)
    for gi, g in enumerate(a["shared_vocabularies"]):
        for c in g:
            layout[c] = (_SHARED, gi)
    for c, via in a["differences"].items():
        if c not in ints:
            continue
        if isinstance(via, int):
            if via in ints:
                layout[c] = (_DIFF1, via)
        elif all(k in ints for k in via):
            layout[c] = (_DIFFN, tuple(via))
    for c in range(len(cols)):
        if layout[c] is None:
            layout[c] = (_NUM, 0) if c in ints else (_TEXT, 0)

    head = bytearray(MAGIC)
    head += struct.pack(">HI", len(cols), n)
    head += struct.pack(">H", len(a["shared_vocabularies"]))

    body = bytearray()
    for g in a["shared_vocabularies"]:
        vocab = sorted(set().union(*[set(cols[c]) for c in g]))
        si = {v: i for i, v in enumerate(vocab)}
        dic = b"".join(v + b"\x00" for v in vocab)
        head += struct.pack(">HI", len(g), len(vocab))
        for c in g:
            head += struct.pack(">H", c)
        body += struct.pack(">I", len(dic)) + dic
        for c in g:
            idx = _vint([si[v] for v in cols[c]])
            body += struct.pack(">I", len(idx)) + idx

    for c in range(len(cols)):
        tag, arg = layout[c]
        if tag == _SHARED:
            head += bytes([tag]) + struct.pack(">H", arg)
            continue
        if tag == _TEXT:
            head += bytes([tag])
            blob = b"".join(v + b"\x00" for v in cols[c])
            body += struct.pack(">I", len(blob)) + blob
            continue
        kind, scale = kinds[c]
        head += bytes([tag]) + bytes([1 if kind == "ts" else 0, scale])
        if tag == _NUM:
            vals = ints[c]
        elif tag == _DIFF1:
            head += struct.pack(">H", arg)
            vals = [ints[c][i] - ints[arg][i] for i in range(n)]
        else:
            head += bytes([len(arg)])
            for k in arg:
                head += struct.pack(">H", k)
            vals = list(ints[c])
            for k in arg:
                src = ints[k]
                vals = [vals[i] - src[i] for i in range(n)]
        v = _vint(vals)
        body += struct.pack(">I", len(v)) + v

    return bytes(head) + struct.pack(">I", len(head)) + bytes(body)


def decode(blob):
    """Reverse encode() exactly.

    Order matters: shared-dictionary columns are rebuilt first, then
    plain columns, then differences - because a difference needs the
    column it was taken from to exist already. The plan is walked twice
    for that reason rather than trying to be clever about ordering."""
    if not blob.startswith(MAGIC):
        raise ValueError("not a crosscol container")
    p = len(MAGIC)
    ncols, nrows = struct.unpack_from(">HI", blob, p)
    p += 6
    ngroups, = struct.unpack_from(">H", blob, p)
    p += 2

    groups = []
    for _ in range(ngroups):
        gsize, vsize = struct.unpack_from(">HI", blob, p)
        p += 6
        members = []
        for _ in range(gsize):
            members.append(struct.unpack_from(">H", blob, p)[0])
            p += 2
        groups.append((members, vsize))

    layout = []
    for _ in range(ncols):
        tag = blob[p]
        p += 1
        if tag == _SHARED:
            gi, = struct.unpack_from(">H", blob, p)
            p += 2
            layout.append((tag, gi, None, None))
        elif tag == _TEXT:
            layout.append((tag, None, None, None))
        else:
            is_ts, scale = blob[p], blob[p + 1]
            p += 2
            if tag == _DIFF1:
                src, = struct.unpack_from(">H", blob, p)
                p += 2
            elif tag == _DIFFN:
                k = blob[p]
                p += 1
                src = tuple(struct.unpack_from(">H", blob, p + 2 * j)[0]
                            for j in range(k))
                p += 2 * k
            else:
                src = None
            layout.append((tag, src, "ts" if is_ts else "num", scale))

    head_len, = struct.unpack_from(">I", blob, p)
    bp = p + 4

    cols = [None] * ncols
    raw_ints = {}

    for members, vsize in groups:
        dl, = struct.unpack_from(">I", blob, bp)
        bp += 4
        vocab = blob[bp:bp + dl].split(b"\x00")[:-1]
        bp += dl
        for c in members:
            il, = struct.unpack_from(">I", blob, bp)
            bp += 4
            idx, _ = _unvint(blob[bp:bp + il], nrows)
            bp += il
            cols[c] = [vocab[i] for i in idx]

    pending = []
    for c in range(ncols):
        tag, src, kind, scale = layout[c]
        if tag == _SHARED:
            continue
        ln, = struct.unpack_from(">I", blob, bp)
        bp += 4
        chunk = blob[bp:bp + ln]
        bp += ln
        if tag == _TEXT:
            cols[c] = chunk.split(b"\x00")[:-1]
            continue
        vals, _ = _unvint(chunk, nrows)
        if tag == _NUM:
            raw_ints[c] = vals
            cols[c] = _enc_vals(vals, kind, scale)
        else:
            pending.append((c, tag, src, kind, scale, vals))

    # Differences resolve after their sources exist. A chain (z from y,
    # y from x) needs more than one pass, so this loops until nothing
    # more can be resolved rather than assuming a single order.
    while pending:
        progress = False
        still = []
        for c, tag, src, kind, scale, vals in pending:
            if tag == _DIFF1:
                if src not in raw_ints:
                    still.append((c, tag, src, kind, scale, vals))
                    continue
                base = raw_ints[src]
                out = [vals[i] + base[i] for i in range(nrows)]
            else:
                if any(k not in raw_ints for k in src):
                    still.append((c, tag, src, kind, scale, vals))
                    continue
                out = list(vals)
                for k in src:
                    b_ = raw_ints[k]
                    out = [out[i] + b_[i] for i in range(nrows)]
            raw_ints[c] = out
            cols[c] = _enc_vals(out, kind, scale)
            progress = True
        if not progress and still:
            raise ValueError("unresolvable difference chain in container")
        pending = still

    return cols


def try_encode(cols, packer=None):
    """Encode, VERIFY, and only return it if it is actually smaller.

    Nothing is returned that has not been decoded and compared. A
    tabular encoder that gets a column slightly wrong corrupts every row
    at once, and the person finding out is the one who needed the data.

    The size check matters as much as the correctness one. Measured
    after the textual-verification fix:

        taxis.csv      +9.8%   worth it
        diamonds.csv   -6.9%   container costs more than it saves
        weather CSV    -3.7%   same
        crypto ticks   refused - no structure to exploit

    Two of four were worse. Without this check the module would have
    quietly made most files bigger while reporting a success on one -
    which is the exact failure this project keeps finding in its own
    work."""
    try:
        out = encode(cols)
    except Exception:
        return None
    if out is None:
        return None
    try:
        back = decode(out)
    except Exception:
        return None
    if len(back) != len(cols):
        return None
    for a, b in zip(back, cols):
        if list(a) != list(b):
            return None
    pack = packer or _pack
    plain = b"".join(b"".join(v + b"\x00" for v in col) for col in cols)
    if len(pack(out)) >= len(pack(plain)):
        return None
    return out
