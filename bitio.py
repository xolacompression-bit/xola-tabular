"""Minimal MSB-first bit writer/reader."""

class BitWriter:
    def __init__(self):
        self.bytes = bytearray()
        self.cur = 0
        self.nbits = 0

    def write_bits(self, value: int, nbits: int):
        if nbits == 0:
            return
        for i in range(nbits - 1, -1, -1):
            bit = (value >> i) & 1
            self.cur = (self.cur << 1) | bit
            self.nbits += 1
            if self.nbits == 8:
                self.bytes.append(self.cur)
                self.cur = 0
                self.nbits = 0

    def getvalue(self) -> bytes:
        if self.nbits > 0:
            pad = 8 - self.nbits
            out = bytearray(self.bytes)
            out.append(self.cur << pad)
            return bytes(out)
        return bytes(self.bytes)

    def bitlength(self) -> int:
        return len(self.bytes) * 8 + self.nbits


class BitReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0  # bit position

    def read_bits(self, nbits: int) -> int:
        val = 0
        for _ in range(nbits):
            byte_idx = self.pos // 8
            bit_idx = 7 - (self.pos % 8)
            bit = (self.data[byte_idx] >> bit_idx) & 1
            val = (val << 1) | bit
            self.pos += 1
        return val

    def bits_remaining(self) -> int:
        return len(self.data) * 8 - self.pos
