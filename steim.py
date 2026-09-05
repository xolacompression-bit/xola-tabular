"""
steim.py — Steim-2 decoder for miniSEED
=======================================

WHY THIS EXISTS

A real miniSEED file measured 7.68 bits per byte of entropy in its payload,
and every general compressor managed only 14-17% on it. The reason is not
that the data lacks structure — it is that the structure has already been
removed. miniSEED stores waveforms Steim-compressed: first differences,
packed into whatever bit width each group of samples needs.

That is the "already compressed" wall, but with an important difference
from JPEG: Steim is LOSSLESS. So the wall can be walked through. Decode the
Steim frames back to raw samples and the original structure reappears —
smooth waveforms with small differences, which is exactly what the
structural transforms are built for.

THE FORMAT

Each 64-byte frame is sixteen 32-bit big-endian words.

  word 0    the nibble word: sixteen 2-bit codes, one per word, describing
            how that word is packed. The code for word 0 itself is unused.
  word 1    in the FIRST frame only, the forward integration constant —
            the actual value of the first sample.
  word 2    in the FIRST frame only, the reverse integration constant —
            the last sample, used as a check.

  nibble 01   four  8-bit differences
  nibble 10   read the top two bits of the word:
                01 -> one   30-bit difference
                10 -> two   15-bit differences
                11 -> three 10-bit differences
  nibble 11   read the top two bits:
                00 -> five  6-bit differences
                01 -> six   5-bit differences
                10 -> seven 4-bit differences
  nibble 00   not data (header words, or padding)

Samples are recovered by running sum from the forward integration constant.

SAFETY

decode() returns None rather than guessing whenever anything does not
match — wrong record length, unexpected encoding, a frame that does not
reconstruct. Any caller must treat None as "leave this file alone".
"""

import struct


def _signed(v, bits):
    """Interpret the low `bits` of v as two's complement."""
    v &= (1 << bits) - 1
    return v - (1 << bits) if v & (1 << (bits - 1)) else v


