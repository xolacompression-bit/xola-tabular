"""
formats.py — recognise the container, undo what it did, then compress
=====================================================================

THE ARCHITECTURE THIS BELONGS TO

    scan      is there anything here at all?      (skip if not)
    formats   what IS this, and what did it do?   (undo it)     <- this file
    router    which transform packs it smallest?  (measure all)

Each layer answers a different question, and only the middle one is
prediction. The router must keep measuring everything, because prediction
loses: scan's detected period and the router's winning period disagree on
three files in five, and the router's is the one that produced the smaller
output.

WHY FORMAT DETECTION IS DIFFERENT

Guessing which transform will win costs ratio. Recognising that a file is
Steim-encoded miniSEED does not - it is a fact about the file, checkable,
and undoing it exposes structure that no amount of transform search can
reach.

The two biggest gains in this project both came from here:

    miniSEED  Steim-2 decoded    +17.4% -> +23.0%
    SAC       float32 -> int16   +76.9% -> +79.2%

Neither is a cleverer compressor. Both are undoing something the format
did for its own convenience.

SAFETY — WHERE THE CERTAINTY BELONGS

The detector may be optimistic. The TRANSFORM must be certain.

That division was found by testing. Requiring SAC's declared length to
match the file exactly refused every truncated file - and the site measures
large files on a 192 KB sample, so it refused the case it most needed.
Relaxing the length check then let ordinary sensor binary through as SAC,
which would have been a corruption. The fix was an invariant, not a
tolerance: SAC's header-version field has been 6 for decades, and checking
it costs nothing.

One optimistic case remains on purpose: a u32 sensor stream is still
flagged as a possible float32 array. It is harmless, because
float_to_int() re-verifies every value and refuses - a false positive
costs one rejected attempt, not a corrupted file. Tightening the detector
until it never guesses would cost real detections, and truncated SAC is
exactly what an over-strict check throws away.
"""

import struct


# ----------------------------------------------------------------------
# detection
# ----------------------------------------------------------------------

def detect(d):
    """Return a dict describing the format, or None.

    Ordered most-specific first. Every check must be structural - a magic
    number alone is not enough, because a magic number can occur by
    chance in binary data."""
    if len(d) < 64:
        return None

    r = _miniseed(d)
    if r:
        return r
    r = _sac(d)
    if r:
        return r
    r = _wav(d)
    if r:
        return r
    r = _float_array(d)
    if r:
        return r
    return None


def _miniseed(d):
    """miniSEED: fixed records, each starting with a 6-digit sequence
    number, carrying a blockette 1000 that names the encoding."""
    if not d[:6].isdigit():
        return None
    if d[6:7] not in (b"D", b"R", b"Q", b"M"):
        return None
    try:
        boff = struct.unpack(">H", d[46:48])[0]
    except Exception:
        return None
    if not (48 <= boff < 512):
        return None
    try:
        btype = struct.unpack(">H", d[boff:boff + 2])[0]
    except Exception:
        return None
    if btype != 1000:
        return None
    enc = d[boff + 4]
    reclen = 1 << d[boff + 6]
    if reclen not in (256, 512, 1024, 2048, 4096, 8192):
        return None
    # the next record must also look like a record
    if len(d) > reclen and not d[reclen:reclen + 6].isdigit():
        return None
    names = {0: "ASCII", 1: "int16", 3: "int32", 4: "float32",
             5: "float64", 10: "Steim-1", 11: "Steim-2", 19: "Steim-3"}
    return {"format": "miniSEED", "reclen": reclen, "encoding": enc,
            "encoding_name": names.get(enc, f"unknown({enc})"),
            "records": len(d) // reclen,
            "action": ("steim-decode" if enc in (10, 11) else "none"),
            "why": f"{names.get(enc, enc)} in {reclen}-byte records"}


