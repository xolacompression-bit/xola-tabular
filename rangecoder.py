"""
Byte-oriented range coder (Subbotin carryless variant) with a static,
two-pass frequency model - the direct entropy-stage replacement for
huffman.py. Same job (compress a stream of int symbols), different
technique: fractional-bit costs instead of whole-bit Huffman codes.
"""

import struct
from lz77 import _bucket_encode, _bucket_decode, _bucket_extra_bits
from bitio import BitWriter, BitReader

TOP = 1 << 24
BOT = 1 << 16
MASK32 = 0xFFFFFFFF


class RangeEncoder:
    def __init__(self):
        self.low = 0
        self.range = MASK32
        self.out = bytearray()

    def encode(self, cum_freq: int, freq: int, tot_freq: int):
        r = self.range // tot_freq
        self.low = (self.low + r * cum_freq) & MASK32
        self.range = r * freq
        self._normalize()

    def _normalize(self):
        while True:
            if (self.low ^ (self.low + self.range)) & MASK32 < TOP:
                pass
            elif self.range < BOT:
                self.range = (-self.low) & (BOT - 1)
            else:
                break
            self.out.append((self.low >> 24) & 0xFF)
            self.low = (self.low << 8) & MASK32
            self.range = (self.range << 8) & MASK32

    def finish(self) -> bytes:
        for _ in range(4):
            self.out.append((self.low >> 24) & 0xFF)
            self.low = (self.low << 8) & MASK32
        return bytes(self.out)


class RangeDecoder:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.low = 0
        self.range = MASK32
        self.code = 0
        for _ in range(4):
            self.code = ((self.code << 8) | self._next_byte()) & MASK32

    def _next_byte(self) -> int:
        if self.pos < len(self.data):
            b = self.data[self.pos]
            self.pos += 1
            return b
        return 0

    def get_freq(self, tot_freq: int) -> int:
        self.r = self.range // tot_freq
        val = (self.code - self.low) // self.r
        return val if val < tot_freq else tot_freq - 1

    def decode(self, cum_freq: int, freq: int, tot_freq: int):
        r = self.r
        self.low = (self.low + r * cum_freq) & MASK32
        self.range = r * freq
        self._normalize()

    def _normalize(self):
        while True:
            if (self.low ^ (self.low + self.range)) & MASK32 < TOP:
                pass
            elif self.range < BOT:
                self.range = (-self.low) & (BOT - 1)
            else:
                break
            self.code = ((self.code << 8) | self._next_byte()) & MASK32
            self.low = (self.low << 8) & MASK32
            self.range = (self.range << 8) & MASK32


def _normalize_freqs(freqs: dict, target_total: int = 1 << 14):
    """Scale raw counts so every nonzero-count symbol keeps freq >= 1 and
    the total is close to target_total (range coder needs tot_freq < BOT)."""
    raw_total = sum(freqs.values())
    scaled = {}
    for sym, f in freqs.items():
        v = max(1, round(f * target_total / raw_total))
        scaled[sym] = v
    return scaled


def range_encode_symbols(symbols: list, target_total: int = 1 << 14) -> bytes:
    if not symbols:
        return struct.pack("<I I I I", 0, 0, 0, 0)

    from collections import Counter
    freqs = Counter(symbols)
    max_symbol = max(freqs.keys())
    num_slots = max_symbol + 1
    scaled = _normalize_freqs(freqs, target_total)
    tot_freq = sum(scaled.values())

    cum = [0] * (num_slots + 1)
    for sym in range(num_slots):
        cum[sym + 1] = cum[sym] + scaled.get(sym, 0)
    assert cum[num_slots] == tot_freq

    enc = RangeEncoder()
    for s in symbols:
        enc.encode(cum[s], cum[s + 1] - cum[s], tot_freq)
    data = enc.finish()

    # Compact table: bucket-encode each frequency value (0 for unused slots)
    # the same way LZ77 encodes lengths/distances - skewed bucket index in a
    # fixed 5 bits, near-uniform remainder as raw extra bits. Unused slots
    # (the common case in a sparse table) cost just 5 bits instead of 16.
    table_bw = BitWriter()
    for sym in range(num_slots):
        v = scaled.get(sym, 0)
        bucket, extra_bits, extra_value = _bucket_encode(v)
        table_bw.write_bits(bucket, 5)
        if extra_bits > 0:
            table_bw.write_bits(extra_value, extra_bits)
    table_bytes = table_bw.getvalue()

    header = struct.pack("<I I I I", num_slots, len(symbols), tot_freq, len(table_bytes))
    return header + table_bytes + data


def range_decode_symbols(blob: bytes) -> list:
    num_slots, n_items, tot_freq, table_len = struct.unpack("<I I I I", blob[0:16])
    if num_slots == 0:
        return []
    pos = 16
    table_bytes = blob[pos:pos + table_len]
    pos += table_len
    data = blob[pos:]

    tbr = BitReader(table_bytes)
    freqs = []
    for _ in range(num_slots):
        bucket = tbr.read_bits(5)
        extra_bits = _bucket_extra_bits(bucket)
        extra_value = tbr.read_bits(extra_bits) if extra_bits > 0 else 0
        freqs.append(_bucket_decode(bucket, extra_value))

    cum = [0] * (num_slots + 1)
    for sym in range(num_slots):
        cum[sym + 1] = cum[sym] + freqs[sym]
    assert cum[num_slots] == tot_freq

    dec = RangeDecoder(data)
    out = []
    for _ in range(n_items):
        f = dec.get_freq(tot_freq)
        lo, hi = 0, num_slots - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if cum[mid + 1] <= f:
                lo = mid + 1
            else:
                hi = mid
        sym = lo
        dec.decode(cum[sym], cum[sym + 1] - cum[sym], tot_freq)
        out.append(sym)
    return out
