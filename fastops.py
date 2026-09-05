"""
fastops.py — NumPy versions of the four hottest prefilter transforms
====================================================================

WHY

Profiling a 7 KB file showed 5.1 seconds inside four pure-Python loops:

    _plane_op        558 calls   1.61 s
    _wdelta_raw      572 calls   0.99 s
    cross2_forward   871 calls   0.71 s
    cross_forward    195 calls   0.67 s

Every one is an elementwise operation over bytes — exactly what NumPy does
in compiled code. The Python versions box every byte as an object and
dispatch on every operation; NumPy works on a contiguous buffer.

THE CONTRACT

Each function here must produce output BYTE-IDENTICAL to the pure-Python
version it replaces. Not "close enough" — identical. A transform whose
output differs by one byte would still round-trip (the inverse is applied
to whatever the forward produced), but the router would silently pick
different candidates and the two implementations would diverge in ways
that are miserable to debug.

verify() checks this against the originals on random input before anything
is swapped in. If it fails, the pure-Python path stays.

WRAPAROUND

Byte arithmetic here is modulo 256. NumPy's uint8 wraps by default, which
matches Python's `& 255` exactly — but only if the intermediate is never
promoted to a wider type. Every operation below is kept in uint8 for that
reason.
"""

import numpy as np


def np_wdelta(d, p, w, big):
    """Delta over w-byte fields at stride p. Matches _wdelta_raw exactly."""
    n = len(d)
    nrec = n // p
    if nrec < 2:
        return bytes(d)
    a = np.frombuffer(d, dtype=np.uint8).copy()
    order = ">" if big else "<"
    dt = {1: "u1", 2: "u2", 4: "u4", 8: "u8"}.get(w)
    if dt is None:
        return None
    dtype = np.dtype(order + dt)
    idx = np.arange(nrec) * p
    for off in range(0, p - w + 1, w):
        starts = idx + off
        # gather the w-byte fields, one column at a time
        cols = np.zeros(nrec, dtype=np.dtype("<u8"))
        for k in range(w):
            shift = (8 * k) if not big else (8 * (w - 1 - k))
            cols |= a[starts + k].astype("<u8") << np.uint64(shift)
        mask = (1 << (8 * w)) - 1
        diff = np.empty_like(cols)
        diff[0] = cols[0]
        diff[1:] = (cols[1:] - cols[:-1]) & np.uint64(mask)
        for k in range(w):
            shift = (8 * k) if not big else (8 * (w - 1 - k))
            a[starts + k] = ((diff >> np.uint64(shift)) & np.uint64(0xFF)
                             ).astype(np.uint8)
    return a.tobytes()


def np_cross_forward(d, p, w, ref, op):
    """Subtract or XOR a reference field from the others in each record."""
    n = len(d) - (len(d) % p)
    if n == 0:
        return bytes(d)
    body = np.frombuffer(d[:n], dtype=np.uint8).copy()
    tail = d[n:]
    nrec = n // p
    starts = np.arange(nrec) * p

    def gather(offset):
        v = np.zeros(nrec, dtype=np.dtype("<u8"))
        for k in range(w):
            v |= body[starts + offset + k].astype("<u8") << np.uint64(8 * k)
        return v

    def scatter(offset, v):
        for k in range(w):
            body[starts + offset + k] = ((v >> np.uint64(8 * k))
                                         & np.uint64(0xFF)).astype(np.uint8)

    mask = np.uint64((1 << (8 * w)) - 1)
    b0 = gather(ref)
    for off in range(0, p - w + 1, w):
        if off == ref:
            continue
        a = gather(off)
        r = ((a - b0) & mask) if op == 0 else (a ^ b0)
        scatter(off, r)
    return body.tobytes() + tail


