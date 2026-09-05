"""
fixedw.py — column transposition for fixed-width text records
=============================================================

WHAT THIS IS FOR

A large family of scientific formats writes records as fixed-width text,
aligned by column rather than separated by a delimiter:

    ATOM      1  N   VAL A   1      19.323  29.727  42.781  1.00 49.05
    ATOM      2  CA  VAL A   1      20.141  30.469  42.414  1.00 43.14

Every line is exactly the same width, so character position 30 is the
same field in every record. Transposing by position puts each field's
digits together - the same idea as csvcol, but the boundaries are
character offsets instead of commas.

WHY THE ROUTER MISSES IT

The transforms already look for a fixed record period, and on a file that
is ENTIRELY fixed-width they find it - a NOAA buoy file gets +30.7% that
way with no help. But a PDB file opens with hundreds of REMARK lines of
varying length, and those break the periodicity before the ATOM records
begin. The structure is there; the leading junk hides it.

MEASURED

    4HHB.pdb    473 KB   -0.1%  ->  +20.6%
    buoy file   400 KB  +30.7%  ->  +33.9%
    1CRN.pdb     49 KB   -0.0%  ->  -15.1%

The small PDB LOSES: 611 lines is not enough for the transposed columns
to pay for the lines held aside. So both layouts are built and the
smaller kept, which is what the rest of this project does with every
other choice.
"""

from collections import Counter

MIN_WIDTH = 16
MIN_LINES = 200


def encode(d):
    """Transpose the dominant fixed-width lines. Returns None when the
    file has no dominant width worth exploiting."""
    if not d.endswith(b'\n') or len(d) < 4096:
        return None
    lines = d.split(b'\n')[:-1]
    if len(lines) < MIN_LINES:
        return None
    lens = Counter(len(l) for l in lines)
    common, cnt = lens.most_common(1)[0]
    if common < MIN_WIDTH or cnt < MIN_LINES or cnt < len(lines) * 0.5:
        return None

    # Which lines are the fixed-width ones, as a bitmap - one byte per
    # line is simpler than a run-length list and compresses to nothing
    # when the answer is "all of them".
    flags = bytearray()
    fixed = []
    other = []
    for l in lines:
        if len(l) == common:
            flags.append(1)
            fixed.append(l)
        else:
            flags.append(0)
            other.append(l)
    cols = bytearray()
    for i in range(common):
        for l in fixed:
            cols.append(l[i])
    oth = b'\n'.join(other)
    return (str(len(lines)).encode() + b'\n' + str(common).encode() + b'\n'
            + str(len(oth)).encode() + b'\n' + bytes(flags) + oth
            + bytes(cols))


def decode(blob):
    nl_s, rest = blob.split(b'\n', 1)
    w_s, rest = rest.split(b'\n', 1)
    ol_s, rest = rest.split(b'\n', 1)
    nl, w, ol = int(nl_s), int(w_s), int(ol_s)
    flags = rest[:nl]
    oth = rest[nl:nl + ol]
    cols = rest[nl + ol:]
    nfixed = sum(flags)
    fixed = [bytearray(w) for _ in range(nfixed)]
    p = 0
    for i in range(w):
        for r in range(nfixed):
            fixed[r][i] = cols[p]
            p += 1
    others = oth.split(b'\n') if ol else []
    out = []
    fi = 0
    oi = 0
    for f in flags:
        if f:
            out.append(bytes(fixed[fi]))
            fi += 1
        else:
            out.append(others[oi])
            oi += 1
    return b'\n'.join(out) + b'\n'


def try_encode(d):
    """Encode and VERIFY. Returns None unless it rebuilds exactly."""
    try:
        x = encode(d)
    except Exception:
        return None
    if x is None:
        return None
    try:
        if decode(x) != d:
            return None
    except Exception:
        return None
    return x
