"""packcache.py — never compress the same bytes twice.

WHY

Traced on a real 1.2 MB CSV: 1,336 pack calls, and 45% of the total
runtime went to compressing buffers that had ALREADY been compressed
with the same codec at the same level. The full input alone was packed
six times, costing 2.02 seconds of a 3.93-second run.

That happens because candidates are built independently and each asks
"how big would this be?", and several of them ask about the same buffer -
the raw input, the columnar encoding, a transform that two paths both
produce. None of them knows the others exist, which is the right design
for correctness and a wasteful one for time.

The result of compressing a given buffer with a given codec is a pure
function of those two things. So it is memoised.

WHAT THIS IS NOT

It is not a shortcut, an approximation, or a tradeoff. Every caller gets
exactly the bytes it would have got, because they ARE the bytes it would
have got. The output of the compressor cannot change.

MEMORY

Compressed results are kept, not inputs, so entries are small - the
things worth caching are the ones that compressed well. The cache is
bounded and cleared between files; holding results from every file ever
seen would be a leak rather than a cache.
"""

import hashlib
import lzma
import bz2
import zlib

# Buffers below this are cheap enough that hashing them costs more than
# recompressing them. Measured: the 16 KB screening slices were packed
# twenty times each and still only cost 0.04 s in total.
MIN_SIZE = 65536

# Total compressed bytes held. Entries are compressed results, so this is
# far more files than the number suggests.
MAX_BYTES = 64 * 1024 * 1024

_cache = {}
_held = 0
_hits = 0
_misses = 0


def _key(data, codec, level):
    # sha1 over the whole buffer. Hashing 1 MB costs about a millisecond
    # where compressing it costs several hundred, so the trade is not
    # close. A prefix hash would be faster and would collide between two
    # transforms of the same file that share a header - which is exactly
    # the case this cache sees most.
    return (hashlib.sha1(data).digest(), codec, level)


def clear():
    global _held, _hits, _misses
    _cache.clear()
    _held = 0
    _hits = 0
    _misses = 0


def stats():
    return {"entries": len(_cache), "bytes_held": _held,
            "hits": _hits, "misses": _misses}


def _store(k, out):
    global _held
    if _held + len(out) > MAX_BYTES:
        # Nothing clever - the working set for one file fits, and an LRU
        # here would cost more in bookkeeping than it saves.
        _cache.clear()
        _held = 0
    _cache[k] = out
    _held += len(out)


def compress(data, codec="lzma", level=6):
    """Compress, or return the identical bytes from a previous call."""
    global _hits, _misses
    if len(data) < MIN_SIZE:
        return _raw(data, codec, level)
    k = _key(data, codec, level)
    hit = _cache.get(k)
    if hit is not None:
        _hits += 1
        return hit
    _misses += 1
    out = _raw(data, codec, level)
    _store(k, out)
    return out


def _raw(data, codec, level):
    if codec == "lzma":
        return lzma.compress(data, preset=level)
    if codec == "lzma_e":
        return lzma.compress(data, preset=level | lzma.PRESET_EXTREME)
    if codec == "bz2":
        return bz2.compress(data, level)
    if codec == "zlib":
        return zlib.compress(data, level)
    raise ValueError(codec)


def best(data, codecs=(("lzma", 6), ("bz2", 9), ("zlib", 9))):
    """Smallest of several codecs, each memoised independently."""
    out = None
    for codec, level in codecs:
        c = compress(data, codec, level)
        if out is None or len(c) < len(out):
            out = c
    return out