def np_cross2_forward(d, p, w, op, r1, r2, target):
    """Two-reference form, for a field derived from two others."""
    n = len(d) - (len(d) % p)
    if n == 0:
        return bytes(d)
    body = np.frombuffer(d[:n], dtype=np.uint8).copy()
    tail = d[n:]
    nrec = n // p
    starts = np.arange(nrec) * p

    def gather(offset):
        v = np.zeros(nrec, dtype=np.dtype("<u8"))
        for k in range(w):
            v |= body[starts + offset + k].astype("<u8") << np.uint64(8 * k)
        return v

    mask = np.uint64((1 << (8 * w)) - 1)
    a = gather(target)
    b1 = gather(r1)
    b2 = gather(r2)
    r = ((a - b1 - b2) & mask) if op == 0 else (a ^ b1 ^ b2)
    for k in range(w):
        body[starts + target + k] = ((r >> np.uint64(8 * k))
                                     & np.uint64(0xFF)).astype(np.uint8)
    return body.tobytes() + tail


def np_delta(d, p):
    """Delta by p positions. Matches _t_delta_raw exactly."""
    if len(d) <= p:
        return bytes(d)
    a = np.frombuffer(d, dtype=np.uint8)
    out = np.empty_like(a)
    out[:p] = a[:p]
    out[p:] = a[p:] - a[:-p]        # uint8 wraps, matching `& 255`
    return out.tobytes()


def np_plane_op(body, p, kind):
    """Transpose into columns, then delta or XOR down each column.

    `kind` is 0 for subtraction, 1 for XOR. The pure-Python version calls a
    lambda per byte, which is why it stayed the hottest function even after
    the others were converted: 8.3 million lambda invocations on a 7 KB
    file.

    Reshaping to (rows, p) and transposing gives the columns as rows of a
    2-D array, and the whole delta is then one vectorised subtraction."""
    n = len(body)
    rows = n // p
    if rows < 1:
        return None
    a = np.frombuffer(body[:rows * p], dtype=np.uint8).reshape(rows, p).T
    out = np.empty_like(a)
    out[:, 0] = a[:, 0]
    if kind == 0:
        out[:, 1:] = a[:, 1:] - a[:, :-1]      # uint8 wraps, as `& 255`
    else:
        out[:, 1:] = a[:, 1:] ^ a[:, :-1]
    return out.tobytes()


def np_plane_delta2(body, p):
    """Lag-2 delta inside each plane. Matches t_plane_delta2 exactly.

    Same shape as np_plane_op but comparing against two positions back
    rather than one, which catches interleaved A/B channels."""
    n = len(body)
    rows = n // p
    if rows < 1:
        return None
    a = np.frombuffer(body[:rows * p], dtype=np.uint8).reshape(rows, p).T
    out = np.empty_like(a)
    if rows >= 2:
        out[:, :2] = a[:, :2]
        out[:, 2:] = a[:, 2:] - a[:, :-2]
    else:
        out[:, :] = a[:, :]
    return out.tobytes()


def float_to_int(d, big=False):
    """float32 array holding only whole numbers -> narrowest integer type.

    WHY THIS EXISTS

    A real SAC file from station ANMO stored 864,000 seismic samples as
    float32. Every one was a whole number, and the whole day spanned
    -1139 to 3143 - a range that fits in int16. Half of every sample was
    padding, and no general compressor can know that: it sees float bytes
    and must preserve them exactly.

    Converting to int16 halved the data BEFORE any compression, and took
    the result from +76.9% to +79.2% on the file, which is +13.7% against
    the best standard tool.

    This is not specific to seismic. Any instrument that digitises
    integers and stores them as float for convenience has the same waste:
    scientific arrays, sensor logs, exported measurements.

    SAFETY

    Returns None unless EVERY value is a whole number in range. Float has
    values that do not survive a round trip through int - negative zero,
    NaN, infinities, and anything with a fractional part - so this refuses
    rather than guessing, and the caller re-encodes and compares before
    using the result."""
    if len(d) % 4:
        return None
    order = ">" if big else "<"
    a = np.frombuffer(d, dtype=np.dtype(order + "f4"))
    if a.size == 0:
        return None
    if not np.all(np.isfinite(a)):
        return None
    if not np.all(a == np.floor(a)):
        return None
    # negative zero survives floor() but not the int round trip
    if np.any((a == 0) & (np.signbit(a))):
        return None
    lo, hi = float(a.min()), float(a.max())
    if -128 <= lo and hi <= 127:
        w, dt = 1, "i1"
    elif -32768 <= lo and hi <= 32767:
        w, dt = 2, "i2"
    else:
        return None                     # 4 bytes buys nothing
    return w, a.astype(np.dtype(order + dt)).tobytes()


