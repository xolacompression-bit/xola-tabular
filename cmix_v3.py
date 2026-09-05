"""
cmix_v2.py — context mixing with a match model
==============================================

WHAT WAS MISSING IN v1, MEASURED

v1 lost 152% to gzip on repetitive text. Diagnosis: 98% of that file was
covered by repeated strings of 8+ bytes, the longest running 199 bytes.
v1's deepest context was order-3, so it had to re-predict every byte of
every repeat from three bytes of history. LZ77 encodes each repeat as one
short reference.

WHAT IS NEW

1. MATCH MODEL — the fix for the above.
   Remembers where the current 6-byte context last occurred and predicts the
   byte that followed it, with confidence rising as the match runs. This is
   how a context mixer competes with LZ77 without becoming one: instead of
   emitting a (length, distance) pair, it makes the predicted bit nearly
   free to code.

2. ORDERS 4 AND 6 — deeper contexts for structured text and records.

3. SSE / APM — a final calibration stage. The mixer's output is refined
   against a table indexed by (quantised probability, match state), which
   corrects systematic over- and under-confidence. This is the same idea as
   the adaptive calibrator we measured earlier, applied per-bit.

4. ADAPTIVE LEARNING RATE — the mixer starts fast and slows as it settles,
   so early bytes adapt quickly without the weights thrashing later.

SYMMETRY
Encoder and decoder share one model class and call it in the same order, so
the decoder reconstructs every state transition exactly. Nothing is
transmitted except the arithmetic-coded bits.
"""

import math

# ----------------------------------------------------------------------
# tables
# ----------------------------------------------------------------------

_STRETCH = [0.0] * 4096
for _p in range(1, 4095):
    _STRETCH[_p] = math.log(_p / (4096.0 - _p))
_STRETCH[0] = _STRETCH[1]
_STRETCH[4095] = _STRETCH[4094]


def _squash(x):
    if x > 20.0:
        return 0.9999
    if x < -20.0:
        return 0.0001
    return 1.0 / (1.0 + math.exp(-x))


TBITS = 22
TSIZE = 1 << TBITS
TMASK = TSIZE - 1

MATCH_MIN = 6            # bytes of context before a match is trusted
MATCH_BITS = 20
MATCH_SIZE = 1 << MATCH_BITS
MATCH_MASK = MATCH_SIZE - 1

N_MODELS = 7             # o0 o1 o2 o3 o4 o6 match


