"""
LZ77-style tokenizer: unbounded-history greedy longest-match, producing a
SMALL, FIXED alphabet (unlike LZW where alphabet size == dictionary size).

Tokens: ('lit', value) or ('match', length, distance).
length in [MIN_MATCH, MAX_MATCH], distance in [1, position].

Length/distance are each split into (bucket, extra_bits) the way DEFLATE
does: bucket is entropy-coded (skewed - short lengths/distances are far more
common), extra bits are stored raw (roughly uniform within a bucket, so
entropy coding them buys little and costs table complexity).
"""

MIN_MATCH = 3
MAX_MATCH = 258
CHAIN_LIMIT = 128  # max candidate positions checked per lookup, for speed


def _bucket_encode(v: int):
    """v >= 0. Returns (bucket, extra_bits_count, extra_value).
    DEFLATE-style scheme: 2 buckets per bit-length above the first few small
    values, so buckets stay few (skewed, good for Huffman) while extra bits
    stay raw (roughly uniform, not worth entropy-coding)."""
    if v < 4:
        return v, 0, 0
    nbits = v.bit_length()
    extra_bits = nbits - 2
    base_for_nbits = 1 << (nbits - 1)
    half = 1 << (nbits - 2)
    sub = 0 if (v - base_for_nbits) < half else 1
    bucket = 4 + 2 * (nbits - 2) + sub
    extra_value = (v - base_for_nbits) - sub * half
    return bucket, extra_bits, extra_value


def _bucket_decode(bucket: int, extra_value: int) -> int:
    if bucket < 4:
        return bucket
    nbits = (bucket - 4) // 2 + 2
    sub = (bucket - 4) % 2
    extra_bits = nbits - 2
    base_for_nbits = (1 << (nbits - 1))
    half = 1 << (nbits - 2)
    return base_for_nbits + sub * half + extra_value


def _bucket_extra_bits(bucket: int) -> int:
    if bucket < 4:
        return 0
    nbits = (bucket - 4) // 2 + 2
    return nbits - 2


def lz77_tokenize(values, chain_limit=CHAIN_LIMIT, nice_length=MAX_MATCH, lazy=True):
    n = len(values)
    tokens = []
    table = {}  # 3-gram tuple -> list of positions, most recent last

    def register(pos):
        if pos + MIN_MATCH <= n:
            k = (values[pos], values[pos + 1], values[pos + 2])
            lst = table.setdefault(k, [])
            lst.append(pos)
            if len(lst) > 128:
                lst.pop(0)

    def find_match(pos):
        if pos + MIN_MATCH > n:
            return 0, 0
        key = (values[pos], values[pos + 1], values[pos + 2])
        chain = table.get(key)
        if not chain:
            return 0, 0
        best_len = 0
        best_dist = 0
        checked = 0
        max_len_possible = min(MAX_MATCH, n - pos)
        for cpos in reversed(chain):
            if checked >= chain_limit:
                break
            checked += 1
            dist = pos - cpos
            l = 0
            while l < max_len_possible and values[cpos + l] == values[pos + l]:
                l += 1
            if l > best_len:
                best_len = l
                best_dist = dist
                if l >= nice_length:
                    break
        return best_len, best_dist

    i = 0
    while i < n:
        cur_len, cur_dist = find_match(i)
        register(i)

        if lazy and cur_len >= MIN_MATCH and i + 1 < n:
            next_len, next_dist = find_match(i + 1)
            if next_len > cur_len:
                # defer: emit literal at i, let the better match happen at i+1
                tokens.append(('lit', values[i]))
                i += 1
                continue

        if cur_len >= MIN_MATCH:
            tokens.append(('match', cur_len, cur_dist))
            end = i + cur_len
            step = 1 if cur_len <= 32 else max(1, cur_len // 32)
            j = i + 1
            while j < end:
                register(j)
                j += step
            i = end
        else:
            tokens.append(('lit', values[i]))
            i += 1
    return tokens


def lz77_detokenize(tokens):
    out = []
    for t in tokens:
        if t[0] == 'lit':
            out.append(t[1])
        else:
            _, length, dist = t
            start = len(out) - dist
            for k in range(length):
                out.append(out[start + k])
    return out