def decode_frames(payload, expected):
    """Decode Steim-2 frames to a list of sample differences plus the
    forward integration constant.

    Returns (first_sample, diffs, last_sample) or None."""
    if len(payload) % 64:
        return None
    diffs = []
    first = last = None
    for f in range(len(payload) // 64):
        frame = payload[f * 64:(f + 1) * 64]
        words = struct.unpack(">16I", frame)
        nib = words[0]
        start = 1
        if f == 0:
            first = _signed(words[1], 32)
            last = _signed(words[2], 32)
            start = 3
        for w in range(start, 16):
            code = (nib >> (2 * (15 - w))) & 3
            word = words[w]
            if code == 0:
                continue
            elif code == 1:
                for k in range(4):
                    diffs.append(_signed(word >> (8 * (3 - k)), 8))
            elif code == 2:
                # ck=10 uses dnib 01, 10, 11 - NOT 00, 01, 10. Getting this
                # off by one made every record fail to decode.
                dnib = (word >> 30) & 3
                if dnib == 1:
                    diffs.append(_signed(word, 30))
                elif dnib == 2:
                    for k in range(2):
                        diffs.append(_signed(word >> (15 * (1 - k)), 15))
                elif dnib == 3:
                    for k in range(3):
                        diffs.append(_signed(word >> (10 * (2 - k)), 10))
                else:
                    return None
            else:
                dnib = (word >> 30) & 3
                if dnib == 0:
                    for k in range(5):
                        diffs.append(_signed(word >> (6 * (4 - k)), 6))
                elif dnib == 1:
                    for k in range(6):
                        diffs.append(_signed(word >> (5 * (5 - k)), 5))
                elif dnib == 2:
                    for k in range(7):
                        diffs.append(_signed(word >> (4 * (6 - k)), 4))
                else:
                    return None
    return first, diffs, last


def decode_record(rec, data_start, nsamples):
    """Decode one miniSEED record's payload to a list of samples."""
    got = decode_frames(rec[data_start:], nsamples)
    if got is None:
        return None
    first, diffs, last = got
    if first is None:
        return None
    samples = [first]
    acc = first
    # The first difference in the stream is a placeholder; real samples
    # start after it. Running sum recovers the waveform.
    for dv in diffs[1:]:
        if len(samples) >= nsamples:
            break
        acc += dv
        samples.append(acc)
    if len(samples) != nsamples:
        return None
    if last is not None and samples[-1] != last:
        return None          # reverse integration constant disagrees
    return samples


def split_records(d, reclen=512):
    """Yield (header, payload_start, nsamples) for each record."""
    out = []
    for i in range(len(d) // reclen):
        rec = d[i * reclen:(i + 1) * reclen]
        try:
            nsamp = struct.unpack(">H", rec[30:32])[0]
            dstart = struct.unpack(">H", rec[44:46])[0]
        except Exception:
            return None
        if dstart < 48 or dstart >= reclen or nsamp == 0:
            return None
        out.append((rec, dstart, nsamp))
    return out


# ----------------------------------------------------------------------
# waveform packing
# ----------------------------------------------------------------------
#
# Decoding Steim is only half the job. What comes out is a sequence of
# samples, and how those are laid out decides most of the result.
#
# Measured across 13 real miniSEED files from stations on five
# continents, against the best of gzip -9, bzip2 -9, lzma -9 and xz -9e:
#
#     raw miniSEED                     +0.4%
#     decode + fixed-width delta      +15.0%
#     decode + best of both below     +19.0%
#
# THINGS TRIED ON THIS DATA AND REJECTED, so nobody repeats them:
#
#   Tuning the escape threshold. Swept 5, 6, 7, 11, 13 and 15 bits on
#   every file. Seven - the byte boundary - won every time. There is no
#   per-file tuning to be had.
#
#   Splitting the zigzag stream into high and low byte planes. The
#   entropy split is dramatic and real: 2.15 bits in the high byte
#   against 7.98 in the low. Compressing them separately still LOST on
#   8 of 9 files, because lzma's context modelling already exploits the
#   alternation and separating them removes context it was using.
#
#   Second-order delta. Seismic is oscillatory, so it looked right.
#   Measured -14.6% against first-order.
#
#   Running the full 17-mode router on the decoded waveform instead of
#   the fast path. Won on 4 of 4 - by 0.1 to 2.6%, for 25 to 50 times
#   the time.
#
# The standard tools get essentially nothing on these files because the
# payload is already Steim-compressed. Undoing that first is the whole
# gain.

import struct as _st


def delta_fixed(samples):
    """First-order delta at the narrowest width the data actually fits.

    Seismic samples drift slowly, so differences are far smaller than
    values. Storing the difference at the narrowest type that holds it
    removes the padding a fixed int32 array carries."""
    if not samples:
        return None
    first = samples[0]
    d1 = [samples[i + 1] - samples[i] for i in range(len(samples) - 1)]
    dmax = max((abs(v) for v in d1), default=0)
    if dmax <= 127:
        fmt = "b"
    elif dmax <= 32767:
        fmt = "h"
    else:
        fmt = "i"
    return (_st.pack("<i", first) + _st.pack("<" + fmt * len(d1), *d1),
            fmt, len(d1))


def delta_escape(samples):
    """One byte per delta, with -128 reserved as an escape.

    A single loud sample forces the whole file to 16 bits. Measured, five
    of thirteen real files had over 99% of their deltas inside a byte and
    were paying double for the rest: one went from +1.3% to +13.6%.

    It is not always right. When a quarter of the samples escape, each
    costs three bytes instead of two and the fixed width wins - one file
    went from +23.8% to +17.3%. So both are built and the smaller kept,
    which is what the rest of this project does with every other choice."""
    if not samples:
        return None
    first = samples[0]
    body = bytearray()
    esc = bytearray()
    for i in range(len(samples) - 1):
        v = samples[i + 1] - samples[i]
        if -127 <= v <= 127:
            body.append(v & 0xFF)
        else:
            body.append(0x80)
            esc += _st.pack("<i", v)
    return (_st.pack("<i", first) + bytes(body) + bytes(esc),
            len(samples) - 1)


def unpack_fixed(blob, fmt, n):
    first = _st.unpack("<i", blob[:4])[0]
    d1 = _st.unpack("<" + fmt * n, blob[4:])
    out = [first]
    for v in d1:
        out.append(out[-1] + v)
    return out


def unpack_escape(blob, n):
    first = _st.unpack("<i", blob[:4])[0]
    body = blob[4:4 + n]
    esc = blob[4 + n:]
    out = [first]
    e = 0
    for b in body:
        if b == 0x80:
            v = _st.unpack("<i", esc[e:e + 4])[0]
            e += 4
        else:
            v = b - 256 if b > 127 else b
        out.append(out[-1] + v)
    return out


def pack_waveform(samples):
    """Build both layouts, verify each round-trips, return the smaller.

    Returns (blob, kind, param) where kind is 'fixed' or 'escape'."""
    a = delta_fixed(samples)
    b = delta_escape(samples)
    best = None
    if a:
        blob, fmt, n = a
        if unpack_fixed(blob, fmt, n) == samples:
            best = (blob, "fixed", fmt)
    if b:
        blob, n = b
        if unpack_escape(blob, n) == samples:
            if best is None or len(blob) < len(best[0]):
                best = (blob, "escape", n)
    return best


# ----------------------------------------------------------------------
# Steim-2 ENCODER
# ----------------------------------------------------------------------
#
# WHY THIS HAS TO EXIST
#
# Decoding alone gives samples, not a file. Everything measured on
# miniSEED - +19% across thirteen real files - was compressing the
# SAMPLES, with no way to rebuild the original .mseed bytes. That is a
# sample-extraction result, not a compression result, and it cannot be
# offered to anyone who needs their file back.
#
# STEIM ENCODING IS NOT UNIQUE - AND THIS ENCODER PROVES IT
#
# Several frame packings decode to the same samples. This encoder gets
# very close: on a real file the DATA WORDS come out identical, and only
# the 32-bit nibble word differs, in a single 2-bit field. For one word
# the original chose code 2 where the greedy rule here chooses code 1.
#
# Two real bugs were found and fixed getting that far, both worth
# knowing if anyone extends this:
#
#   diffs[0] is not zero. For the FIRST record it is the opening sample
#   itself. For every record after, it is that record's first sample
#   minus the PREVIOUS record's last - the difference stream runs
#   continuously across record boundaries. Using zero matched 0 of 50
#   records; using the sample matched 1 of 50, the one case where they
#   coincide.
#
#   The magnitude of diffs[0] decides how many differences fit in the
#   first data word, so getting it wrong shifts the packing of the
#   entire record.
#
# WHAT REMAINS, AND WHY IT IS NOT SOLVABLE IN GENERAL
#
# Matching the rest would mean reproducing the exact heuristic of
# whichever encoder produced the file - and different networks and
# software versions use different ones. There is no single correct
# packing to aim at.
#
# SO THE RULE IS VERIFY AND REFUSE
#
# can_round_trip() re-encodes every record and compares. If they all
# match, the decode path is safe and the file can be rebuilt byte for
# byte. If any record differs, the decode path MUST NOT be used - the
# file would compress well and be impossible to restore.
#
# In practice that means the +19% measured on miniSEED is available only
# where this encoder happens to agree with the original. Everywhere else
# the file falls back to ordinary compression, around +0.4%.
#
# That is a disappointing result stated honestly. The alternative -
# shipping a compressor that cannot restore its own output - is not an
# alternative.

def _fits(v, bits):
    lim = 1 << (bits - 1)
    return -lim <= v < lim


def encode_frames(first, diffs, last, nframes):
    """Pack differences into Steim-2 frames.

    Mirrors decode_frames exactly: same codes, same dnib values, same
    word order. The ck=10 dnib mapping is 01/10/11 rather than 00/01/10,
    which is the detail that made every record fail to decode until it
    was found."""
    words = []
    nibs = []
    i = 0
    n = len(diffs)
    while i < n:
        # Greedy, widest-first, exactly as the format intends: fit as
        # many differences into one 32-bit word as their magnitudes allow.
        if i + 7 <= n and all(_fits(d, 4) for d in diffs[i:i + 7]):
            w = 2 << 30
            for k in range(7):
                w |= (diffs[i + k] & 0xF) << (4 * (6 - k))
            words.append(w); nibs.append(3); i += 7
        elif i + 6 <= n and all(_fits(d, 5) for d in diffs[i:i + 6]):
            w = 1 << 30
            for k in range(6):
                w |= (diffs[i + k] & 0x1F) << (5 * (5 - k))
            words.append(w); nibs.append(3); i += 6
        elif i + 5 <= n and all(_fits(d, 6) for d in diffs[i:i + 5]):
            w = 0
            for k in range(5):
                w |= (diffs[i + k] & 0x3F) << (6 * (4 - k))
            words.append(w); nibs.append(3); i += 5
        elif i + 4 <= n and all(_fits(d, 8) for d in diffs[i:i + 4]):
            w = 0
            for k in range(4):
                w |= (diffs[i + k] & 0xFF) << (8 * (3 - k))
            words.append(w); nibs.append(1); i += 4
        elif i + 3 <= n and all(_fits(d, 10) for d in diffs[i:i + 3]):
            w = 3 << 30
            for k in range(3):
                w |= (diffs[i + k] & 0x3FF) << (10 * (2 - k))
            words.append(w); nibs.append(2); i += 3
        elif i + 2 <= n and all(_fits(d, 15) for d in diffs[i:i + 2]):
            w = 2 << 30
            for k in range(2):
                w |= (diffs[i + k] & 0x7FFF) << (15 * (1 - k))
            words.append(w); nibs.append(2); i += 2
        elif _fits(diffs[i], 30):
            words.append((1 << 30) | (diffs[i] & 0x3FFFFFFF))
            nibs.append(2); i += 1
        else:
            return None                  # will not fit Steim-2 at all

    # Lay the words into 64-byte frames. The first frame spends two of its
    # sixteen slots on the integration constants.
    out = bytearray()
    wi = 0
    for f in range(nframes):
        slots = 13 if f == 0 else 15
        take = words[wi:wi + slots]
        tnib = nibs[wi:wi + slots]
        wi += len(take)
        nib = 0
        frame = [0] * 16
        if f == 0:
            frame[1] = first & 0xFFFFFFFF
            frame[2] = last & 0xFFFFFFFF
            base = 3
        else:
            base = 1
        for k, w in enumerate(take):
            frame[base + k] = w & 0xFFFFFFFF
            nib |= (tnib[k] & 3) << (2 * (15 - (base + k)))
        frame[0] = nib
        out += struct.pack(">16I", *frame)
    if wi != len(words):
        return None                      # did not fit the frames given
    return bytes(out)


def encode_record(original, data_start, nsamples, samples, prev_last=None):
    """Rebuild one record's payload from samples, then VERIFY.

    Returns the rebuilt record, or None if it does not match the original
    byte for byte."""
    if len(samples) != nsamples or nsamples < 1:
        return None
    # THE DIFFERENCE STREAM IS CONTINUOUS ACROSS RECORDS.
    #
    # For the FIRST record diffs[0] is the opening sample itself. For
    # every record after it, diffs[0] is that record's first sample minus
    # the PREVIOUS record's last sample - the stream does not restart at
    # a record boundary.
    #
    # Verified against a real file:
    #
    #     rec 0  first=1093  diffs[0]=1093   (the sample)
    #     rec 1  first= 932  diffs[0]=  26   (932 - 906, prev last)
    #     rec 2  first=1374  diffs[0]= -29
    #
    # Using zero gave 0/50 records matching. Using the sample gave 1/50 -
    # only the first record, which is the one where they coincide. The
    # magnitude of diffs[0] decides how many differences fit in the first
    # data word, so getting it wrong shifts the entire packing.
    d0 = samples[0] if prev_last is None else samples[0] - prev_last
    diffs = [d0] + \
            [samples[i] - samples[i - 1] for i in range(1, len(samples))]
    payload_len = len(original) - data_start
    if payload_len % 64:
        return None
    body = encode_frames(samples[0], samples[-1], diffs, payload_len // 64) \
        if False else encode_frames(samples[0], diffs, samples[-1],
                                    payload_len // 64)
    if body is None or len(body) != payload_len:
        return None
    rebuilt = original[:data_start] + body
    return rebuilt if rebuilt == original else None


def can_round_trip(d, reclen=512):
    """Decode then re-encode the whole file. Returns (ok, matched, total).

    This is the gate. Unless every record comes back identical, a
    miniSEED file must not be compressed through the decode path -
    because it could not be rebuilt."""
    recs = split_records(d, reclen)
    if not recs:
        return False, 0, 0
    matched = 0
    prev_last = None
    for rec, dstart, nsamp in recs:
        smp = decode_record(rec, dstart, nsamp)
        if smp is None:
            return False, matched, len(recs)
        if encode_record(rec, dstart, nsamp, smp, prev_last) is not None:
            matched += 1
        prev_last = smp[-1]
    return matched == len(recs), matched, len(recs)