class Model:
    """All adaptive state. One instance per direction; encoder and decoder
    drive it identically, which is what makes the scheme lossless."""

    def __init__(self):
        self.t = [[2048] * TSIZE for _ in range(6)]
        # per-slot observation count, capped. A slot seen twice should move
        # far on the next bit; one seen 500 times should barely budge.
        self.cnt = [bytearray(TSIZE) for _ in range(6)]
        self.rates = (4, 4, 4, 5, 5, 6)
        self.count_adaptive = True

        # mixer: one weight set per match state, so a run of 40 matched
        # bytes and a cold start do not share a trust profile
        self.n_mixers = 8
        self.W = [[0.25] * N_MODELS for _ in range(self.n_mixers)]
        self.mixer_sel = 0
        self.n_updates = 0
        # OFF by default. Ablation: match-selected mixers cost 7-8% on text
        # and json and gained nothing on structured data. Count adaptation
        # carries the entire improvement on its own - the two are NOT a
        # coupled pair on this engine, whatever an isolated rig suggests.
        self.match_mixers = False

        # SSE: 33 probability buckets x 8 match states
        self.apm = [[(i * 4096) // 32 for i in range(33)] for _ in range(8)]

        # history
        self.hist = bytearray()
        self.h = [0] * 6

        # match model
        self.mtab = [0] * MATCH_SIZE
        self.match_ptr = 0
        self.match_len = 0
        self.pred_byte = -1

    # ---------------- context construction ----------------
    def byte_start(self):
        """Called once per byte, before its 8 bits. Recomputes the context
        hashes and looks up the match model."""
        h = self.hist
        n = len(h)
        b1 = h[n - 1] if n >= 1 else 0
        b2 = h[n - 2] if n >= 2 else 0
        b3 = h[n - 3] if n >= 3 else 0
        b4 = h[n - 4] if n >= 4 else 0
        b6 = h[n - 6] if n >= 6 else 0

        self.h[0] = 0
        self.h[1] = (b1 * 0x1000193) & TMASK
        self.h[2] = ((b1 << 8 | b2) * 0x9E3779B1) & TMASK
        self.h[3] = ((b1 << 16 | b2 << 8 | b3) * 0x85EBCA77) & TMASK
        self.h[4] = ((b1 << 24 | b2 << 16 | b3 << 8 | b4) * 0xC2B2AE3D) & TMASK
        self.h[5] = ((b1 << 24 | b2 << 16 | b3 << 8 | b6) * 0x27D4EB2F) & TMASK

        # --- match model lookup ---
        if n >= MATCH_MIN:
            key = 0
            for k in range(MATCH_MIN):
                key = (key * 0x9E3779B1 + h[n - 1 - k]) & 0xFFFFFFFF
            key &= MATCH_MASK
            cand = self.mtab[key]
            if self.match_len and self.match_ptr < n and \
                    h[self.match_ptr] == h[n - 1]:
                # the running match continued
                self.match_ptr += 1
                self.match_len = min(self.match_len + 1, 65535)
            elif cand and cand < n:
                self.match_ptr = cand
                self.match_len = 1
            else:
                self.match_len = 0
            self.mtab[key] = n

        self.pred_byte = (h[self.match_ptr]
                          if self.match_len and self.match_ptr < n else -1)

    def match_state(self):
        m = self.match_len
        if not m or self.pred_byte < 0:
            return 0
        if m < 2:
            return 1
        if m < 4:
            return 2
        if m < 8:
            return 3
        if m < 16:
            return 4
        if m < 32:
            return 5
        if m < 64:
            return 6
        return 7

    # ---------------- prediction ----------------
    def predict(self, partial, bitpos):
        cx = []
        for i in range(6):
            cx.append((self.h[i] + partial * 0x6F4F2B1D) & TMASK)

        st = []
        probs = []
        for i in range(6):
            p = self.t[i][cx[i]]
            probs.append(p)
            st.append(_STRETCH[p])

        # match model: does the predicted byte agree with the bits so far?
        ms = self.match_state()
        if ms:
            pb = self.pred_byte
            # bits of pb already emitted must equal partial's low bits
            emitted = partial & ((1 << bitpos) - 1) if bitpos else 0
            shift = 8 - bitpos
            if (pb >> shift) == emitted:
                expect = (pb >> (shift - 1)) & 1
                conf = min(0.5 + 0.06 * self.match_len, 0.97)
                mp = conf if expect else (1.0 - conf)
            else:
                mp = 0.5
                ms = 0
        else:
            mp = 0.5
        st.append(math.log(mp / (1.0 - mp)))
        probs.append(int(mp * 4096))

        self.mixer_sel = ms if self.match_mixers else 0
        w = self.W[self.mixer_sel]
        x = 0.0
        for i in range(N_MODELS):
            x += w[i] * st[i]
        pm = _squash(x)

        # ---- SSE / APM refinement ----
        idx = pm * 32.0
        lo = int(idx)
        if lo > 31:
            lo = 31
        frac = idx - lo
        row = self.apm[ms]
        pa = (row[lo] * (1.0 - frac) + row[lo + 1] * frac) / 4096.0
        # blend raw and calibrated: full trust in SSE early on is unstable
        pf = 0.25 * pm + 0.75 * pa
        if pf < 0.0002:
            pf = 0.0002
        elif pf > 0.9998:
            pf = 0.9998

        return pf, (cx, probs, st, pm, ms, lo, frac)

    # ---------------- update ----------------
    def update(self, state, bit):
        cx, probs, st, pm, ms, lo, frac = state

        # mixer: learning rate decays so early bytes adapt fast and later
        # ones stop thrashing
        self.n_updates += 1
        lr = 0.03 if self.n_updates < 4000 else 0.012
        err = (bit - pm) * lr
        w = self.W[self.mixer_sel]
        for i in range(N_MODELS):
            w[i] += err * st[i]

        tgt = 4095 if bit else 0
        for i in range(6):
            p = probs[i]
            ix = cx[i]
            if self.count_adaptive:
                c = self.cnt[i][ix]
                # shift starts at 1 (huge steps) and grows toward the
                # model's settled rate as evidence accumulates
                sh = 1 + (c >> 1)
                if sh > self.rates[i]:
                    sh = self.rates[i]
                if c < 255:
                    self.cnt[i][ix] = c + 1
            else:
                sh = self.rates[i]
            self.t[i][ix] = p + ((tgt - p) >> sh)

        # SSE table
        row = self.apm[ms]
        g = 4095 if bit else 0
        row[lo] += int((g - row[lo]) * (1.0 - frac) * 0.04)
        row[lo + 1] += int((g - row[lo + 1]) * frac * 0.04)

    def push(self, byte):
        self.hist.append(byte)


# ----------------------------------------------------------------------
# arithmetic coder
# ----------------------------------------------------------------------

def compress(data: bytes) -> bytes:
    m = Model()
    low, high = 0, 0xFFFFFFFF
    out = bytearray()
    for byte in data:
        m.byte_start()
        partial = 1                      # sentinel: keeps prefixes distinct
        for i in range(7, -1, -1):
            bit = (byte >> i) & 1
            pf, state = m.predict(partial, 7 - i)
            p12 = int(pf * 4096)
            if p12 < 1:
                p12 = 1
            elif p12 > 4095:
                p12 = 4095
            mid = low + ((high - low) >> 12) * p12
            if bit:
                high = mid
            else:
                low = mid + 1
            while ((low ^ high) & 0xFF000000) == 0:
                out.append((high >> 24) & 0xFF)
                low = (low << 8) & 0xFFFFFFFF
                high = ((high << 8) | 0xFF) & 0xFFFFFFFF
            m.update(state, bit)
            partial = (partial << 1) | bit
        m.push(byte)
    for _ in range(4):
        out.append((high >> 24) & 0xFF)
        high = (high << 8) & 0xFFFFFFFF
    return len(data).to_bytes(4, 'big') + bytes(out)


def decompress(blob: bytes) -> bytes:
    n = int.from_bytes(blob[:4], 'big')
    body = blob[4:]
    m = Model()
    low, high = 0, 0xFFFFFFFF
    x = int.from_bytes(body[:4].ljust(4, b'\0'), 'big')
    pos = 4
    out = bytearray()
    for _ in range(n):
        m.byte_start()
        partial = 1                      # sentinel: must match the encoder
        for i in range(8):
            pf, state = m.predict(partial, i)
            p12 = int(pf * 4096)
            if p12 < 1:
                p12 = 1
            elif p12 > 4095:
                p12 = 4095
            mid = low + ((high - low) >> 12) * p12
            bit = 1 if x <= mid else 0
            if bit:
                high = mid
            else:
                low = mid + 1
            while ((low ^ high) & 0xFF000000) == 0:
                low = (low << 8) & 0xFFFFFFFF
                high = ((high << 8) | 0xFF) & 0xFFFFFFFF
                nxt = body[pos] if pos < len(body) else 0
                pos += 1
                x = ((x << 8) | nxt) & 0xFFFFFFFF
            m.update(state, bit)
            partial = (partial << 1) | bit
        byte = partial & 0xFF
        out.append(byte)
        m.push(byte)
    return bytes(out)