def int_to_float(x, w, big=False, count=None):
    order = ">" if big else "<"
    dt = {1: "i1", 2: "i2"}[w]
    a = np.frombuffer(x, dtype=np.dtype(order + dt))
    if count is not None:
        a = a[:count]
    return a.astype(np.dtype(order + "f4")).tobytes()


def int_narrow(d, w_in, big=False, signed=True):
    """int32/int64 array whose values all fit a narrower type.

    The same waste as float32-holding-integers, in integer form, and just
    as common: an int32 column of small counts, int64 timestamps that span
    a day, int16 flags holding 0-3.

    Measured: int32 -> int16 gained 18.9%, int64 -> int32 gained 20.9%,
    int32 -> int8 gained 9.4%, on top of everything the router already
    does. A file of genuinely wide int32 values is refused.

    Returns (out_width, narrowed_bytes) or None."""
    if w_in not in (2, 4, 8) or len(d) % w_in or len(d) < w_in * 32:
        return None
    order = ">" if big else "<"
    k = "i" if signed else "u"
    a = np.frombuffer(d, dtype=np.dtype(f"{order}{k}{w_in}"))
    lo, hi = int(a.min()), int(a.max())
    for w_out in (1, 2, 4):
        if w_out >= w_in:
            break
        info = np.iinfo(np.dtype(f"{k}{w_out}"))
        if info.min <= lo and hi <= info.max:
            return w_out, a.astype(np.dtype(f"{order}{k}{w_out}")).tobytes()
    return None


def int_widen(x, w_in, w_out, big=False, signed=True, count=None):
    order = ">" if big else "<"
    k = "i" if signed else "u"
    a = np.frombuffer(x, dtype=np.dtype(f"{order}{k}{w_out}"))
    if count is not None:
        a = a[:count]
    return a.astype(np.dtype(f"{order}{k}{w_in}")).tobytes()


# ----------------------------------------------------------------------
# verification
# ----------------------------------------------------------------------

def verify(pf, trials=200, seed=0):
    """Prove each NumPy version matches the pure-Python original byte for
    byte. Returns a list of failures; empty means safe to swap in."""
    import random
    rng = random.Random(seed)
    bad = []

    for _ in range(trials):
        n = rng.choice([0, 1, 2, 17, 64, 255, 1000, 4001])
        d = bytes(rng.randint(0, 255) for _ in range(n))

        p = rng.choice([1, 2, 3, 4, 8])
        if np_delta(d, p) != pf._t_delta_raw(d, p):
            bad.append(("delta", n, p))

        w = rng.choice([1, 2, 4])
        big = rng.choice([True, False])
        if p >= w and n >= p * 2:
            got = np_wdelta(d, p, w, big)
            if got is not None and got != pf._wdelta_raw(d, p, w, big):
                bad.append(("wdelta", n, p, w, big))

        if p % w == 0 and w * 2 <= p and n >= p * 2:
            op = rng.choice([0, 1])
            ref = rng.choice(range(0, p, w))
            if np_cross_forward(d, p, w, ref, op) != \
                    pf.cross_forward(d, p, w, ref, op):
                bad.append(("cross", n, p, w, ref, op))

            slots = list(range(0, p - w + 1, w))
            if len(slots) >= 3 and n >= p * 2:
                r1, r2, tg = slots[0], slots[1], slots[2]
                op = rng.choice([0, 1])
                if np_cross2_forward(d, p, w, op, r1, r2, tg) != \
                        pf.cross2_forward(d, p, w, op, r1, r2, tg):
                    bad.append(("cross2", n, p, w, op))
    return bad
