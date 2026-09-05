"""
fasta.py — 2-bit packing for DNA sequence files
===============================================

WHAT THIS IS FOR

A FASTA file stores DNA as the letters A, C, G and T - one byte each.
Four symbols need two bits, so three quarters of every byte is padding.
General compressors recover some of that through string matching, but
they cannot see that the alphabet is only four wide.

MEASURED ON THE REAL E. COLI GENOME (GCF_000005845.2, first 150 KB)

    gzip -9        45,029
    xz -9e         42,708
    bzip2 -9       42,717
    lzma -9        42,640
    ours, before   41,995   +1.5%
    2-bit packed   37,117  +13.0%

The 2-bit floor for 150,000 bases is 37,500 bytes, so the packed form is
essentially AT the floor. Compressing it further does not help - lzma on
the packed stream gives 37,124, slightly WORSE than leaving it alone,
because packed DNA has almost no redundancy left. That is the correct
outcome, not a failure.

WHAT IT HANDLES

Real FASTA is not pure ACGT. It carries a header line, wraps sequence at
a fixed width, and contains occasional ambiguity codes - N for unknown, R
for purine, and others. All three are stored explicitly:

  the header verbatim
  the common line length, plus a list of the lines that differ
  the position and byte of every character that is not A, C, G or T

The exceptions cost five bytes each, so a sequence that is mostly N would
be larger than the original. try_encode compares against the input and
refuses when that happens.
"""

import struct
from collections import Counter

CODE = {65: 0, 67: 1, 71: 2, 84: 3}          # A C G T
BACK = {0: 65, 1: 67, 2: 71, 3: 84}


def looks_like_fasta(d):
    if len(d) < 1024 or not d.startswith(b'>'):
        return False
    nl = d.find(b'\n')
    if nl < 1 or nl > 4096:
        return False
    sample = d[nl + 1:nl + 4001].replace(b'\n', b'')
    if not sample:
        return False
    acgt = sum(1 for b in sample if b in CODE)
    return acgt > len(sample) * 0.9


def encode(d):
    if not looks_like_fasta(d):
        return None
    lines = d.split(b'\n')
    hdr = lines[0]
    tail = b'\n' if d.endswith(b'\n') else b''
    seq_lines = lines[1:-1] if tail else lines[1:]
    if not seq_lines:
        return None
    body = b''.join(seq_lines)
    lens = [len(l) for l in seq_lines]

    exc = []
    bits = bytearray()
    acc = 0
    n = 0
    for i, b in enumerate(body):
        v = CODE.get(b)
        if v is None:
            exc.append((i, b))
            v = 0
        acc = (acc << 2) | v
        n += 2
        if n == 8:
            bits.append(acc)
            acc = 0
            n = 0
    if n:
        bits.append(acc << (8 - n))

    common = Counter(lens).most_common(1)[0][0]
    odd = [(i, l) for i, l in enumerate(lens) if l != common]

    out = bytearray()
    out += struct.pack('>H', len(hdr)) + hdr
    out += struct.pack('>B', 1 if tail else 0)
    out += struct.pack('>I', len(body))
    out += struct.pack('>I', len(exc))
    for i, b in exc:
        out += struct.pack('>IB', i, b)
    out += struct.pack('>H', common) + struct.pack('>I', len(lens))
    out += struct.pack('>I', len(odd))
    for i, l in odd:
        out += struct.pack('>IH', i, l)
    return bytes(out) + bytes(bits)


def decode(blob):
    p = 0
    hl, = struct.unpack_from('>H', blob, p); p += 2
    hdr = blob[p:p + hl]; p += hl
    tail_flag, = struct.unpack_from('>B', blob, p); p += 1
    nbody, = struct.unpack_from('>I', blob, p); p += 4
    nexc, = struct.unpack_from('>I', blob, p); p += 4
    exc = {}
    for _ in range(nexc):
        i, b = struct.unpack_from('>IB', blob, p); p += 5
        exc[i] = b
    common, = struct.unpack_from('>H', blob, p); p += 2
    nlines, = struct.unpack_from('>I', blob, p); p += 4
    nodd, = struct.unpack_from('>I', blob, p); p += 4
    lens = [common] * nlines
    for _ in range(nodd):
        i, l = struct.unpack_from('>IH', blob, p); p += 6
        lens[i] = l
    bits = blob[p:]

    body = bytearray()
    for i in range(nbody):
        byte = bits[i >> 2]
        v = (byte >> (6 - 2 * (i & 3))) & 3
        body.append(exc.get(i, BACK[v]))
    out = [hdr]
    pos = 0
    for l in lens:
        out.append(bytes(body[pos:pos + l]))
        pos += l
    return b'\n'.join(out) + (b'\n' if tail_flag else b'')


def try_encode(d):
    """Encode, VERIFY, and refuse if it did not shrink."""
    try:
        x = encode(d)
    except Exception:
        return None
    if x is None or len(x) >= len(d):
        return None
    try:
        if decode(x) != d:
            return None
    except Exception:
        return None
    return x