def _sac(d):
    """SAC: 632-byte header whose npts field must match the file length
    exactly. That equality is what makes this safe - a coincidence would
    have to get the arithmetic right too."""
    if len(d) < 632 + 4:
        return None
    for big in (False, True):
        o = ">" if big else "<"
        try:
            npts = struct.unpack(o + "i", d[79 * 4:79 * 4 + 4])[0]
            nvhdr = struct.unpack(o + "i", d[76 * 4:76 * 4 + 4])[0]
            iftype = struct.unpack(o + "i", d[85 * 4:85 * 4 + 4])[0]
        except Exception:
            continue
        # nvhdr is the header version and has been 6 for decades. Without
        # this check, relaxing the length match for truncated files let
        # ordinary sensor binary through as "SAC" - a false positive that
        # would have corrupted it. A format needs an invariant, not just
        # a plausible length.
        if nvhdr != 6:
            continue
        if iftype not in (-12345, 1, 2, 3, 4, 5):
            continue
        if npts <= 0 or npts > 1 << 28:
            continue
        full = 632 + npts * 4
        if full == len(d):
            avail = npts
            note = ""
        elif full > len(d) and (len(d) - 632) % 4 == 0 and len(d) > 632 + 4096:
            # A TRUNCATED or sampled SAC file. The site measures large
            # files on a 192 KB sample, and requiring an exact length match
            # meant those were never recognised - the safety check was
            # refusing the very case it most needed to handle.
            #
            # Still safe: the header must declare MORE samples than are
            # present (never fewer), the remainder must divide evenly by 4,
            # and the float-to-int step re-verifies every value anyway.
            avail = (len(d) - 632) // 4
            note = " (truncated - measuring the part present)"
        else:
            continue
        return {"format": "SAC", "samples": avail, "declared": npts,
                "big_endian": big, "header": 632, "action": "float-to-int",
                "why": f"{avail:,} float32 samples after a 632-byte "
                       f"header{note}"}
    return None


def _wav(d):
    """RIFF/WAVE with PCM data."""
    if d[:4] != b"RIFF" or d[8:12] != b"WAVE":
        return None
    pos = 12
    fmt = None
    while pos + 8 <= len(d):
        cid = d[pos:pos + 4]
        try:
            sz = struct.unpack("<I", d[pos + 4:pos + 8])[0]
        except Exception:
            return None
        if cid == b"fmt " and pos + 8 + 16 <= len(d):
            af, ch, sr, br, ba, bits = struct.unpack(
                "<HHIIHH", d[pos + 8:pos + 8 + 16])
            fmt = (af, ch, bits)
        elif cid == b"data" and fmt:
            af, ch, bits = fmt
            if af != 1:
                return None                 # not PCM
            return {"format": "WAV", "channels": ch, "bits": bits,
                    "data_at": pos + 8, "data_len": min(sz, len(d) - pos - 8),
                    "action": "none",
                    "why": f"{bits}-bit PCM, {ch} channel(s)"}
        pos += 8 + sz + (sz & 1)
    return None


def _float_array(d):
    """A bare float32 array with no header - common in exported
    scientific data. Only claimed when the values are sane: finite, and
    not absurdly large, across a decent sample."""
    if len(d) < 4096 or len(d) % 4:
        return None
    try:
        import numpy as np
    except ImportError:
        return None
    for big in (False, True):
        a = np.frombuffer(d[:65536], dtype=np.dtype((">" if big else "<") + "f4"))
        if not np.all(np.isfinite(a)):
            continue
        mx = float(np.max(np.abs(a)))
        if mx == 0 or mx > 1e30:
            continue
        # plausible float data: exponents clustered, not uniform
        u = np.frombuffer(d[:65536], dtype=np.dtype((">" if big else "<") + "u4"))
        expo = (u >> 23) & 0xFF
        spread = len(np.unique(expo))
        if spread > 40:
            continue                        # too varied to be one signal
        return {"format": "float32 array", "big_endian": big,
                "header": 0, "action": "float-to-int",
                "why": f"{len(d) // 4:,} float32 values, "
                       f"{spread} distinct exponents"}
    return None


# ----------------------------------------------------------------------
# reporting
# ----------------------------------------------------------------------

def describe(d):
    r = detect(d)
    if not r:
        return "unrecognised container - the router handles it directly"
    act = {"steim-decode": "decode Steim first, then compress",
           "float-to-int": "narrow the floats to integers, then compress",
           "none": "no container to undo"}[r["action"]]
    return f"{r['format']}: {r['why']}\n  -> {act}"


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        with open(p, "rb") as f:
            d = f.read(4 * 1024 * 1024)
        print(f"\n{p}")
        print("  " + describe(d).replace("\n", "\n  "))
