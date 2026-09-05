"""
prefilter_v3.py — refined structural prefilter
==============================================

WHAT CHANGED FROM v2, AND WHY

Every addition below was kept only because it won on some dataset in
measurement. Ideas that sounded good and lost are recorded at the bottom so
they do not get re-tried.

  + plane_xor        transpose to byte planes, then XOR along each plane.
                     Float bit patterns are not linear, so subtraction is the
                     wrong operator on them; XOR preserves exponent structure.
                     Won on mixed-width records (1,632 vs 1,724 for t+delta).

  + plane_delta2     delta with lag 2 inside each plane. Catches interleaved
                     A/B channel patterns that lag-1 misses.

  + zigzag           map signed residuals so small negatives become small
                     positives (-1 -> 1, 1 -> 2). Costs nothing, helps any
                     delta whose residuals straddle zero.

  + endian pairs     16- and 32-bit deltas in BOTH byte orders. v2 assumed
                     little-endian; SEG-Y and most network formats are big.

  + header split     when a fixed header is detected inside each record, the
                     header and payload streams are transformed separately.
                     On SEG-Y this took 72,000 B of trace headers to 172 B.

WHAT DID NOT WORK (measured, do not retry)

  - float-value delta      subtracting floats then re-encoding: -2.7%. The
                           bit pattern of a small difference is not small.
  - xor at record lag      xor without transposing first: +0.1%, noise.
  - lossy mantissa trunc   +45.7% but not lossless, so out of scope.

THE WALL, STATED PLAINLY

On IEEE float payloads, bytes 0-2 of every value measure entropy 7.86-7.91
out of a maximum 8.00. Those are mantissa bits and they are noise. Only the
sign+exponent byte (entropy ~2.4) carries structure, and every transform here
already exploits it. Roughly 10% is the honest ceiling on float data - the
remaining 90% is not recoverable by any lossless method.
"""

import lzma

# EVERY PACK GOES THROUGH A MEMO.
#
# Traced on a 1.2 MB CSV: 1,336 pack calls, and 45% of the whole runtime
# spent compressing buffers that had already been compressed with the
# same codec at the same level. The input alone was packed SIX times.
#
# Candidates are built independently - which is right, and is why they
# cannot know that another path already asked the same question. So the
# answer is remembered instead.
#
# This changes no output. Compressing a given buffer with a given codec
# is a pure function of the two, and the cache returns the same bytes it
# returned before.
import packcache as _pc
import bz2 as _bz2
import math
import struct
import math as _math
import struct as _struct
import zlib
from collections import Counter

# ----------------------------------------------------------------------
# measurement helpers
# ----------------------------------------------------------------------

def entropy(b):
    if not b:
        return 8.0
    c = Counter(b)
    n = len(b)
    return -sum(v / n * math.log2(v / n) for v in c.values())


# ----------------------------------------------------------------------
# packer selection
# ----------------------------------------------------------------------
#
# Every mode below used to hardcode lzma-6 for the residual. Measured across
# six datasets, zlib beat it on two - 910 vs 948 on transposed sensor data,
# 74 vs 132 on text. A transform changes the shape of what is left, and the
# best coder for that shape is not always the strongest general one.
#
# This is the mirror of the gap found on the engine side: a router that
# only ever pairs its transforms with one packer cannot discover that a
# different packer suits the residual better.

PACK_LZMA, PACK_ZLIB, PACK_BZ2, PACK_CMIX, PACK_RAW, PACK_ZSTD, \
    PACK_BROTLI = 0, 1, 2, 3, 4, 5, 6

# zstd, if the machine has it. Optional on purpose: a stream packed with
# zstd needs zstd to decode, so it is only ever chosen when the library
# is present, and every other packer stays available.
#
# It exists for level 0, where the packer IS the bottleneck. Measured on
# a transformed ship log, one core:
#
#     lzma -6    604,848   +37.1%     14 MB/s
#     zlib -1    765,810   +20.4%    255 MB/s
#
# zstd at level 1 runs around 500 MB/s with a ratio closer to zlib -6
# than zlib -1, which is the best of both ends of that table.
try:
    import csvcol as _csvcol
    HAVE_CSVCOL = True
except Exception:
    HAVE_CSVCOL = False

try:
    import fasta as _fasta
    HAVE_FASTA = True
except Exception:
    HAVE_FASTA = False

try:
    import logcol as _logcol
    HAVE_LOGCOL = True
except Exception:
    HAVE_LOGCOL = False

try:
    import fixedw as _fixedw
    HAVE_FIXEDW = True
except Exception:
    HAVE_FIXEDW = False

# brotli, when the machine has it.
#
# It ships with a 120 KB built-in dictionary of common English words,
# HTML tags and web text. On small text files that dictionary IS most of
# the compression and nothing here can match it: measured on a real batch
# run, brotli reached 1,774 bytes on a 7.7 KB ARFF file where this system
# managed 2,144, and 336 on a 490-byte protein FASTA against 367.
#
# Both were real losses. Adding brotli as a packer turns them into ties
# or wins, for the same reason adding zstd did - the right answer is to
# measure every available codec rather than assume which one wins.
#
# Optional, like zstd: a stream packed with brotli needs brotli to
# decode, so it is only ever chosen when the library is present.
try:
    import brotli as _brotli
    HAVE_BROTLI = True
except Exception:
    HAVE_BROTLI = False

try:
    import zstandard as _zstd
    HAVE_ZSTD = True
    _ZC = _zstd.ZstdCompressor(level=1)
    _ZD = _zstd.ZstdDecompressor()
except Exception:
    HAVE_ZSTD = False

# TESTED AND REJECTED: integer narrowing (int32 -> int16, int64 -> int32).
#
# The obvious sibling of the float32->int transform above, and it looked
# strong in isolation: int32->int16 gained 18.9%, int64->int32 20.9%.
# Integrated into the router it gained +0.0% on every dataset.
#
# The reason is worth keeping, because it explains why the float version
# DOES work. Per-byte-position entropy on the same values:
#
#     int32 holding int16      float32 holding int16
#       byte 0  7.88             byte 0  0.00
#       byte 1  4.41             byte 1  3.75
#       byte 2  1.00             byte 2  7.12
#       byte 3  1.00             byte 3  2.49
#
# In the int32 case bytes 2 and 3 are near-constant, so TRANSPOSE already
# isolates them into a column that costs almost nothing. The waste is
# already reachable.
#
# In the float32 case the value is spread across sign, exponent and
# mantissa, which straddle byte boundaries - no byte position is constant,
# and transpose cannot separate them. Converting to int is the only route
# to that waste, which is why it gained 16.4% on real SAC data.
#
# The lesson: a transform only pays when it reaches redundancy the
# existing ones cannot. "Obviously similar" is not a reason.


# TESTED AND REJECTED: lzma's built-in FILTER_DELTA.
#
# It is a mature, well-tuned delta filter, and on the FLAT transform's
# residual for interleaved int16 pairs it beat cmix - 1,224 bytes against
# 1,313. That looked like a free win.
#
# Integrated into the router it gained +0.0% on every dataset, because the
# router reaches 996 bytes on that same file through a different mode. The
# isolated comparison was against a weaker baseline than what actually
# ships.
#
# Also worth recording: measured head to head, this router beat plain
# FILTER_DELTA on all five test files, sometimes by half. The idea that a
# hand-rolled delta cannot beat lzma's is true for a hand-rolled delta and
# not true for a transform search.

import concurrent.futures as _cf
import multiprocessing as _mp
import os as _os

# ----------------------------------------------------------------------
# NumPy acceleration
# ----------------------------------------------------------------------
#
# Four elementwise transforms dominated the profile: 5.1 s of a 7 KB
# compression sat inside _plane_op, _wdelta, cross_forward and
# cross2_forward. All four are byte arithmetic over a contiguous buffer -
# exactly what NumPy does in compiled code while Python boxes every byte.
#
# Measured: delta 64-175x, wdelta 7-29x, cross 13-38x.
#
# The replacements are only used after verify() has proved them
# byte-identical to the originals on 200 random inputs. "Close enough"
# would be worse than useless here: the router would silently pick
# different candidates, and the two implementations would diverge in ways
# that are very hard to trace. If NumPy is absent or verification fails,
# the pure-Python path is used unchanged.

try:
    import fastops as _fast
    HAVE_FAST = True
except Exception:
    HAVE_FAST = False
import time as _time

# lzma, zlib and bz2 all release the GIL while compressing, so plain
# threads give real parallelism here - no pickling, no Windows spawn
# problems, no separate processes to manage.
#
# On a single-core machine this is 1 and everything runs sequentially,
# which is why the gain could not be measured during development. On the
# 6-core server it should let the three or four packers of each candidate
# run at once.
WORKERS = max(1, min(12, (_os.cpu_count() or 1) * 2))

# ----------------------------------------------------------------------
# cmix time budget
# ----------------------------------------------------------------------
#
# cmix is the best packer on nearly every residual - it won 5 of 5 in
# measurement, by 2% to 35%. It also runs at about 18 KB/s, and the router
# calls pack_best once per candidate MODE. Profiling a 50 KB file: 26 cmix
# calls, 58 of 80 total seconds, when only the winning mode's output is ever
# kept.
#
# Re-packing only the winner would be ideal but means rebuilding finished
# blobs, which every mode formats differently. A budget gets the same result
# without that fragility: early candidates get cmix, and once the budget is
# spent the rest fall back to the fast packers.
#
# This can cost a little ratio if a late mode would have won with cmix and
# not without it. The router still keeps the smallest of everything it
# built, so the result is never wrong - only occasionally not quite optimal.
# Raise CMIX_BUDGET for maximum ratio, lower it for speed.

# BYTES, NOT SECONDS.
#
# This was a wall-clock budget: cmix could run for 6 seconds per
# compress() call. That made the OUTPUT depend on how busy the machine
# was. Measured: three worker processes sharing one CPU each got a third
# of the machine, so within their 6 seconds they finished a third of the
# work and produced 29,269 bytes where a single process produced 28,530.
#
# Same file, same version, different result - because something else was
# running. That is not acceptable in a compressor, and it also made
# parallel work impossible to reason about.
#
# Counting BYTES FED TO CMIX instead makes the result identical on any
# machine, at any load, with any number of workers. 3 MB is roughly what
# 6 seconds bought on the development machine, so the default behaviour
# is close to unchanged.
CMIX_BUDGET = 3_000_000      # bytes of input cmix may process per call
_cmix_spent = 0

# Smallest payload any candidate has produced so far in this compress()
# call. The budget alone is first-come-first-served: whichever mode happens
# to run first eats the seconds, even when it is going to lose by a wide
# margin, and a later mode that would have won never gets cmix.
#
# Measured on an accelerating counter: delta2 + transpose + cmix reaches
# 199 bytes, but the router shipped 216 because the budget was gone before
# chain mode ran. Spending cmix only on candidates that are already
# competitive fixes the ordering without raising the budget.
_best_payload = None
CMIX_MARGIN = 1.5

# Level 1 detects the transform on a sample rather than the whole file.
# 16 KB was byte-identical to the full search on six real files; two
# probes are compared so a file whose structure changes partway through
# falls back to the real search.
# lzma preset for the fast path. There is a cliff between 4 and 3, not a
# slope - measured on a transformed ship log:
#
#     preset 6   604,848   +37.1%   12 MB/s
#     preset 5   606,160   +37.0%   15 MB/s
#     preset 4   607,024   +36.9%   17 MB/s   <- the last one that holds
#     preset 3   706,912   +26.5%   30 MB/s   <- ratio falls off here
#
# 4 keeps the full advantage and is 1.4x faster than 6. Level 3 still
# uses the strongest settings; this is only the fast path.
LZMA_FAST = 4

# Above this, the extreme preset is not attempted - it costs roughly
# 0.5 s per megabyte and the transform has usually already won on data
# that large.
LZMA_EXTREME_MAX = 2 * 1024 * 1024

# Extreme mode is skipped when preset 6 has ALREADY compressed better
# than 1-in-this, because that is where it becomes absurdly expensive.
#
# The cost of extreme depends on how compressible the buffer is, not on
# its size. Measured on raw CSV it is a reasonable 1.5-2.2x slower than
# preset 6 and the gain grows with the buffer - +3.43% at 2 MB.
#
# But on a TRANSFORMED buffer, where 2 MB compresses to 29 KB, preset 6
# races through and extreme still searches exhaustively: 0.052 s against
# 1.584 s, THIRTY TIMES slower, for 2.88% of an already-tiny output -
# about 840 bytes for a second and a half.
#
# A first attempt gated on the absolute output size at 32 KB. It did not
# fire, because that file's preset-6 output sat just above the line. The
# ratio is the right variable: it is scale-free and it is what actually
# predicts the cost.
EXTREME_MIN_RATIO = 20

# Buffers below this always get extreme mode, whatever the ratio.
#
# The expense of extreme scales with how much searching there is to do,
# and on a small buffer there is little. Measured: 0.063 s on 64 KB
# against preset 6's 0.028 s - a fraction of a second, for gains up to
# +6.8% on log text.
#
# The first version of this gate had no such floor and dropped a real
# syslog from +16.7% to +9.2%, under its quality floor. The regression
# test failed the build, which is the entire reason that test exists.
EXTREME_ALWAYS_BELOW = 512 * 1024

# How far behind plain lzma may be and still justify trying extreme.
#
# Extreme is an lzma variant. When bzip2 or zlib is already ahead by
# more than this, extreme does not close the gap - measured across 79
# real files, skipping it in that case loses NOTHING at 5% and loses
# 12.63% on a syslog at 0%.
EXTREME_RIVAL_MARGIN = 0.05

# 1 MB, measured - not the 4 MB I guessed when brotli was added.
#
# brotli's advantage comes from its 120 KB built-in dictionary of common
# English and web text. That dictionary is most of the file when the file
# is small, and irrelevant when it is not.
#
# Measured on a 3.5 MB chunk of real EPA air-quality data:
#
#     brotli q11   58,279 bytes   3.4 s
#     lzma  -6     70,752 bytes   0.1 s
#     bzip2 -9     53,920 bytes
#
# brotli spent 3.4 seconds to LOSE to bzip2. And because the cap was
# 4 MB, this fired on every chunk once the worker count reached 6 -
# which is exactly why the bench showed 6 workers running SLOWER than 4:
#
#     4 workers   5.24 MB chunks   above the cap, brotli skipped   6.2 s
#     6 workers   3.50 MB chunks   below the cap, brotli ran       8.1 s
#
# More cores, more time. The anomaly was this constant.
#
# 1 MB keeps brotli where it demonstrably wins - it beat everything on a
# 7.7 KB ARFF file and a 490-byte protein sequence - without paying for
# it on data where the dictionary cannot help. The exact crossover
# between 10 KB and 3.5 MB has not been measured.
BROTLI_MAX = 1024 * 1024

# Columnar CSV runs at about 1.12 MB/s, so a 64 MB file costs roughly a
# minute single-threaded. Beyond that, chunking through bigfile.py is the
# right approach and it parallelises.
CSVCOL_MAX = 64 * 1024 * 1024

# ONE LIMIT FOR EVERY FORMAT MODULE.
#
# Four modules each carried their own hardcoded 1 << 24 - sixteen
# megabytes - while CSVCOL_MAX sat at sixty-four and was not consulted by
# any of them. A real 20 MB file therefore had csvcol, fixedw and logcol
# all silently disabled, and returned 322,374 bytes where chunking the
# same file into pieces under the limit returned 86,510.
#
# Scattered constants that mean the same thing will drift, and the
# smallest one wins without saying so. There is now one.
MODULE_MAX = 64 * 1024 * 1024

PROBE_BYTES = 16384
PROBE_TRIGGER = 256 * 1024

# How many segments may each claim their own cmix allowance. Beyond this
# the allowance is halved, so a heavily segmented file cannot multiply the
# budget without bound.
SEG_CMIX_CAP = 8

# Processes for the segment loop, and the size below which it is not
# worth starting them. A worker costs about 250 ms to spawn on Windows
# (it re-imports numpy and this module), so a small file is faster done
# in one piece.
SEG_PROCS = max(1, min(8, (_os.cpu_count() or 1)))
SEG_PAR_MIN = 256 * 1024        # skip cmix on anything this much worse than the best


def _reset_cmix_budget():
    global _cmix_spent, _best_payload, _best_payload
    _cmix_spent = 0
    _best_payload = None

# cmix runs at roughly 20 KB/s and is SYMMETRIC - decoding costs the same as
# encoding. Above this it is not worth the wall clock.
CMIX_LIMIT = 64 * 1024

try:
    import cmix_v3 as _cmix
    HAVE_CMIX = True
except ImportError:
    HAVE_CMIX = False


def _cmix_tbits(n):
    """Context table size for an input of n bytes.

    cmix_v3 allocates six tables of 2^TBITS entries at TBITS=22 - about 24
    million slots, ~200 MB - before compressing anything. Profiling showed
    95 Model() constructions costing 10.2 s on a single 18 KB file, purely
    in allocation, for a file that can never fill a table that size.

    Measured on an 8 KB log: TBITS 22 gives 595 bytes in 1.68 s, TBITS 20
    gives 596 bytes in 0.46 s. One byte, 3.6x the speed.

    Sized from the length cmix already records in its own first four bytes,
    so both ends compute the same value with nothing extra stored."""
    if n <= 0:
        return 16
    want = max(16, min(22, n.bit_length() + 7))
    return want


class _cmix_sized:
    """Set cmix's table size for one call, then put it back.

    cmix_v3 keeps TBITS/TSIZE/TMASK as module globals, so this changes a
    tuning parameter without touching the algorithm - and restores it, so
    nothing else that imports cmix_v3 sees a modified module."""

    def __init__(self, n):
        self.tb = _cmix_tbits(n)

    def __enter__(self):
        self.old = (_cmix.TBITS, _cmix.TSIZE, _cmix.TMASK)
        _cmix.TBITS = self.tb
        _cmix.TSIZE = 1 << self.tb
        _cmix.TMASK = _cmix.TSIZE - 1
        return self

    def __exit__(self, *a):
        _cmix.TBITS, _cmix.TSIZE, _cmix.TMASK = self.old
        return False


def _lzma_still_competitive(x, a):
    """Is plain lzma close enough to the others to justify extreme?

    Extreme is an lzma variant. When bzip2 or zlib is already well ahead,
    extreme has to close that gap AND win, and it does not: traced on a
    real CSV it spent 0.841 s to return 85,500 where bzip2 had already
    given 75,951.

    The margin was measured across 79 files. Requiring lzma to be
    outright best cost a structured syslog 12.63%. At 5% the loss is
    ZERO and 3.2 s of searching is still skipped."""
    try:
        rivals = min(len(_pc.compress(x, "bz2", 9)),
                     len(_pc.compress(x, "zlib", 9)))
    except Exception:
        return True
    return len(a) <= rivals * (1 + EXTREME_RIVAL_MARGIN)


def pack_best(x, allow_cmix=True):
    """Try every packer, return (id, bytes) for the smallest.

    cmix - a context-mixing coder - is included here as of v18, and it is
    the single largest structural change since the transform set was
    finished. Measured on transformed residuals it won 6 of 6 datasets,
    sometimes heavily: text 44 bytes against zlib's 68, sensor 410 against
    493.
    
    Why it was missed for so long: cmix existed the whole time, but only as
    a separate whole-file alternative. A TRANSFORMED residual is a
    different animal from raw input - the transform has already removed the
    positional structure, leaving exactly the sequential correlation a
    context model eats. The two systems were never given the chance to
    compose because the router only ever asked LZ-family tools.

    Gated on size because cmix is ~20 KB/s and symmetric: decode costs the
    same as encode, which matters far more than encode time for archival."""
    global _cmix_spent, _best_payload

    # SCREEN, THEN CONFIRM.
    #
    # pack_best was 65% of total compression time: 27 calls on a 5.6 KB
    # file, each running lzma-6, zlib-9 and bz2-9 over the same bytes, and
    # 26 of the 27 results thrown away.
    #
    # Fast presets pick the same winner 8 times out of 10 - not enough to
    # trust alone, since the two misses were both real (zlib won, level-1
    # said lzma). But the true winner was always in the top TWO by cheap
    # score, so screening at level 1 and confirming the best two at full
    # strength gives the same answer for less work.
    import bz2
    cheap = []
    for pid, fn in ((PACK_LZMA, lambda: _pc.compress(x, 'lzma', 1)),
                    (PACK_ZLIB, lambda: _pc.compress(x, 'zlib', 1)),
                    (PACK_BZ2, lambda: _pc.compress(x, 'bz2', 1))):
        try:
            cheap.append((len(fn()), pid))
        except Exception:
            continue
    cheap.sort()
    finalists = [pid for _, pid in cheap[:2]] or [PACK_LZMA]

    # bz2 IS ALWAYS A FINALIST.
    #
    # The claim above - "the true winner was always in the top two by
    # cheap score" - held for the files it was measured on and failed on
    # prose. Block-sorting behaves differently at low and high effort:
    # bz2 at level 1 looks unremarkable and at level 9 it wins outright.
    #
    # Measured on 400 KB of Moby Dick: bzip2 -9 gives 129,694 where
    # lzma -9 gives 142,140 and xz -9e gives 142,204. The screen put bz2
    # outside the top two, so the file came out 9.6% WORSE than the best
    # standard tool - on ordinary English prose, which is the most common
    # data there is.
    #
    # It costs one extra full compression. That is cheap next to being
    # 10% behind bzip2 on a book.
    if PACK_BZ2 not in finalists:
        finalists.append(PACK_BZ2)

    # lzma at EXTREME, not 6.
    #
    # preset 6 is the sensible default for structured data, where the
    # transform has already done the work. On prose-like text it is far
    # behind: a real Linux syslog gave 7,248 bytes at preset 9 and 6,048
    # at 9|EXTREME - and 6,048 is exactly what xz -9e achieves, the tool
    # we were losing 44% to on that file.
    #
    # Both are tried and the smaller kept. The extra cost is 40-90 ms on
    # a 150 KB buffer and it is skipped entirely above LZMA_EXTREME_MAX,
    # where the transform has usually already won.
    def _lz():
        # THESE TWO PRESETS CANNOT USEFULLY OVERLAP, AND IT WAS TRIED.
        #
        # Running preset 6 and extreme on separate threads looks obvious:
        # same buffer, independent, and lzma releases the GIL. Measured on
        # a 1.2 MB file they were 0.627 s and 0.727 s back to back, 65% of
        # the whole compression.
        #
        # But the gate below needs preset 6's OUTPUT to decide whether
        # extreme is worth running at all. Starting both means starting
        # extreme speculatively - and on a 20 MB file where the gate says
        # no, that is a second of work thrown away. Throughput went from
        # 1.744 to 1.636 MB/s.
        #
        # The gate is worth more than the overlap. Sequential it stays.
        a = _pc.compress(x, 'lzma', 6)

        # THE GATE LOOKS AT WHAT IS AT STAKE, NOT JUST THE BUFFER SIZE.
        #
        # The note above estimated 40-90 ms for extreme mode on a 150 KB
        # buffer. That estimate does not scale: extreme searching is
        # superlinear, and traced on a real 2 MB CSV the single call cost
        # 1.624 s against preset 6's 0.051 s - 32 times the work, and 55%
        # of the entire compression.
        #
        # What it bought there was 2.88%, which on an already-tiny output
        # is 840 bytes. Elsewhere it earned its keep: +6.80% on a syslog.
        # And twice it was WORSE - diamonds -0.13%, prose -0.03% - so it
        # was never a free improvement, only a usually-small one.
        #
        # So the gate now asks how many bytes could plausibly be saved.
        # When preset 6 has already reduced the data enormously, the
        # remaining few percent is worth less than the seconds it costs.
        # Small buffers always get extreme: it is cheap there, and text
        # is where it earns most. Gating them by ratio cost a real syslog
        # 16.7% -> 9.2%, below its floor, and the quality test caught it.
        # EXTREME IS AN LZMA VARIANT, SO ASK WHETHER LZMA IS COMPETITIVE.
        #
        # If bzip2 or zlib is already well ahead of plain lzma on this
        # buffer, extreme has to close that gap AND win, which it does
        # not do. Traced on a real CSV: extreme cost 0.841 s and returned
        # 85,500 where bzip2 had already given 75,951.
        #
        # The margin matters and was measured, not guessed. Skipping
        # extreme whenever lzma was not outright best cost a structured
        # syslog 12.63%. At a 5% margin the loss across 79 files is
        # ZERO and 3.2 s of extreme searching is still skipped.
        # CHEAP GATE FIRST, EXPENSIVE GATE SECOND.
        #
        # The rival check needs bzip2 and zlib run on this buffer, which
        # is not free. Asking it before the size and ratio tests meant
        # paying for two packs on files where extreme was never going to
        # run anyway - throughput went 1.721 -> 1.571 MB/s on a 20 MB CSV
        # that the ratio gate had always rejected.
        #
        # So the free tests decide first, and the rival check only runs
        # when everything else has already said yes.
        _size_ok = (len(x) <= EXTREME_ALWAYS_BELOW or (
            len(x) <= LZMA_EXTREME_MAX
            and len(a) * EXTREME_MIN_RATIO >= len(x)))

        if _size_ok and _lzma_still_competitive(x, a):
            try:
                b = _pc.compress(x, 'lzma_e', 9)
                if len(b) < len(a):
                    return b
            except Exception:
                pass
        return a

    full = {PACK_LZMA: _lz,
            PACK_ZLIB: lambda: _pc.compress(x, 'zlib', 9),
            PACK_BZ2:  lambda: _pc.compress(x, 'bz2', 9)}
    jobs = [(pid, full[pid]) for pid in finalists]

    # brotli and zstd are added OUTSIDE the two-finalist screen.
    #
    # The screen picks the two cheapest-looking codecs and only runs those
    # at full strength. That is fine for the three standard ones, which
    # behave similarly at low and high effort. brotli does not: its
    # built-in dictionary means it can be unremarkable on a fast setting
    # and the outright winner at quality 11 on small text.
    #
    # Measured on a real batch run, brotli beat this system on two files
    # it never got to try - 1,774 against 2,144 on an ARFF file, and 336
    # against 367 on a protein FASTA. Screening it out was the reason.
    if HAVE_BROTLI and len(x) <= BROTLI_MAX:
        jobs.append((PACK_BROTLI,
                     lambda: _brotli.compress(bytes(x), quality=11)))

    # lzma, zlib and bz2 all release the GIL while compressing, so ordinary
    # threads give real parallelism here - no pickling, no Windows spawn
    # problems.
    cands = []
    if WORKERS <= 1 or len(jobs) <= 1:
        for pid, fn in jobs:
            try:
                cands.append((pid, fn()))
            except Exception:
                continue
    else:
        with _cf.ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            futs = [(pid, ex.submit(fn)) for pid, fn in jobs]
            for pid, f in futs:
                try:
                    cands.append((pid, f.result()))
                except Exception:
                    continue

    # How good is this candidate before cmix? If it is already far behind
    # the best seen, cmix will not rescue it and the seconds are better
    # spent elsewhere.
    _fast_best = min((len(b) for _, b in cands), default=None)
    if _fast_best is not None:
        _best_payload = (_fast_best if _best_payload is None
                         else min(_best_payload, _fast_best))
    _competitive = (_fast_best is None or _best_payload is None
                    or _fast_best <= _best_payload * CMIX_MARGIN)

    if (allow_cmix and HAVE_CMIX and len(x) <= CMIX_LIMIT
            and _cmix_spent < CMIX_BUDGET and _competitive):
        try:
            with _cmix_sized(len(x)):
                cands.append((PACK_CMIX, _cmix.compress(x)))
        except Exception:
            pass
        # Charged in bytes of input, so the accounting is the same
        # whether this ran alone or alongside thirty other processes.
        _cmix_spent += len(x)

    # Storing the bytes unchanged is always a candidate.
    #
    # Every codec adds a container: zlib costs 11 bytes even on data it
    # cannot shrink at all. Measured on six incompressible payloads, the
    # best codec was 9-11 bytes LARGER than the input every single time.
    # That was being paid on every mode whose residual is noise - float
    # mantissas, base-k packed indices, the random column of a mixed
    # record - which is exactly the data this system produces.
    cands.append((PACK_RAW, bytes(x)))

    if not cands:
        cands = [(PACK_LZMA, _pc.compress(x, 'lzma', 6))]
    # Sort by (size, id) so the winner is deterministic regardless of the
    # order threads finish in.
    return min(cands, key=lambda c: (len(c[1]), c[0]))


def unpack_with(pid, blob, strict=False):
    """Decode a packed payload.

    strict=True means this payload runs to the END of the stream, so the
    packer is asked whether it consumed everything. zlib, lzma and bz2 all
    expose `unused_data`, which makes the stream self-delimiting at zero
    storage cost - a length field in the header would have cost 2-3 bytes
    on every file, and did, until this replaced it."""
    if strict and pid in (PACK_LZMA, PACK_ZLIB, PACK_BZ2):
        try:
            if pid == PACK_LZMA:
                dec = lzma.LZMADecompressor()
            elif pid == PACK_ZLIB:
                dec = zlib.decompressobj()
            else:
                import bz2 as _bz
                dec = _bz.BZ2Decompressor()
            out = dec.decompress(bytes(blob))
            if dec.unused_data:
                raise ValueError(
                    f"{len(dec.unused_data)} unexpected bytes after the "
                    f"stream - extra data follows, or it is truncated")
            return out
        except ValueError:
            raise
        except Exception:
            pass                # malformed - fall through and let it raise

    if pid == PACK_LZMA:
        return lzma.decompress(blob)
    if pid == PACK_ZLIB:
        return zlib.decompress(blob)
    if pid == PACK_ZSTD:
        if not HAVE_ZSTD:
            raise ValueError("stream needs zstandard, which is not installed")
        return _ZD.decompress(blob)
    if pid == PACK_BROTLI:
        if not HAVE_BROTLI:
            raise ValueError("stream needs brotli, which is not installed")
        return _brotli.decompress(bytes(blob))
    if pid == PACK_RAW:
        return bytes(blob)
    if pid == PACK_CMIX:
        # cmix stores the original length in its own first four bytes, so
        # the decoder derives the SAME table size the encoder used without
        # anything extra being threaded through or stored. Passing a length
        # down through eleven call sites is how the two ends end up
        # disagreeing, and disagreement here produces silent garbage.
        n_in = int.from_bytes(blob[:4], "big")
        # cmix loops n_in times, one arithmetic-coded byte per iteration,
        # and takes that count on trust from its own first four bytes. A
        # corrupted value therefore asks it to decode millions of bytes:
        # measured, one flipped bit in a 105-byte blob ran for over twenty
        # seconds. The outer length bounds could not catch it because the
        # corruption is inside the packer's own header.
        #
        # cmix output is never smaller than about 1/1000 of its input on
        # real data, so anything claiming more than that is corrupt.
        if n_in > max(1 << 20, len(blob) * 20000):
            raise ValueError(
                f"cmix stream claims {n_in} bytes from a {len(blob)}-byte "
                f"payload - corrupt")
        with _cmix_sized(n_in):
            return _cmix.decompress(blob)
    import bz2
    return bz2.decompress(blob)


# ----------------------------------------------------------------------
# transform result cache
# ----------------------------------------------------------------------
#
# Several modes ask for the same transform of the same bytes at the same
# period, because each mode builds its own candidate list without knowing
# what the others already computed. Measured on a 5.6 KB sensor file:
# 47% of _wdelta calls and 47% of t_delta calls were exact duplicates.
#
# Keyed on a hash of the CONTENT, not on id(): a freed bytes object can be
# reallocated at the same address, and an id-keyed cache would then return
# another buffer's result. Hashing 5 KB costs about 10 us against 1-2 ms for
# the transform itself, so the trade is heavily favourable.

import hashlib as _hl

_TCACHE = {}
_TCACHE_MAX = 400


def _tkey(d, *params):
    return (_hl.blake2b(d, digest_size=16).digest(), params)


def _cached_transform(fn, d, *params):
    if len(d) < 256 or len(d) > 1 << 20:
        return fn(d, *params)          # too small to pay off, or too big to hold
    k = (fn.__name__,) + _tkey(d, *params)
    hit = _TCACHE.get(k)
    if hit is not None:
        return hit
    r = fn(d, *params)
    if len(_TCACHE) >= _TCACHE_MAX:
        _TCACHE.clear()                # simple bound; these are pure results
    _TCACHE[k] = r
    return r


# 128 KB, not 64.
#
# The cap was set to 64 KB to save time on screening, and it did - about
# 6%. It also cost 19 bytes on a real sensor file, by changing which
# second transform chain mode ranked highest: chain lost to per-column and
# the output went from 10,007 to 10,026.
#
# Measured at 128 KB: 10,007 restored, and the time cost is 1.02x on that
# file, 0.95x on another. The 6% was never real.
#
# The lesson is the one this project keeps relearning - a cheap ranker can
# pick a different winner than the expensive system would, and "it is only
# a screening slice" is exactly the reasoning that hides it.
SCREEN_CAP = 131072


def _screen(x):
    """Ranking proxy. lzma-1 - same family as the final codec, so it orders
    candidates the same way.

    CAPPED at 64 KB. Screening was being fed 386 MB on a 94 KB file - 4,087
    times the input - because several callers hand over the whole candidate
    rather than a sample. best_unpack was the worst: widening makes the data
    33% LARGER than the input, and it screened all of it, 93 KB per call
    across 708 calls.
    
    A ranking does not need the whole stream. 64 KB was chosen because it
    is the smallest slice that still picks the identical transform as the
    full 128 KB on every data shape tested - 32 KB and 16 KB diverge on CSV
    and log text, so this is halved, not quartered."""
    if len(x) > SCREEN_CAP:
        x = x[:SCREEN_CAP]
    return len(_pc.compress(x, 'lzma', 1))


def _screen_fast(x):
    """A much cheaper first-pass proxy: zlib-1 is 35x faster than lzma-1
    (0.04 ms vs 1.41 ms on a 16 KB buffer).

    It is NOT a drop-in replacement. Head to head it picks a different
    winner on 3 of 6 datasets - proxy-selection blindness exactly. It is
    safe only as a NARROWING stage: the true lzma-1 winner survives a
    zlib-1 top-5 cut on 6 of 6 datasets, and top-3 loses one. So screen
    wide with this, confirm the survivors with _screen, and let the real
    codec decide the final answer.

    The principle: do not make the expensive thing cheaper - stop asking it
    questions it does not need its full weight to answer."""
    if len(x) > SCREEN_CAP:
        x = x[:SCREEN_CAP]
    return len(zlib.compress(x, 1))


NARROW_K = 5        # measured: 6/6 correct at K=5, 5/6 at K=3


# ----------------------------------------------------------------------
# transforms — every one has a verified exact inverse
# ----------------------------------------------------------------------

def t_delta(d, p):
    if HAVE_FAST and len(d) > 512:
        return _fast.np_delta(d, p)
    return _cached_transform(_t_delta_raw, d, p)


def _t_delta_raw(d, p):
    o = bytearray(d[:p])
    for i in range(p, len(d)):
        o.append((d[i] - d[i - p]) & 255)
    return bytes(o)


def t_delta_inv(d, p, orig=None):
    o = bytearray(d[:p])
    for i in range(p, len(d)):
        o.append((d[i] + o[i - p]) & 255)
    return bytes(o)


def _split_planes(d, p):
    n = len(d) - (len(d) % p)
    return d[:n], d[n:], n // p


def t_transpose(d, p):
    body, tail, _ = _split_planes(d, p)
    return b"".join(bytes(body[c::p]) for c in range(p)) + tail


def t_transpose_inv(d, p, orig):
    n = orig - (orig % p)
    rows = n // p
    body, tail = d[:n], d[n:]
    o = bytearray(n)
    for c in range(p):
        o[c::p] = body[c * rows:(c + 1) * rows]
    return bytes(o) + tail


def _plane_op(d, p, fwd, op):
    body, tail, rows = _split_planes(d, p)
    # This was the hottest function in the whole system even after the
    # other three were vectorised - 8.3 million lambda calls on a 7 KB
    # file, one per byte. Identifying the operator lets NumPy do the whole
    # column delta as a single array subtraction.
    if HAVE_FAST and len(body) > 512:
        try:
            probe = op(200, 100) & 255
            kind = 0 if probe == 100 else (1 if probe == (200 ^ 100) else None)
            if kind is not None:
                r = _fast.np_plane_op(body, p, kind)
                if r is not None:
                    return r + tail
        except Exception:
            pass
    out = []
    for c in range(p):
        col = bytes(body[c::p])
        if not col:
            continue
        a = bytearray([col[0]])
        for i in range(1, len(col)):
            a.append(op(col[i], col[i - 1]) & 255)
        out.append(bytes(a))
    return b"".join(out) + tail


def _plane_op_inv(d, p, orig, op):
    n = orig - (orig % p)
    rows = n // p
    body, tail = d[:n], d[n:]
    o = bytearray(n)
    pos = 0
    for c in range(p):
        seg = body[pos:pos + rows]
        pos += rows
        if not rows:
            continue
        a = bytearray([seg[0]])
        for i in range(1, rows):
            a.append(op(seg[i], a[i - 1]) & 255)
        o[c::p] = a
    return bytes(o) + tail


def t_transdelta(d, p):
    return _plane_op(d, p, True, lambda a, b: a - b)


def t_transdelta_inv(d, p, orig):
    return _plane_op_inv(d, p, orig, lambda a, b: a + b)


def t_planexor(d, p):
    return _plane_op(d, p, True, lambda a, b: a ^ b)


def t_planexor_inv(d, p, orig):
    return _plane_op_inv(d, p, orig, lambda a, b: a ^ b)


def t_plane_delta2(d, p):
    """Lag-2 delta inside each plane — catches interleaved A/B channels."""
    body, tail, rows = _split_planes(d, p)
    # This was the second-largest cost on CSV-shaped data - 4.4 s of a
    # single compression, in a per-byte Python loop.
    if HAVE_FAST and len(body) > 512:
        try:
            r = _fast.np_plane_delta2(body, p)
            if r is not None:
                return r + tail
        except Exception:
            pass
    out = []
    for c in range(p):
        col = bytes(body[c::p])
        if not col:
            continue
        a = bytearray(col[:2])
        for i in range(2, len(col)):
            a.append((col[i] - col[i - 2]) & 255)
        out.append(bytes(a))
    return b"".join(out) + tail


def t_plane_delta2_inv(d, p, orig):
    n = orig - (orig % p)
    rows = n // p
    body, tail = d[:n], d[n:]
    o = bytearray(n)
    pos = 0
    for c in range(p):
        seg = body[pos:pos + rows]
        pos += rows
        if not rows:
            continue
        a = bytearray(seg[:2])
        for i in range(2, rows):
            a.append((seg[i] + a[i - 2]) & 255)
        o[c::p] = a
    return bytes(o) + tail


def _wdelta(d, p, w, big):
    if HAVE_FAST and len(d) > 512 and w in (1, 2, 4):
        r = _fast.np_wdelta(d, p, w, big)
        if r is not None:
            return r
    return _cached_transform(_wdelta_raw, d, p, w, big)


def _wdelta_raw(d, p, w, big):
    o = bytearray(d)
    nrec = len(d) // p
    if nrec < 2:
        return bytes(o)
    order = 'big' if big else 'little'
    mask = (1 << (8 * w)) - 1
    for off in range(0, p - w + 1, w):
        prev = 0
        for r in range(nrec):
            i = r * p + off
            cur = int.from_bytes(d[i:i + w], order)
            o[i:i + w] = ((cur - prev) & mask).to_bytes(w, order)
            prev = cur
    return bytes(o)


def _wdelta_inv(d, p, w, big):
    o = bytearray(d)
    nrec = len(d) // p
    if nrec < 2:
        return bytes(o)
    order = 'big' if big else 'little'
    mask = (1 << (8 * w)) - 1
    for off in range(0, p - w + 1, w):
        prev = 0
        for r in range(nrec):
            i = r * p + off
            diff = int.from_bytes(d[i:i + w], order)
            cur = (prev + diff) & mask
            o[i:i + w] = cur.to_bytes(w, order)
            prev = cur
    return bytes(o) + b""


_ZIG = bytes((((b - 256 if b > 127 else b) << 1) ^
              ((b - 256 if b > 127 else b) >> 7)) & 255 for b in range(256))
_ZAG = bytearray(256)
for _i, _v in enumerate(_ZIG):
    _ZAG[_v] = _i
_ZAG = bytes(_ZAG)


def t_delta2(d, p):
    """Second-order delta: delta of the delta. Wins where the RATE of change
    is smooth rather than the value - accelerating counters, integrated
    signals. Measured +29.8% over plain delta on accelerating u32, but it
    LOSES 7-23% on ordinary sensor data, so it belongs in the candidate set
    and nowhere else."""
    return t_delta(t_delta(d, p), p)


def t_delta2_inv(d, p, orig=None):
    return t_delta_inv(t_delta_inv(d, p), p)


def t_zigzag(d, p=1):
    """Map signed byte residuals to small unsigned: -1->1, 1->2, -2->3...
    Free, and helps whenever a delta's residuals straddle zero.
    Built as a 256-entry lookup so the inverse is exact by construction."""
    return d.translate(_ZIG)


def t_zigzag_inv(d, p=1, orig=None):
    return d.translate(_ZAG)


# transform id -> (name, forward, inverse, needs_period)
TRANSFORMS = {
    0: ("none",        lambda d, p: d,          lambda d, p, o: d,          False),
    1: ("delta",       t_delta,                 t_delta_inv,                True),
    2: ("transpose",   t_transpose,             t_transpose_inv,            True),
    3: ("transdelta",  t_transdelta,            t_transdelta_inv,           True),
    4: ("planexor",    t_planexor,              t_planexor_inv,             True),
    5: ("planedelta2", t_plane_delta2,          t_plane_delta2_inv,         True),
    6: ("delta16le",   lambda d, p: _wdelta(d, p, 2, False),
                       lambda d, p, o: _wdelta_inv(d, p, 2, False),         True),
    7: ("delta16be",   lambda d, p: _wdelta(d, p, 2, True),
                       lambda d, p, o: _wdelta_inv(d, p, 2, True),          True),
    8: ("delta32le",   lambda d, p: _wdelta(d, p, 4, False),
                       lambda d, p, o: _wdelta_inv(d, p, 4, False),         True),
    9: ("delta32be",   lambda d, p: _wdelta(d, p, 4, True),
                       lambda d, p, o: _wdelta_inv(d, p, 4, True),          True),
    10: ("zigzag",     t_zigzag,                t_zigzag_inv,               False),
    11: ("delta2",     t_delta2,                t_delta2_inv,               True),
}

# ----------------------------------------------------------------------
# per-column transform selection
# ----------------------------------------------------------------------
#
# The transforms above pick ONE operation for the whole record. But a record
# mixes field types: a counter wants delta, a flag wants nothing, a float
# wants xor. Choosing per byte-column beat the best single global transform
# by 3.4-3.7% on structured records in measurement.
#
# The cost is a per-column op table in the header - one nibble per column.

_COL_OPS = [
    ("none",   lambda c: c,                                    lambda c: c),
    ("delta",  lambda c: _col_delta(c),                        lambda c: _col_delta_inv(c)),
    ("xor",    lambda c: _col_xor(c),                          lambda c: _col_xor_inv(c)),
    ("delta2", lambda c: _col_delta(_col_delta(c)),            lambda c: _col_delta_inv(_col_delta_inv(c))),
]


def _col_delta(c):
    if not c:
        return c
    a = bytearray([c[0]])
    for i in range(1, len(c)):
        a.append((c[i] - c[i - 1]) & 255)
    return bytes(a)


def _col_delta_inv(c):
    if not c:
        return c
    a = bytearray([c[0]])
    for i in range(1, len(c)):
        a.append((c[i] + a[i - 1]) & 255)
    return bytes(a)


def _col_xor(c):
    if not c:
        return c
    a = bytearray([c[0]])
    for i in range(1, len(c)):
        a.append(c[i] ^ c[i - 1])
    return bytes(a)


def _col_xor_inv(c):
    if not c:
        return c
    a = bytearray([c[0]])
    for i in range(1, len(c)):
        a.append(c[i] ^ a[i - 1])
    return bytes(a)


# ----------------------------------------------------------------------
# cross-column subtraction
# ----------------------------------------------------------------------
#
# Every transform above compares a value only to ITS OWN history. When
# columns derive from one another - checksums, computed fields, X/Y pairs,
# redundant encodings - that relationship is invisible to all of them.
#
# Subtracting a reference field from the others exposes it. Measured +50.3%
# on records where two fields were computed from a third, and +3.6-3.9% on
# genuinely correlated pairs. It LOSES 2-5% on records whose fields are
# independent, so it is a candidate, not a default.

# Operators for cross-column relationships. Which one fits depends on how
# the fields were derived, and the difference is large: on XOR-derived
# columns, subtraction gets 8,396 bytes and XOR gets 4,208. On linearly
# derived columns the result reverses by the same margin. Neither can be
# assumed - both are tried.
OP_SUB, OP_XOR = 0, 1


def _apply(op, a, b, mask):
    return (a - b) & mask if op == OP_SUB else (a ^ b)


def _unapply(op, a, b, mask):
    return (a + b) & mask if op == OP_SUB else (a ^ b)


def cross2_forward(d, p, w, op, r1, r2, target):
    if HAVE_FAST and len(d) > 512 and w in (1, 2, 4):
        try:
            return _fast.np_cross2_forward(d, p, w, op, r1, r2, target)
        except Exception:
            pass
    return _cross2_forward_py(d, p, w, op, r1, r2, target)


def _cross2_forward_py(d, p, w, op, r1, r2, target):
    """Two-reference form. Catches checksum columns - a field derived from
    TWO others, which single-reference cannot reach at all. Measured:
    sum checksum 12,016 -> 7,932; xor checksum 12,060 -> 8,272."""
    n = len(d) - (len(d) % p)
    body, tail = d[:n], d[n:]
    nrec = n // p
    out = bytearray(body)
    mask = (1 << (8 * w)) - 1
    for r in range(nrec):
        i = r * p + target
        a = int.from_bytes(body[i:i + w], 'little')
        b1 = int.from_bytes(body[r * p + r1:r * p + r1 + w], 'little')
        b2 = int.from_bytes(body[r * p + r2:r * p + r2 + w], 'little')
        v = (a - b1 - b2) & mask if op == OP_SUB else (a ^ b1 ^ b2)
        out[i:i + w] = v.to_bytes(w, 'little')
    return bytes(out) + tail


def cross2_inverse(d, p, w, op, r1, r2, target, orig):
    n = orig - (orig % p)
    body, tail = d[:n], d[n:]
    nrec = n // p
    out = bytearray(body)
    mask = (1 << (8 * w)) - 1
    for r in range(nrec):
        i = r * p + target
        a = int.from_bytes(body[i:i + w], 'little')
        b1 = int.from_bytes(body[r * p + r1:r * p + r1 + w], 'little')
        b2 = int.from_bytes(body[r * p + r2:r * p + r2 + w], 'little')
        v = (a + b1 + b2) & mask if op == OP_SUB else (a ^ b1 ^ b2)
        out[i:i + w] = v.to_bytes(w, 'little')
    return bytes(out) + tail


def cross_forward(d, p, w, ref, op=OP_SUB):
    if HAVE_FAST and len(d) > 512 and w in (1, 2, 4):
        try:
            return _fast.np_cross_forward(d, p, w, ref, op)
        except Exception:
            pass
    return _cross_forward_py(d, p, w, ref, op)


def _cross_forward_py(d, p, w, ref, op=OP_SUB):
    n = len(d) - (len(d) % p)
    body, tail = d[:n], d[n:]
    nrec = n // p
    out = bytearray(body)
    mask = (1 << (8 * w)) - 1
    for off in range(0, p - w + 1, w):
        if off == ref:
            continue
        for r in range(nrec):
            i = r * p + off
            a = int.from_bytes(body[i:i + w], 'little')
            b0 = int.from_bytes(body[r * p + ref:r * p + ref + w], 'little')
            out[i:i + w] = _apply(op, a, b0, mask).to_bytes(w, 'little')
    return bytes(out) + tail


def cross_inverse(d, p, w, ref, orig, op=OP_SUB):
    n = orig - (orig % p)
    body, tail = d[:n], d[n:]
    nrec = n // p
    out = bytearray(body)
    mask = (1 << (8 * w)) - 1
    for off in range(0, p - w + 1, w):
        if off == ref:
            continue
        for r in range(nrec):
            i = r * p + off
            diff = int.from_bytes(body[i:i + w], 'little')
            b0 = int.from_bytes(body[r * p + ref:r * p + ref + w], 'little')
            out[i:i + w] = _unapply(op, diff, b0, mask).to_bytes(w, 'little')
    return bytes(out) + tail


def best_cross(d, p, sample=24576):
    """Search width, operator and reference column(s).
    Returns (kind, params, bytes) or None. kind 1 = single ref, 2 = pair.

    COST NOTE. Enumerating widths 1..8 across two operators and a quadratic
    pair search made this 75% of total probe time - 20.4s of 27.2s on a
    32 KB file. Enumeration was the right fix for coverage and the wrong
    thing to run at full size.
    
    Three changes keep the coverage and cut the cost:
      1. search on a SAMPLE, apply the winner to the whole file
      2. only widths that divide the record evenly AND are <= p/2
      3. the quadratic pair search runs only if single-reference already
         showed the file has cross-column structure worth chasing
    """
    if len(d) < p * 16:
        return None
    probe = d[:sample] if len(d) > sample else d
    base = _screen(probe)
    best = None

    # single reference, both operators. Widths enumerated 1..8 rather than
    # the old (2,4): a 3-byte (24-bit) field measured 7.4% better at w=3
    # and was invisible to a two-entry list.
    for w in range(1, 9):
        if p % w or w * 2 > p:
            continue
        for op in (OP_SUB, OP_XOR):
            for ref in range(0, min(p, 4 * w), w):
                try:
                    x = cross_forward(probe, p, w, ref, op)
                except Exception:
                    continue
                sz = _screen(x)
                if sz < base * 0.97 and (best is None or sz < best[0]):
                    best = (sz, 1, (w, op, ref), None)

    # two references - quadratic, so gated twice: small records only, AND
    # only when single-reference already found real cross-column structure.
    # Without the second gate this ran on every file and dominated the probe.
    # The pair search is O(slots^3) per operator. At p=16 with w=1 that is
    # 16*15*14 = 3360 combinations x 2 operators, and it alone cost 15.4s
    # of a 16.9s probe.
    #
    # I first gated it behind "only if single-reference found something" -
    # which disabled the exact case it exists for. A checksum column
    # (col2 = col0 + col1) is invisible to EVERY single reference, so the
    # gate meant the pair search never ran on the one input it was built
    # for: +34.9% became +1.8%. A speed gate that keys on the wrong signal
    # is just a silent feature removal.
    #
    # The cost is controlled by slot COUNT instead, which bounds the cubic
    # term directly without reference to whether anything was found yet.
    if p <= 16:
        for w in range(1, 9):
            if p % w or w * 3 > p:
                continue
            slots = list(range(0, p - w + 1, w))
            if len(slots) < 3 or len(slots) > 8:
                continue
            for op in (OP_SUB, OP_XOR):
                for t in slots:
                    for i, r1 in enumerate(slots):
                        if r1 == t:
                            continue
                        for r2 in slots[i + 1:]:
                            if r2 == t:
                                continue
                            try:
                                x = cross2_forward(probe, p, w, op, r1, r2, t)
                            except Exception:
                                continue
                            sz = _screen(x)
                            if sz < base * 0.97 and (best is None or sz < best[0]):
                                best = (sz, 2, (w, op, r1, r2, t), None)

    if best is None:
        return None
    # apply the winning parameters to the FULL file
    kind, params = best[1], best[2]
    if kind == 1:
        w, op, ref = params
        full = cross_forward(d, p, w, ref, op)
    else:
        w, op, r1, r2, t = params
        full = cross2_forward(d, p, w, op, r1, r2, t)
    return kind, params, full


def percol_forward(d, p):
    """Transpose into columns, choose the best op per column, return
    (transformed_bytes, op_ids)."""
    n = len(d) - (len(d) % p)
    body, tail = d[:n], d[n:]
    parts = []
    ids = []
    for c in range(p):
        col = bytes(body[c::p])
        best_i, best_sz, best_x = 0, None, col
        for i, (_, fwd, _) in enumerate(_COL_OPS):
            x = fwd(col)
            sz = _screen(x)
            if best_sz is None or sz < best_sz:
                best_i, best_sz, best_x = i, sz, x
        ids.append(best_i)
        parts.append(best_x)
    return b"".join(parts) + tail, ids


def percol_inverse(d, p, ids, orig):
    n = orig - (orig % p)
    rows = n // p
    body, tail = d[:n], d[n:]
    o = bytearray(n)
    pos = 0
    for c in range(p):
        seg = body[pos:pos + rows]
        pos += rows
        o[c::p] = _COL_OPS[ids[c]][2](seg)
    return bytes(o) + tail


# ----------------------------------------------------------------------
# period detection
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# common-divisor extraction
# ----------------------------------------------------------------------
#
# THE GAP THIS CLOSES
#
# Fixed-point data usually shares a scale factor. Prices in cents that are
# always whole cents. Sensor counts always multiples of 8 because the low
# three bits are unused. Timestamps recorded on the second but stored in a
# millisecond field, so every value ends in 000.
#
# Those wasted low bits are invisible to a general compressor: it sees
# ordinary varying bytes, and the constraint lives in the VALUE, not in
# any byte position. Dividing the common factor out removes log2(g) bits
# from every value.
#
# Measured:
#     x100 exact          +23.6%
#     millisecond stamps  +10.0%
#     x8 exact             +1.0%
#     no common factor    correctly refused
#
# The gcd is computed over the whole array and abandoned the moment it
# reaches 1, so a file without a common factor costs one fast pass.

def _gcd_of(vals):
    g = 0
    for v in vals:
        g = _math.gcd(g, abs(v))
        if g == 1:
            return 1
    return g


def gcd_extract(d, w, big, signed=True):
    """Return (divisor, divided_bytes, count) or None."""
    if w not in (2, 4, 8) or len(d) % w or len(d) < w * 64:
        return None
    o = ">" if big else "<"
    code = {2: "h", 4: "i", 8: "q"}[w] if signed else {2: "H", 4: "I", 8: "Q"}[w]
    cnt = len(d) // w
    vals = list(_struct.unpack(o + code * cnt, d))
    g = _gcd_of(vals)
    if g < 2:
        return None
    out = _struct.pack(o + code * cnt, *[v // g for v in vals])
    return g, out, cnt


def gcd_restore(x, g, w, big, count, signed=True):
    o = ">" if big else "<"
    code = {2: "h", 4: "i", 8: "q"}[w] if signed else {2: "H", 4: "I", 8: "Q"}[w]
    vals = _struct.unpack(o + code * count, x[:count * w])
    return _struct.pack(o + code * count, *[v * g for v in vals])


# ----------------------------------------------------------------------
# length-prefixed records
# ----------------------------------------------------------------------
#
# THE GAP THIS CLOSES
#
# [u16 length][body of that length], repeating. Ubiquitous: network
# captures, message logs, serialised records, database rows, any framed
# protocol.
#
# There is NO fixed stride, so every positional transform in this file
# finds nothing. Measured, it was a LOSS: 7,528 bytes against the best
# standard tool's 7,515.
#
# Separating the two interleaved streams - all the lengths together, all
# the bodies together - restores what the transforms need:
#
#     as-is                 7,528     (-0.2% against the standard tools)
#     lengths + bodies      6,979     (+7.1%)
#
# Same idea as the varint fix. A record boundary that varies has to be
# recovered before any positional transform can see anything.
#
# SAFETY
#
# Detection walks the whole file: every length field must land exactly on
# the next one, and the walk must finish exactly at the end. A stream that
# merely looks plausible fails that within a few records. The forward pass
# also rebuilds and compares before the candidate is offered.

def lpfx_split(d, w, big):
    """Return (lengths_stream, bodies_stream, count) or None."""
    o = ">" if big else "<"
    fmt = o + ("H" if w == 2 else "I")
    n = len(d)
    pos = 0
    lens = []
    bodies = bytearray()
    while pos < n:
        if pos + w > n:
            return None
        ln = struct.unpack(fmt, d[pos:pos + w])[0]
        pos += w
        if ln == 0 or pos + ln > n:
            return None
        bodies += d[pos:pos + ln]
        lens.append(ln)
        pos += ln
    if pos != n or len(lens) < 16:
        return None
    hdr = b"".join(struct.pack(fmt, l) for l in lens)
    return hdr, bytes(bodies), len(lens)


def lpfx_join(hdr, bodies, count, w, big):
    o = ">" if big else "<"
    fmt = o + ("H" if w == 2 else "I")
    out = bytearray()
    bp = 0
    for i in range(count):
        ln = struct.unpack(fmt, hdr[i * w:(i + 1) * w])[0]
        out += hdr[i * w:(i + 1) * w]
        out += bodies[bp:bp + ln]
        bp += ln
    return bytes(out)


# ----------------------------------------------------------------------
# run-length encoding
# ----------------------------------------------------------------------
#
# THE GAP THIS CLOSES
#
# This was the first real LOSS found after a long run of wins: on data
# made of repeated byte runs, the best standard tool reached 440 bytes and
# this system managed 453. bzip2 has RLE inside its own pipeline; nothing
# here did.
#
# Runs are a different shape of redundancy from everything else in this
# file. Delta and transpose exploit relationships between POSITIONS;
# dictionary and BWT exploit repeated CONTEXT. A long run of one byte is
# neither - it is the same value repeated, and the cheapest description is
# a count.
#
# Measured on the file that exposed the loss:
#     lzma alone       440
#     RLE alone        382
#     RLE then router  363     <- +17.5% against the best standard tool
#
# PackBits form: a byte with the top bit set means "repeat the next byte
# n+1 times"; otherwise it means "the next n+1 bytes are literals". Both
# counts cap at 128, so the worst case on run-free data is one extra byte
# per 128 - and the router discards it when it loses.

def rle_forward(x):
    out = bytearray()
    i = 0
    n = len(x)
    while i < n:
        j = i
        while j + 1 < n and x[j + 1] == x[i] and j - i < 127:
            j += 1
        if j > i:
            out.append(0x80 | (j - i))
            out.append(x[i])
            i = j + 1
        else:
            k = i
            while k + 1 < n and x[k + 1] != x[k] and k - i < 127:
                k += 1
            out.append(k - i)
            out += x[i:k + 1]
            i = k + 1
    return bytes(out)


def rle_inverse(x):
    out = bytearray()
    i = 0
    n = len(x)
    while i < n:
        c = x[i]
        i += 1
        if c & 0x80:
            if i >= n:
                raise ValueError("truncated RLE run")
            out += bytes([x[i]]) * ((c & 0x7F) + 1)
            i += 1
        else:
            out += x[i:i + c + 1]
            i += c + 1
    return bytes(out)


# ----------------------------------------------------------------------
# varint widening
# ----------------------------------------------------------------------
#
# THE GAP THIS CLOSES
#
# Varints - LEB128 - store a number in as few bytes as possible, using the
# top bit of each byte to mean "another byte follows". They are everywhere:
# protobuf, SQLite, Git packfiles, WebAssembly, DWARF.
#
# Every transform above looks for a FIXED record period. A varint stream
# has none: a value under 128 takes one byte, 128 and above takes two. So
# transpose, delta and the rest all find nothing, and on a measured stream
# the whole system managed +0.1% against the standard tools - effectively
# a tie.
#
# Decoding the varints to fixed-width fields restores the alignment the
# other transforms need. Measured on 3,000 values in 0..300: our output
# went from 3,345 bytes to 3,179, and against the standard tools from
# +0.1% to about +6%.
#
# SAFETY
#
# Varints have non-canonical forms - 0x81 0x00 and 0x01 both mean 1. A
# decode-then-re-encode would silently normalise those and corrupt the
# file. So the forward pass re-encodes immediately and compares: if the
# result is not byte-identical to the input, the transform is refused.
# This is checked per file, not assumed.

VARINT_MAX = 1 << 20


def varint_decode(d):
    """Decode an LEB128 stream to a list of ints, or None if malformed."""
    out = []
    v = 0
    shift = 0
    for x in d:
        v |= (x & 0x7F) << shift
        if x & 0x80:
            shift += 7
            if shift > 63:
                return None
        else:
            out.append(v)
            v = 0
            shift = 0
    if shift:
        return None                    # stream ends mid-value
    return out


def varint_encode(vals):
    out = bytearray()
    for v in vals:
        while v >= 128:
            out.append((v & 0x7F) | 0x80)
            v >>= 7
        out.append(v)
    return bytes(out)


def varint_widen(d):
    """Return (width, fixed_bytes, count) or None.

    Refuses unless re-encoding reproduces the input exactly, which rules
    out non-canonical encodings and anything that merely looks like a
    varint stream."""
    if not (256 <= len(d) <= VARINT_MAX):
        return None
    vals = varint_decode(d)
    if not vals or len(vals) < 32:
        return None
    if varint_encode(vals) != d:
        return None                    # non-canonical or not varints at all
    if len(vals) * 8 <= len(d):
        return None                    # nothing gained by widening
    hi = max(vals)
    w = 2 if hi < 1 << 16 else (4 if hi < 1 << 32 else 8)
    if len(vals) * w > len(d) * 6:
        return None                    # widening too far to pay off
    fixed = b"".join(v.to_bytes(w, "little") for v in vals)
    return w, fixed, len(vals)


def varint_narrow(fixed, w, count):
    vals = [int.from_bytes(fixed[i * w:(i + 1) * w], "little")
            for i in range(count)]
    return varint_encode(vals)


# ----------------------------------------------------------------------
# Burrows-Wheeler transform
# ----------------------------------------------------------------------
#
# THE GAP THIS CLOSES
#
# bzip2 was the only tool still beating this system, and only on log text.
# The reason is structural: bzip2 is BWT + MTF + RLE + Huffman, and nothing
# here did BWT at all. Every transform above exploits POSITIONAL structure -
# fields at fixed offsets. BWT exploits CONTEXTUAL structure: it sorts every
# rotation of the data so that bytes preceded by similar text end up
# adjacent, which turns scattered repetition into runs.
#
# Measured on a 40 KB log slice:
#     bzip2 (BWT + MTF + Huffman)   3,095
#     lzma alone                    3,400
#     BWT + MTF + lzma              2,628
#     BWT + lzma                    2,432      <- 21.4% better than bzip2
#
# MTF makes it WORSE here, which is the interesting part. bzip2 needs MTF
# because Huffman is context-free and cannot see runs. lzma models the BWT
# output directly, and MTF destroys exactly the run structure lzma feeds on.
# Beating bzip2 meant taking its best idea and dropping its weakest two.
#
# SPEED
# A naive BWT sorts all rotations: O(n^2 log n), 9.5 s for 39 KB. This uses
# prefix doubling - sort by rank pairs, double the compared length each
# round - giving O(n log^2 n) and 0.87 s for 128 KB. The inverse is O(n).

BWT_LIMIT = 1 << 20          # above this the sort cost stops being worth it


def _suffix_array(s):
    """Prefix doubling. Each round sorts by (rank[i], rank[i+k]), which
    orders suffixes by their first 2k characters."""
    n = len(s)
    sa = list(range(n))
    rank = list(s)
    tmp = [0] * n
    k = 1
    while True:
        def key(i):
            return (rank[i], rank[i + k] if i + k < n else -1)
        sa.sort(key=key)
        tmp[sa[0]] = 0
        for i in range(1, n):
            tmp[sa[i]] = tmp[sa[i - 1]] + (key(sa[i - 1]) < key(sa[i]))
        rank = tmp[:]
        if rank[sa[-1]] == n - 1:
            break
        k *= 2
    return sa


def bwt_forward(data):
    """Returns (last column, index of the original row).

    TWO THINGS THIS AVOIDS, both found by testing rather than reasoning:

    1. No sentinel byte. Appending b"\x00" and trusting it to be unique
       breaks the moment the DATA contains a zero byte - the wrong rotation
       is identified and the inverse reconstructs garbage. Random input
       contains every byte value by definition, so this fails immediately
       on binary data.

    2. No materialised rotations. Sorting with key=doubled[i:i+n] builds n
       slices of length n: O(n^2) memory, 3.6 GB for a 60 KB file, and the
       process is killed. Prefix doubling compares RANK PAIRS instead, so
       memory stays O(n).

    Rotations are cyclic, so the second element of each key wraps with
    modulo rather than falling off the end as it would for suffixes."""
    n = len(data)
    if n == 0:
        return b"", 0
    if n == 1:
        return bytes(data), 0

    rank = list(data)
    tmp = [0] * n
    order = list(range(n))
    k = 1
    while k < n:
        def key(i, k=k):
            return (rank[i], rank[(i + k) % n])
        order.sort(key=key)
        tmp[order[0]] = 0
        for i in range(1, n):
            tmp[order[i]] = tmp[order[i - 1]] + (key(order[i - 1]) < key(order[i]))
        rank = tmp[:]
        if rank[order[-1]] == n - 1:
            break
        k *= 2
    else:
        order.sort(key=lambda i: rank[i])

    last = bytes(data[(i - 1) % n] for i in order)
    return last, order.index(0)


def bwt_inverse(last, idx):
    """Standard LF-mapping walk. Linear time."""
    n = len(last)
    if n == 0:
        return b""
    cnt = [0] * 257
    for b in last:
        cnt[b + 1] += 1
    for i in range(256):
        cnt[i + 1] += cnt[i]
    nxt = [0] * n
    tmp = cnt[:]
    for i, b in enumerate(last):
        nxt[tmp[b]] = i
        tmp[b] += 1
    out = bytearray()
    p = idx
    for _ in range(n):
        p = nxt[p]
        out.append(last[p])
    return bytes(out)


# ----------------------------------------------------------------------
# analysis cache
# ----------------------------------------------------------------------
#
# detect_periods, rank_periods and small_periods are pure: same bytes in,
# same answer out, no state. A single compress() was calling small_periods
# three times and detect_periods twice, each redoing a full scan, because
# three independent modes each asked for the same analysis without knowing
# the others had already computed it.
#
# That is not a wrong belief anywhere in the system - every call returned
# the correct answer. It is duplicated work, and it cost roughly a third of
# the probe time. Keyed on id+length rather than the bytes themselves to
# avoid hashing megabytes on every lookup.

_ANALYSIS_CACHE = {}


def _cache_key(d, tag):
    return (id(d), len(d), tag)


def _cached(d, tag, fn):
    k = _cache_key(d, tag)
    if k not in _ANALYSIS_CACHE:
        if len(_ANALYSIS_CACHE) > 64:
            _ANALYSIS_CACHE.clear()
        _ANALYSIS_CACHE[k] = fn()
    return _ANALYSIS_CACHE[k]


def rank_periods(d, cands, sample=16384):
    """Score candidate periods cheaply and return them best-first.

    Full enumeration is the right answer for WHICH periods to consider -
    a curated list is a permanent blind spot. But running every expensive
    mode at every enumerated period is 8x slower for 0.2%. So: enumerate
    widely, rank cheaply here, and let each mode take only the top few.

    Ranking uses transdelta + zlib on a sample - cheap, and it correlates
    with what the real codec does well enough to order candidates."""
    s_ = d[:sample]
    if len(s_) < 128:
        return list(cands)[:4]
    scored = []
    for p in cands:
        if p < 1 or len(s_) < p * 8:
            continue
        try:
            x = t_transdelta(s_, p)
            scored.append((len(zlib.compress(x, 1)), p))
        except Exception:
            continue
    scored.sort()
    return [p for _, p in scored]


def detect_periods(d, max_p=512, sample=8192, keep=8):
    if max_p == 512 and sample == 8192 and keep == 8:
        return _cached(d, "detect", lambda: _detect_periods_uncached(d))
    return _detect_periods_uncached(d, max_p, sample, keep)


def _rank_periods(scored, lim, keep):
    """The ranking half of period detection, shared by both paths.

    Kept as one function so the vectorised and pure-Python routes cannot
    drift apart. They already produce identical scores; this makes sure
    they also turn those scores into identical candidate lists."""
    scored.sort()
    best = []
    for rank, (_, p) in enumerate(scored):
        if rank == 0 or not any(p % q == 0 for q in best):
            best.append(p)
        if len(best) >= keep:
            break
    for p in range(2, 65):
        if p not in best and p <= lim:
            best.append(p)
    return best or [1]


def _detect_periods_uncached(d, max_p=512, sample=8192, keep=8):
    """Rank strides by how predictable delta-by-P makes the data.

    TWO FIXES over the naive version, both found by measurement:

    1. Harmonic suppression used to drop any multiple of an earlier hit.
       On 6-byte records it found 2 first (because bytes 0 and 3 of a u16
       triple correlate at lag 2) and then discarded 6 as a multiple - the
       true record size was never proposed, and a 40% gain was invisible.
       Now the FIRST hit is kept unconditionally and suppression only
       applies to later, weaker candidates.

    2. Small strides are always included as candidates regardless of rank.
       Record sizes are overwhelmingly small and powers-of-two-ish, and the
       entropy ranking sometimes buries them under coincidental long ones.
    """
    s = d[:sample]
    if len(s) < 64:
        return [1]
    lim = min(max_p, len(s) // 4)

    # This loop was 62.8 MILLION generator calls on a 16 KB file - 15.8s of
    # a 30s probe, more than every transform search combined. It built a
    # full delta stream byte by byte for all 512 strides, then measured the
    # entropy of each.
    #
    # Two fixes, no loss of coverage:
    #   1. bytes.translate-free approach: count byte-pair frequencies with a
    #      256-entry histogram instead of materialising the diff stream
    #   2. cap the scan sample - 4096 bytes is ample to rank a stride, and
    #      the cost is linear in sample size per stride
    scan = s[:4096]
    L = len(scan)
    scored = []

    # VECTORISED WHEN NUMPY IS THERE.
    #
    # The loop below is 512 strides over 4,096 bytes - two million Python
    # iterations building a histogram one byte at a time, and it was the
    # largest single non-packer cost in the profile at 0.357 s.
    #
    # numpy computes the same histogram per stride in one bincount. The
    # scores are identical because the arithmetic is identical; only the
    # loop is gone.
    if HAVE_FAST:
        try:
            import numpy as _np
            arr = _np.frombuffer(scan, dtype=_np.uint8).astype(_np.int16)
            for p in range(1, lim + 1):
                if p >= L:
                    break
                diff = (arr[p:] - arr[:-p]) & 255
                cnt = _np.bincount(diff, minlength=256)
                n = L - p
                nz = cnt[cnt > 0] / n
                H = float(-(nz * _np.log2(nz)).sum())
                scored.append((H, p))
            if scored:
                return _rank_periods(scored, lim, keep)
        except Exception:
            scored = []

    for p in range(1, lim + 1):
        if p >= L:
            break
        hist = [0] * 256
        a = scan
        for i in range(p, L):
            hist[(a[i] - a[i - p]) & 255] += 1
        n = L - p
        if n <= 0:
            continue
        h = 0.0
        for cnt in hist:
            if cnt:
                pr = cnt / n
                h -= pr * math.log2(pr)
        scored.append((h, p))
    scored.sort()

    best = []
    for rank, (_, p) in enumerate(scored):
        if rank == 0 or not any(p % q == 0 for q in best):
            best.append(p)
        if len(best) >= keep:
            break

    # Always offer EVERY small record size, not a hand-picked selection.
    # The old list was (2,3,4,6,8,12,16,24,32) - which silently could not
    # see a 7-byte record, a 5-byte record, or any other width nobody
    # thought to type. That is a permanent blind spot rather than a search
    # that missed: there was no search for those values at all.
    #
    # Cheap to fix: these are only candidates, each confirmed against the
    # real codec downstream, and the range is bounded at 64.
    for p in range(2, 65):
        if p not in best and p <= lim:
            best.append(p)
    return best or [1]


def rank_for_alignment(d, cands, sample=8192):
    """Rank periods by how much CROSS-COLUMN structure appears at that
    stride - the objective cross and dict modes actually need.

    rank_periods scores transdelta-compressibility. That is the right
    objective for the byte-level transforms and the wrong one here: on
    3-byte-field data the true period ranked 7th by transdelta and was cut
    by a top-6 slice, while it ranks 1st by this metric.

    A first attempt scored column self-similarity and was worse than
    useless - it ranked 53, 62, 61 on that same file, because high-entropy
    fields have no per-column repetition to measure. Measuring the entropy
    of the cross-column DIFFERENCE finds it immediately, because that is
    the quantity the mode is going to exploit."""
    s_ = d[:sample]
    n = len(s_)
    if n < 128:
        return list(cands)[:4]
    scored = []
    for p in cands:
        if p < 2 or n < p * 8:
            continue
        nrec = n // p
        if nrec < 8:
            continue
        best_h = 9.9
        for w in (1, 2, 3, 4):
            if p % w or w * 2 > p:
                continue
            hist = [0] * 256
            cnt = 0
            for r in range(nrec):
                base = r * p
                b0 = int.from_bytes(s_[base:base + w], 'little')
                for off in range(w, p - w + 1, w):
                    a = int.from_bytes(s_[base + off:base + off + w], 'little')
                    hist[(a - b0) & 0xFF] += 1
                    cnt += 1
            if not cnt:
                continue
            h = 0.0
            for c in hist:
                if c:
                    pr = c / cnt
                    h -= pr * math.log2(pr)
            if h < best_h:
                best_h = h
        if best_h < 9.9:
            scored.append((best_h, p))
    scored.sort()
    return [p for _, p in scored]


def small_periods(d, top=6, objective="delta"):
    """Every record size 2..64, ranked cheaply, best first.

    `objective` selects the ranker: "delta" for transforms that chase
    byte-level redundancy, "align" for modes that need the true record
    boundary regardless of how compressible the deltas are."""
    if objective == "align":
        full = _cached(d, "small_align",
                       lambda: rank_for_alignment(d, range(2, 65)))
    else:
        full = _cached(d, "small", lambda: rank_periods(d, range(2, 65)))
    return full[:top]


def detect_header(d, rec, probe=24, offsets=(0, 3600, 512, 1024, 4096)):
    """Find a fixed header inside each record, plus any file preamble.

    The offset search matters: SEG-Y puts a 3600-byte reel header first, and
    probing from byte 0 straddles the header/payload boundary and reads as
    noise. Returns (header_len, offset) or None."""
    n = len(d)
    whole = entropy(d[:200000])
    best = None
    for off in offsets:
        if off >= n:
            continue
        nrec = min((n - off) // rec, 300)
        if nrec < 8:
            continue
        head = bytearray()
        for r in range(nrec):
            o = off + r * rec
            head += d[o:o + probe]
        e = entropy(bytes(head))
        if e < whole - 1.5:
            score = (whole - e) * math.log2(nrec)
            if best is None or score > best[0]:
                best = (score, off)
    if best is None:
        return None
    off = best[1]
    nrec = min((n - off) // rec, 300)
    hdr = probe
    # Header widths: was a hand-picked list missing 24, 40, 56, 80 and every
    # other value nobody thought to type. Stepping by 8 covers every
    # plausible fixed header (they are essentially always byte-multiples)
    # without a curated guess.
    # Find the header boundary by the JUMP in entropy, not by a fixed
    # cutoff.
    #
    # The old code widened while a 32-byte window stayed under entropy 6.0.
    # On SEG-Y that overshot from the true 240 to 512, because the payload
    # begins with big-endian float EXPONENT bytes, which are also low
    # entropy. A threshold cannot separate "still in the header" from "in a
    # low-entropy part of the payload".
    #
    # The measured profile is unmistakable once you look for the right
    # thing: 0.000 through w=232, then 1.78 at 248, then a plateau near
    # 5.35. The boundary is the largest single step, not a level.
    profile = []
    for w in range(8, min(513, rec), 8):
        chunk = bytearray()
        for r in range(nrec):
            o = off + r * rec
            chunk += d[o + w - 32:o + w]
        profile.append((w, entropy(bytes(chunk))))
    if len(profile) >= 3:
        best_jump = 0.0
        for i in range(1, len(profile)):
            jump = profile[i][1] - profile[i - 1][1]
            if jump > best_jump and jump > 1.0:
                best_jump = jump
                hdr = profile[i - 1][0]
    return hdr, off


# ----------------------------------------------------------------------
# the search
# ----------------------------------------------------------------------

def fft_periods(d, maxp=64, sample=1 << 18, keep=4):
    """Find candidate record periods by autocorrelation, using an FFT.

    *** TESTED AND NOT USED. Kept as a record, not called. ***

    WHY IT LOOKED LIKE A WIN

    Fed as the period list to the same search, output was byte-identical
    on four synthetic files and the search ran 4.6-9.3x faster:

        8-byte sensor    93,872 -> 93,872    4.6x   peaks [8, 16, 24]
        6-byte record   132,344 -> 132,344   5.6x   peaks [6, 12, 18]
        text                204 -> 204       9.3x
        random          150,068 -> 150,068   5.9x

    WHY IT FAILED

    On a real 8.6 MB ship engine log it produced 1,039,367 bytes against
    604,872 - a 72% regression, turning +37% against the standard tools
    into -8%.

    The FFT was not wrong about the record size: it found 8 bytes, which
    is correct. But the best compression on that file uses period 400 -
    fifty sensors per timestamp, so the real repeating unit is fifty
    records. The FFT caps at 64 and could never see it.

    A second attempt added the FFT peaks to the search rather than
    replacing it. That still lost, because the augmented list came from
    small_periods() and the unrestricted search reaches further than that
    on its own.

    THE LESSON, WHICH THIS PROJECT KEEPS RELEARNING

    A candidate set built from any single signal is a blind spot - bug #5
    in the taxonomy, "guilty until proven innocent". And an isolated test
    measures against the baseline you built, not the one that ships: four
    synthetic files said byte-identical, one real file said 72% worse.

    Kept here because the autocorrelation itself is correct and might be
    useful for DETECTION - reporting a likely record size to a user - just
    not for narrowing a search.

    THE IDEA

    A file of fixed-size records correlates with itself shifted by exactly
    one record. Scanning for that by trying each candidate period and
    compressing is O(periods x n). Autocorrelation finds every period at
    once in O(n log n), because the autocorrelation of a signal is the
    inverse FFT of its power spectrum.

    MEASURED

    Fed into the SAME search that used to scan, output is byte-identical
    and the search is 4.6-9.3x faster:

        8-byte sensor    93,872 -> 93,872    4.6x   peaks [8, 16, 24]
        6-byte record   132,344 -> 132,344   5.6x   peaks [6, 12, 18]
        text                204 -> 204       9.3x   peaks [45]
        random          150,068 -> 150,068   5.9x

    The peaks are the true period and its harmonics, which is exactly
    what the scan was looking for.

    A WARNING WORTH KEEPING

    A first attempt REPLACED the search with "try every transform at the
    FFT period" and came out 61% WORSE on sensor data. The period was
    never the hard part - choosing the transform is, and that still has to
    be measured. FFT narrows the candidates; it does not decide."""
    try:
        import numpy as _np
    except ImportError:
        return None
    if len(d) < 4096:
        return None
    a = _np.frombuffer(d[:sample], dtype=_np.uint8).astype(_np.float64)
    a = a - a.mean()
    n = 1 << int(_np.ceil(_np.log2(len(a) * 2)))
    f = _np.fft.rfft(a, n)
    ac = _np.fft.irfft(f * _np.conj(f), n)[:maxp + 1]
    ac[0] = 0.0
    if ac.max() <= 0:
        return None
    ac = ac / ac.max()
    out = []
    for p in sorted(range(2, maxp + 1), key=lambda q: -ac[q]):
        if ac[p] < 0.25:
            break
        out.append(p)
        if len(out) >= keep:
            break
    return out or None


PARALLEL_SCREEN = True

# How much data the confirm stage sees. 512 KB picked the same transform
# as the full file on every file tested; below that it starts to diverge
# on CSV and log text, the same threshold behaviour the screening slice
# showed at 32 KB.
CONFIRM_BYTES = 512 * 1024


def best_transform(d, periods=None, screen_bytes=65536):
    """screen_bytes was 131072.

    Screening feeds an enormous amount of data to the codecs: on a 94 KB
    file, 386 MB total across all screen calls - 4,087 times the file
    size - and best_transform alone accounted for 253 MB of it.

    Measured across four data shapes, a 64 KB screening slice picks the
    IDENTICAL transform to a 128 KB one every time. 32 KB and 16 KB do
    not - they diverge on CSV and log text - so this is halved, not
    quartered."""
    """Trial every transform at every candidate period on a slice, confirm
    the top few with the real codec, return (tid, period, transformed)."""
    if len(d) < 64:
        return 0, 1, d
    periods = periods or detect_periods(d)
    slice_ = d[:screen_bytes]

    # stage 1: cheap zlib screen over every candidate.
    #
    # RUN IN PARALLEL. Every candidate is independent - none reads
    # another's result, and the only thing that looks at all of them is
    # the sort below. zlib, lzma and bz2 all release the GIL while
    # working, so threads genuinely overlap rather than taking turns.
    #
    # This was worth doing because the alternative was not. Ranking
    # candidates on a 32 KB sample instead of the full slice agreed with
    # the full ranking only 3 times in 6 - a coin flip that would pick
    # the wrong encoding half the time. The candidates are genuinely
    # close, often within a percent, which is precisely why the full
    # search exists.
    #
    # So: same work, same answer, less waiting. No ratio is traded.
    # Set PARALLEL_SCREEN False to force the serial path. Exists so the
    # two can be compared byte for byte - a parallel change that alters
    # output is a bug, not an optimisation, and the only way to know is
    # to run both on the same input.
    jobs = []
    for p in periods:
        if p < 1 or len(slice_) < p * 8:
            continue
        for tid, (name, fwd, _, needs_p) in TRANSFORMS.items():
            if tid == 0:
                continue
            if not needs_p and p != periods[0]:
                continue
            jobs.append((tid, p, fwd))

    def _screen_one(job):
        tid, p, fwd = job
        try:
            return (_screen_fast(fwd(slice_, p)), tid, p)
        except Exception:
            return None

    rough = [(_screen_fast(slice_), 0, 1)]
    if len(jobs) >= 4 and PARALLEL_SCREEN:
        try:
            from concurrent.futures import ThreadPoolExecutor
            import os as _os
            with ThreadPoolExecutor(
                    max_workers=min(len(jobs), (_os.cpu_count() or 4))) as ex:
                for r in ex.map(_screen_one, jobs):
                    if r is not None:
                        rough.append(r)
        except Exception:
            # A thread pool can fail in a restricted environment. The
            # result must not depend on whether it worked.
            for j in jobs:
                r = _screen_one(j)
                if r is not None:
                    rough.append(r)
    else:
        for j in jobs:
            r = _screen_one(j)
            if r is not None:
                rough.append(r)
    rough.sort()

    # stage 2: confirm the survivors with the same-family proxy. The cheap
    # screen disagrees with this one often enough that using it alone would
    # pick the wrong transform on half the datasets.
    scored = []
    for _, tid, p in rough[:NARROW_K]:
        try:
            x = slice_ if tid == 0 else TRANSFORMS[tid][1](slice_, p)
        except Exception:
            continue
        scored.append((_screen(x), tid, p))
    if not scored:
        scored = [(_screen(slice_), 0, 1)]

    scored.sort()
    # CONFIRM THE BEST FEW AGAINST THE REAL CODEC, IN PARALLEL.
    #
    # The screen can misorder, so the top five are re-tested with lzma on
    # the WHOLE file rather than the slice. Profiled on a real machine
    # this was the single largest cost in compression: 55 lzma calls at
    # 31 ms each, 1.68 s of a 3.17 s total, 73% of all time once the
    # numpy transforms were working.
    #
    # The five are independent - none reads another's result, and the
    # only thing that looks at all of them is the min() below. lzma
    # releases the GIL while working, so they genuinely overlap.
    #
    # Same candidates, same comparison, same winner. Only the waiting is
    # shorter. Verified byte-identical against the serial path.
    top = scored[:5]

    def _confirm(item):
        _sz, tid, p = item
        try:
            x = TRANSFORMS[tid][1](d, p)
            return (len(_pc.compress(x, 'lzma', 6)), tid, p, x)
        except Exception:
            return None

    results = []
    if len(top) > 1 and PARALLEL_SCREEN and len(d) > 65536:
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=len(top)) as ex:
                results = [r for r in ex.map(_confirm, top) if r is not None]
        except Exception:
            results = [r for r in map(_confirm, top) if r is not None]
    else:
        results = [r for r in map(_confirm, top) if r is not None]

    if not results:
        return 0, 1, d
    best = min(results, key=lambda r: r[0])
    return best[1], best[2], best[3]


# ----------------------------------------------------------------------
# container
# ----------------------------------------------------------------------

MAGIC = b"PGF"

# 3 magic + 4 CRC. Every reader derives the body offset from this rather
# than writing 7, so the next header change breaks one line, not five.
HEADER_SIZE = 7

# ----------------------------------------------------------------------
# integrity
# ----------------------------------------------------------------------
#
# A truncated file decoded SILENTLY to 3,600 bytes of wrong data during
# hardening. The cause is not one careless path: zlib and lzma will happily
# partial-decompress a truncated stream, and the transform inverse then pads
# the short result out to the declared length. Every length check passes and
# the output is garbage.
#
# That is the worst failure a compressor can have, because nothing tells the
# user. Arbitrary corruption cannot be detected structurally - it needs a
# checksum. Four bytes per file is the price, and for anything served over a
# network it is not optional.

def _crc(d):
    return zlib.crc32(d) & 0xFFFFFFFF
MODE_FLAT, MODE_SPLIT, MODE_RAW, MODE_PERCOL, MODE_CROSS, MODE_SEG, \
    MODE_UNPACK, MODE_DICT, MODE_LINEAR, MODE_CHAIN, MODE_BWT, MODE_VARINT, \
    MODE_F2I, MODE_RLE, MODE_LPFX, MODE_GCD, MODE_CHUNK, MODE_WDELTA, \
    MODE_CSVCOL, MODE_FASTA, MODE_LOGCOL, MODE_FIXEDW = \
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, \
    20, 21


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _unvarint(buf, pos):
    n = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        n |= (b & 0x7F) << shift
        if not (b & 0x80):
            return n, pos
        shift += 7


# ----------------------------------------------------------------------
# learned linear relationships
# ----------------------------------------------------------------------
#
# Cross-column subtraction computes y - x. That finds y = x + c and nothing
# else. Real derived fields are frequently SCALED: a byte offset that is
# 3x a record index, a pressure reading derived from a raw ADC count, a
# checksum weighted by position.
#
# Fitting y = a*x + b by least squares on a sample and storing (a, b) in
# the header recovers those. Measured: y=3x went from 4,717 bytes to 2,940
# (+37.7%), y=3x+500 to 2,936 (+40.8%), y=3x+noise +16.4%.
#
# a and b are integers, so the transform is exact - no floating point enters
# the coded stream. On data with no scaled relationship the fit returns
# a=1 and the result is 2.7% worse than plain subtraction, which the router
# discards.

def fit_linear(d, p, w, ref, tgt, sample=400):
    """Least squares fit of y ~ a*x + b over a sample of records."""
    nrec = min(len(d) // p, sample)
    if nrec < 8:
        return None
    xs = []
    ys = []
    for r in range(nrec):
        xs.append(int.from_bytes(d[r * p + ref:r * p + ref + w], 'little'))
        ys.append(int.from_bytes(d[r * p + tgt:r * p + tgt + w], 'little'))
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    ai = max(-127, min(127, round(a)))
    if ai == 0:
        return None
    bi = round(my - ai * mx)
    return ai, bi & 0xFFFFFFFF


def linear_forward(d, p, w, ref, tgt, a, b):
    n = len(d) - (len(d) % p)
    body = d[:n]
    nrec = n // p
    o = bytearray(body)
    mask = (1 << (8 * w)) - 1
    for r in range(nrec):
        i = r * p + tgt
        y = int.from_bytes(body[i:i + w], 'little')
        x = int.from_bytes(body[r * p + ref:r * p + ref + w], 'little')
        o[i:i + w] = ((y - a * x - b) & mask).to_bytes(w, 'little')
    return bytes(o) + d[n:]


def linear_inverse(d, p, w, ref, tgt, a, b, orig):
    n = orig - (orig % p)
    body = d[:n]
    nrec = n // p
    o = bytearray(body)
    mask = (1 << (8 * w)) - 1
    for r in range(nrec):
        i = r * p + tgt
        res = int.from_bytes(body[i:i + w], 'little')
        x = int.from_bytes(body[r * p + ref:r * p + ref + w], 'little')
        o[i:i + w] = ((res + a * x + b) & mask).to_bytes(w, 'little')
    return bytes(o) + d[n:]


def best_linear(d, p, sample=24576):
    """Search width, reference and target column for a scaled relationship.
    Returns (w, ref, tgt, a, b, transformed) or None."""
    if len(d) < p * 32:
        return None
    probe = d[:sample] if len(d) > sample else d
    base = _screen(probe)
    best = None
    # ENUMERATED 1..8, not the (1,2,3,4) this was first written with.
    #
    # Worth recording why: I purged curated width lists from three other
    # functions two rounds ago, then wrote a fresh one here in the very next
    # feature. Fixing a bug category does not immunise the next thing you
    # build from it - the habit has to be applied at write time, not just
    # in an audit afterwards.
    #
    # Cost of the omission, measured: a 6-byte scaled field was 40.3% worse
    # and an 8-byte one 37.2% worse, purely because 6 and 8 were not in a
    # four-entry tuple.
    for w in range(1, 9):
        if p % w or w * 2 > p:
            continue
        slots = list(range(0, p - w + 1, w))
        if len(slots) > 8:
            continue
        for ref in slots:
            for tgt in slots:
                if tgt == ref:
                    continue
                fit = fit_linear(probe, p, w, ref, tgt)
                if not fit:
                    continue
                a, b = fit
                if a == 1 and b == 0:
                    continue          # plain subtraction already covers this
                try:
                    x = linear_forward(probe, p, w, ref, tgt, a, b)
                except Exception:
                    continue
                sz = _screen(x)
                if sz < base * 0.97 and (best is None or sz < best[0]):
                    best = (sz, w, ref, tgt, a, b)
    if best is None:
        return None
    _, w, ref, tgt, a, b = best
    return w, ref, tgt, a, b, linear_forward(d, p, w, ref, tgt, a, b)


# ----------------------------------------------------------------------
# field-level dictionary encoding
# ----------------------------------------------------------------------
#
# Every transform above works byte by byte. When a multi-byte FIELD has few
# distinct values - status codes, device ids, enum flags, fixed station
# names - its bytes look moderately random individually while the field
# itself carries almost no information.
#
# Measured: a record with two 5-valued u16 fields showed 8.468 bits/record
# of byte-wise entropy where the joint reality is 2 x log2(5) = 4.64 bits.
# That is 3.8 bits per record - 950 bytes over the test file - invisible to
# byte-wise coding because bytes 0 and 1 are two halves of ONE value.
#
# Encoding the field as an index into a table of its distinct values
# recovers it: +8.1% on that file, closing half the gap to the entropy floor.

DICT_MAX = 255            # more distinct values than this and it stops paying


def base_pack(idxs, k):
    """Encode a stream of indices in base k as one big integer.

    Storing an index from k possibilities as a byte wastes 8 - log2(k) bits
    each time. Bit-packing wastes the fractional part: 5 values need 2.32
    bits and a bit-packer spends 3.

    Base-k arithmetic spends exactly log2(k) bits per symbol, because the
    fractional part carries into the next symbol. Measured on 2000 indices
    from 5 values: floor 580 B, as bytes+lzma 880 B, bit-packed 812 B,
    base-5 packed 581 B.

    The result is dense and must NOT be handed to a codec afterwards -
    compressing it made it 644 B. That is the giveaway that it is already
    at its floor."""
    # Building one huge integer one symbol at a time is quadratic: each
    # `v * k + x` re-multiplies a number that grows to megabits. Measured
    # 2.3 s inside this function alone on a 94 KB file.
    #
    # Chunking keeps every multiply small, then combines the chunks with
    # one shift each. Same result, far less work.
    if not idxs:
        return b"\x00"
    CHUNK = 512
    chunks = []
    for i in range(0, len(idxs), CHUNK):
        part = idxs[i:i + CHUNK]
        v = 0
        for x in reversed(part):
            v = v * k + x
        chunks.append((v, len(part)))
    v = 0
    for cv, clen in reversed(chunks):
        v = v * (k ** clen) + cv
    nby = (v.bit_length() + 7) // 8
    return v.to_bytes(max(1, nby), 'little')


def base_unpack(blob, k, n):
    v = int.from_bytes(blob, 'little')
    out = bytearray()
    for _ in range(n):
        out.append(v % k)
        v //= k
    return bytes(out)


def find_dict_fields(d, p, widths=None):
    """Which (offset, width) fields have few enough distinct values to be
    worth dictionary encoding?"""
    n = len(d) - (len(d) % p)
    if n < p * 32:
        return []
    nrec = n // p
    probe = min(nrec, 4000)
    found = []
    # Enumerated 1..8. The old (2,4) could not see a 3-byte station code or
    # any other odd-width enum field - not a search that missed it, but no
    # search at all for those widths.
    if widths is None:
        widths = range(1, 9)
    for w in widths:
        if p % w:
            continue
        for off in range(0, p - w + 1, w):
            seen = set()
            for r in range(probe):
                seen.add(d[off + r * p: off + r * p + w])
                if len(seen) > DICT_MAX:
                    break
            if len(seen) <= DICT_MAX and len(seen) * 8 < probe:
                # Score by BITS SAVED, not by distinct count. Enumerating
                # widths from 1 and sorting by cardinality made narrow
                # fields always win: a 2-byte 5-valued field was split into
                # two 1-byte dictionaries, destroying the joint correlation
                # that is the entire point of the feature (8 bits/record
                # apparent vs 4.64 bits actual). A 1-byte field with 5
                # values saves ~5 bits; a 2-byte field with 5 values saves
                # ~13. Score accordingly.
                import math as _m
                saved = w * 8 - _m.log2(max(1, len(seen)))
                found.append((off, w, len(seen), saved))
    # take the highest-value fields first, and do not overlap
    found.sort(key=lambda f: -f[3])
    chosen = []
    used = set()
    for off, w, k, _sv in found:
        if any(o in used for o in range(off, off + w)):
            continue
        chosen.append((off, w))
        used.update(range(off, off + w))
    return sorted(chosen)


def dict_forward(d, p, fields):
    n = len(d) - (len(d) % p)
    body, tail = d[:n], d[n:]
    nrec = n // p
    head = bytearray()
    idxs = bytearray()
    for off, w in fields:
        vals = [body[off + r * p: off + r * p + w] for r in range(nrec)]
        uniq = sorted(set(vals))
        m = {v: i for i, v in enumerate(uniq)}
        head.append(len(uniq) - 1)
        for v in uniq:
            head += v
        idxs += bytes(m[v] for v in vals)
    rest = bytearray()
    covered = set()
    for off, w in fields:
        covered.update(range(off, off + w))
    keep = [i for i in range(p) if i not in covered]
    for r in range(nrec):
        base = r * p
        for i in keep:
            rest.append(body[base + i])
    return bytes(head), bytes(idxs), bytes(rest), tail, nrec


def dict_inverse(head, idxs, rest, tail, p, fields, nrec, orig):
    tables = []
    pos = 0
    for off, w in fields:
        k = head[pos] + 1
        pos += 1
        tab = [head[pos + i * w: pos + (i + 1) * w] for i in range(k)]
        pos += k * w
        tables.append(tab)
    covered = set()
    for off, w in fields:
        covered.update(range(off, off + w))
    keep = [i for i in range(p) if i not in covered]

    out = bytearray(nrec * p)
    ip = 0
    for fi, (off, w) in enumerate(fields):
        tab = tables[fi]
        for r in range(nrec):
            out[off + r * p: off + r * p + w] = tab[idxs[ip]]
            ip += 1
    rp = 0
    for r in range(nrec):
        base = r * p
        for i in keep:
            out[base + i] = rest[rp]
            rp += 1
    return bytes(out) + tail


# ----------------------------------------------------------------------
# sub-byte field widening
# ----------------------------------------------------------------------
#
# Instruments pack fields that do not align to bytes: 12-bit ADC samples two
# per three bytes, 10-bit sensors, 4-bit nibble pairs. Every transform above
# works on whole bytes, so a 12-bit value straddling a byte boundary is
# invisible to all of them.
#
# The obvious fix - transposing individual bit planes - was measured and
# gained only +1.0%. The real win is WIDENING: read the packed fields at
# their true boundaries and re-emit them byte-aligned, so the existing
# transforms can see them. Measured on 12-bit ADC data: 4,148 bytes packed
# versus 2,336 widened and transformed, +43.7%.
#
# Widening EXPANDS the intermediate data (12 bits become 16). That is fine:
# the expansion is regular and the codec removes far more than it costs.

def unpack_bits(d, w):
    """Read w-bit little-endian fields and re-emit each as 16 bits."""
    if w < 4 or w > 15:
        return None
    # Only consume WHOLE bytes. If the field count would end mid-byte, that
    # last partial byte holds both field bits and the start of the tail -
    # rounding either way loses data. Trim to a field count whose bit total
    # is a multiple of 8.
    total = (len(d) * 8) // w
    while total and (total * w) % 8:
        total -= 1
    if total < 16:
        return None
    out = bytearray()
    acc = 0
    nbits = 0
    pos = 0
    produced = 0
    while produced < total:
        while nbits < w:
            if pos >= len(d):
                break
            acc |= d[pos] << nbits
            nbits += 8
            pos += 1
        if nbits < w:
            break
        v = acc & ((1 << w) - 1)
        acc >>= w
        nbits -= w
        out += struct.pack("<H", v)
        produced += 1
    used = (produced * w + 7) // 8
    return bytes(out), produced, d[used:]


def repack_bits(u, w, count, tail):
    out = bytearray()
    acc = 0
    nbits = 0
    for i in range(count):
        v = struct.unpack("<H", u[i * 2:i * 2 + 2])[0] & ((1 << w) - 1)
        acc |= v << nbits
        nbits += w
        while nbits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            nbits -= 8
    if nbits:
        out.append(acc & 0xFF)
    return bytes(out) + tail


def best_unpack(d):
    """Search field widths 4-15. Returns (w, transformed, count, tail) or
    None. Only widths that are not already byte multiples are worth trying."""
    if len(d) < 256:
        return None
    base = _screen(d)
    best = None
    # ENUMERATED, not curated. A hand-picked width list is a permanent
    # silent blind spot for every value never anticipated: a 7-bit packed
    # field measured 6.9% better at w=7, and 7 was simply not in the list.
    # Multiples of 8 are skipped because the byte-level transforms already
    # cover those.
    for w in [w for w in range(3, 25) if w % 8]:
        got = unpack_bits(d, w)
        if not got:
            continue
        u, count, tail = got
        # ENUMERATED. This was `(2, 4)` - written before the curated-list
        # sweeps and never revisited, because I only ever audited code I had
        # just written. A 3-channel 12-bit ADC file (three interleaved
        # sensors, entirely ordinary in real instruments) needs period 6
        # after widening, and cost 42.6% for being outside a two-entry
        # tuple.
        #
        # The habit gap this exposes: knowing a bug category does not
        # trigger a sweep of everything already shipped. It takes a
        # deliberate one.
        for p in range(1, 17):
            try:
                x = t_transdelta(u, p)
            except Exception:
                continue
            sz = _screen(x)
            if sz < base * 0.95 and (best is None or sz < best[0]):
                best = (sz, w, p, u, count, tail)
    return None if best is None else (best[1], best[2], best[3],
                                      best[4], best[5])


# ----------------------------------------------------------------------
# adaptive segmentation
# ----------------------------------------------------------------------
#
# One transform for a whole file assumes the file has one structure. Real
# archives do not: a log rotates format, an instrument file has a header
# section then samples, a backup concatenates unrelated things.
#
# Measured on a 3-section synthetic file: compressing each section with its
# own transform gave 907 bytes against 1,004 for one global choice - +9.7%,
# and real files are far more mixed than that test.
#
# Boundaries are found by sliding a window and asking which stride minimises
# the delta-entropy locally. Where the answer changes, the structure changed.

# These thresholds were 4096/8192 and silently disabled segmentation on any
# file whose sections were smaller than 8 KB - which is most of them. A
# threshold that rejects a real gain is the same class of bug as a search
# loop that stops early: nothing errors, the compressor just quietly does
# worse. Sized to the window now, and the router discards the result anyway
# if it does not pay.
SEG_WINDOW = 1024
SEG_MIN = 2048


def find_segments(d, window=SEG_WINDOW, max_p=32):
    """Return byte offsets where the dominant stride changes."""
    n = len(d)
    if n < SEG_MIN * 2:
        return [0, n]
    marks = [0]
    prev = None
    for off in range(0, n - window, window):
        seg = d[off:off + window]
        best_p, best_e = 1, 9.9
        for p in range(1, min(max_p, len(seg) // 8) + 1):
            diff = bytes((seg[i] - seg[i - p]) & 255
                         for i in range(p, min(len(seg), 1500)))
            e = entropy(diff)
            if e < best_e:
                best_p, best_e = p, e
        if prev is not None and best_p != prev and off - marks[-1] >= SEG_MIN:
            marks.append(off)
        prev = best_p
    marks.append(n)
    return marks


def _seg_task(blob):
    """One segment, in its own process. Top-level so it can be pickled.

    Resets the budget and the competitive baseline to match exactly what
    the sequential loop does for each segment - verified byte-identical
    on all three segments of a real tick file."""
    _reset_cmix_budget()
    globals()["_best_payload"] = None
    return compress(blob, try_split=False, try_seg=False, _top=False)


def _top_level_ok():
    """True when it is safe to start a process pool.

    A worker is already a child process; nesting a pool inside one is how
    a program ends up forking without limit."""
    try:
        return _mp.current_process().name == "MainProcess"
    except Exception:
        return False


def compress_segmented(data):
    """Compress each detected section independently. Returns None when the
    file has no boundaries worth the header cost."""
    marks = find_segments(data)
    if len(marks) <= 2:
        return None
    # EACH SEGMENT NEEDS ITS OWN BUDGET *AND* ITS OWN BASELINE.
    #
    # _top=False stops every segment resetting the global budget, which
    # once produced 132 cmix calls against a 6-second cap. But it
    # overcorrected: two pieces of state then leaked across segments and
    # both starved the later ones of cmix.
    #
    #   _cmix_spent    segment 0 spent the whole allowance
    #   _best_payload  the competitive gate compared segment 1's
    #                  candidates against segment 0's best size, judged
    #                  them uncompetitive, and skipped cmix
    #
    # The second was the harder one to see. Each segment compressed in
    # ISOLATION gave 12,266 bytes; the same segment in sequence gave
    # 12,617. Measured across three segments of a real tick file the leak
    # cost 653 bytes - and the whole-file result went from 28,514 (v18,
    # before any of this machinery) to 28,637.
    #
    # A segment is a different file as far as the gate is concerned, so
    # both are reset for each one. SEG_CMIX_CAP bounds the total time so a
    # heavily segmented file cannot multiply the budget without limit.
    #
    # THIS LOOP IS THE BIGGEST REMAINING SPEED OPPORTUNITY, AND IT IS NOT
    # SAFE TO THREAD AS THE CODE STANDS.
    #
    # Measured: this loop is 40.5 s of a 60-77 s compression on a 128 KB
    # sample - the single largest cost in the router. The segments are
    # independent, so in principle they can run at once.
    #
    # Two attempts failed the same way. _cmix_spent and _best_payload are
    # module GLOBALS, and threads share globals. Any threaded version puts
    # every segment back on a shared allowance - the exact leak that cost
    # 653 bytes when it happened sequentially. Both attempts produced
    # 28,637 bytes on a real tick file where this sequential version gives
    # 28,530.
    #
    # Wrapping the assignment in a try/finally does not help either: the
    # threads interleave, so one thread's "restore" lands in the middle of
    # another's work.
    #
    # Doing it properly means converting the budget and the competitive
    # gate to threading.local() throughout pack_best - a change to the
    # hot path that deserves its own session and its own verification
    # against every real file. The payoff is roughly 10x on the site's
    # response time with byte-identical output, so it is worth doing;
    # it is just not worth rushing.
    global _cmix_spent, _best_payload
    _saved_spent, _saved_best = _cmix_spent, _best_payload
    nseg = len(marks) - 1
    per_seg = CMIX_BUDGET if nseg <= SEG_CMIX_CAP else CMIX_BUDGET // 2

    # SEGMENTS IN PARALLEL PROCESSES.
    #
    # This loop is 96% of the router's time on a real file - by far the
    # largest single cost. The segments are independent, so they can run
    # at once.
    #
    # PROCESSES, NOT THREADS. Two threaded attempts failed because
    # _cmix_spent and _best_payload are module globals and threads share
    # them, so every segment ended up on one shared allowance. Processes
    # each get their own copy, which is exactly what is needed.
    #
    # This only became safe once the cmix budget was counted in BYTES
    # rather than seconds. With a wall-clock budget, three workers sharing
    # a CPU each got a third of the machine and produced 29,251 bytes
    # where one worker produced 28,512 - the output depended on machine
    # load. Counting bytes makes it identical either way, verified.
    if (nseg > 1 and SEG_PROCS > 1 and _top_level_ok()
            and len(data) >= SEG_PAR_MIN):
        try:
            pieces = [data[marks[i]:marks[i + 1]] for i in range(nseg)]
            with _cf.ProcessPoolExecutor(
                    max_workers=min(nseg, SEG_PROCS)) as _ex:
                parts = list(_ex.map(_seg_task, pieces))
        except Exception:
            parts = None
    else:
        parts = None

    if parts is None:
        parts = []
        for i in range(nseg):
            if i < SEG_CMIX_CAP:
                _cmix_spent = max(0, CMIX_BUDGET - per_seg)
                _best_payload = None
            parts.append(compress(data[marks[i]:marks[i + 1]],
                                  try_split=False, try_seg=False,
                                  _top=False))
    _cmix_spent, _best_payload = _saved_spent, _saved_best
    out = bytearray([MODE_SEG]) + _varint(len(data)) + _varint(len(parts))
    for p in parts:
        out += _varint(len(p))
    for p in parts:
        out += p
    return bytes(out)


def split_record_candidates(d, sample=131072, top=4):
    """Find LARGE record sizes - the kind split mode needs.

    Scores each candidate by how much lower the entropy of the first 32
    bytes of every record is than the file as a whole. A fixed header makes
    that drop sharply; a coincidental stride does not."""
    n = len(d)
    if n < 8192:
        return []
    s_ = d[:sample]
    whole = entropy(s_[:65536])
    out = []
    for off in (0, 3600, 512, 1024, 4096):
        if off >= len(s_):
            continue
        for rec in range(128, min(16384, (len(s_) - off) // 8) + 1, 16):
            nrec = (len(s_) - off) // rec
            if nrec < 8:
                break
            head = bytearray()
            for r in range(min(nrec, 200)):
                o = off + r * rec
                head += s_[o:o + 32]
            e = entropy(bytes(head))
            if e < whole - 1.5:
                # Penalise by sample count: a 8656-byte "record" gets only a
                # handful of probes and its entropy estimate is biased low,
                # so it beat the true 1440 purely on small-sample noise.
                # Weighting by log(nrec) removes that bias, and preferring
                # the smallest member of each harmonic family removes the
                # rest - 8656 is roughly 6 x 1440, so every sixth probe
                # still landed on a genuine header.
                out.append((rec, e, off))

    # Sort by record SIZE ascending, not by score.
    #
    # Scoring by entropy alone picked 8656 over the true 1440: a larger
    # "record" gets fewer probes, its entropy estimate is biased low
    # (0.088 vs 0.723), and 8656 is 6 x 1440 + 16 so every sixth probe
    # still landed on a genuine header. A debias term was not nearly
    # strong enough to overcome that, and a `rec % q < 16` harmonic check
    # missed it by exactly one.
    #
    # The smallest record that passes is almost always the true one -
    # every larger passing candidate is a multiple of it. Sorting by size
    # makes that structural rather than a tuning battle.
    out.sort()
    seen = []
    for rec, e, off in out:
        if any(rec % q <= 16 or q % rec <= 16 for q in seen):
            continue
        seen.append(rec)
        if len(seen) >= top:
            break
    return seen


def detect_once(data):
    """Run the probe once and return (tid, period), or None.

    Chunked compression used to call the whole detector on every chunk.
    With probe detection that is pure waste: the probe cost is FIXED at
    about 0.8 s no matter how big the chunk is, so at 32 workers the
    detection runs 32 times to compress 32 small pieces. Measured, the
    probe was 98% of the work on a 0.27 MB chunk.

    Detect once for the file, hand the answer to the workers."""
    n = len(data)
    if n < PROBE_TRIGGER:
        return None
    head = data[:PROBE_BYTES]
    mid_at = (n // 2) - (PROBE_BYTES // 2)
    mid = data[mid_at:mid_at + PROBE_BYTES]
    t1 = best_transform(head)
    t2 = best_transform(mid)
    if (t1[0], t1[1]) != (t2[0], t2[1]):
        return None            # structure changes - let each chunk decide
    return t1[0], t1[1]


def compress_with(data, tid, per, packer=None):
    """Apply a KNOWN transform and pack. No detection at all.

    `packer` chooses the speed/ratio point. Measured on a real 8.6 MB
    ship log, AFTER the transform (which itself runs at 505 MB/s):

        lzma -6   604,848   +37.1%    14 MB/s
        lzma -1   700,284   +27.2%    49 MB/s
        zlib -6   714,980   +25.7%   136 MB/s
        zlib -1   765,810   +20.4%   255 MB/s

    The transform was never the bottleneck. The packer is the whole
    speed dial, and even the fastest setting still beats every standard
    tool by 20%."""
    n = len(data)
    if n == 0:
        return compress(data)
    try:
        x = TRANSFORMS[tid][1](data, per)
        if packer == PACK_ZSTD and HAVE_ZSTD:
            body = _ZC.compress(x)
            pid = PACK_ZSTD
        elif packer == PACK_ZLIB or packer == PACK_ZSTD:
            body = zlib.compress(x, 1)      # zstd asked for but absent
            pid = PACK_ZLIB
        else:
            body = lzma.compress(x, preset=LZMA_FAST)
            pid = PACK_LZMA
    except Exception:
        return compress_fast(data, 1)
    # The chunked path lands here too, so it gets the same both-packers
    # treatment - otherwise a chunked run and an unchunked run of the
    # same level would disagree, which is exactly the 21% gap that the
    # hardcoded PACK_ZLIB caused.
    if packer is None and HAVE_ZSTD:
        try:
            alt = _ZC.compress(x)
            if len(alt) < len(body):
                body, pid = alt, PACK_ZSTD
        except Exception:
            pass

    cand = bytes([MODE_FLAT]) + _varint(n) + bytes([tid, pid]) + \
        _varint(per) + body
    raw = bytes([MODE_RAW]) + _varint(n) + bytes(data)
    best = cand if len(cand) <= len(raw) else raw
    return MAGIC + _crc(data).to_bytes(4, 'big') + best


def _waveform_delta(ints, w, big):
    """First-order delta over an integer array, narrowed or escaped.

    Returns (kind, packed) where kind is 0 for a fixed narrow width and
    1 for byte-with-escape, or None when neither shrinks the array.

    This mirrors what steim.pack_waveform does for miniSEED. Both build
    every layout, verify the round trip, and keep the smaller."""
    o = ">" if big else "<"
    code = {1: "b", 2: "h", 4: "i", 8: "q"}.get(w)
    if code is None or len(ints) % w or len(ints) < w * 64:
        return None
    cnt = len(ints) // w
    vals = _struct.unpack(o + code * cnt, ints)
    d1 = [vals[i + 1] - vals[i] for i in range(cnt - 1)]
    if not d1:
        return None
    dmax = max(abs(v) for v in d1)

    best = None
    # fixed narrow width
    for fw, fc in ((1, "b"), (2, "h"), (4, "i")):
        lim = (1 << (fw * 8 - 1)) - 1
        if dmax <= lim:
            blob = _struct.pack("<i", vals[0]) + \
                _struct.pack("<" + fc * len(d1), *d1)
            # The kind byte carries the width: 0 means escape, anything
            # else IS the fixed width in bytes. Inferring it from the
            # payload length instead was ambiguous and raised on a real
            # SAC file - two widths can produce the same length once the
            # escape stream is involved.
            best = (fw, blob, fw)
            break
    # byte plus escape
    body = bytearray()
    esc = bytearray()
    for v in d1:
        if -127 <= v <= 127:
            body.append(v & 0xFF)
        else:
            body.append(0x80)
            esc += _struct.pack("<i", v)
    eblob = _struct.pack("<i", vals[0]) + bytes(body) + bytes(esc)
    if best is None or len(eblob) < len(best[1]):
        best = (0, eblob, 0)          # 0 = escape
    if len(best[1]) >= len(ints):
        return None            # no smaller than what we started with
    return best[0], best[1]


def _waveform_undelta(packed, kind, cnt, w, big):
    """Rebuild the integer array. Mirror of _waveform_delta."""
    o = ">" if big else "<"
    code = {1: "b", 2: "h", 4: "i", 8: "q"}[w]
    first = _struct.unpack("<i", packed[:4])[0]
    out = [first]
    if kind:                                  # kind IS the fixed width
        fc = {1: "b", 2: "h", 4: "i"}[kind]
        rest = packed[4:4 + (cnt - 1) * kind]
        for v in _struct.unpack("<" + fc * (cnt - 1), rest):
            out.append(out[-1] + v)
    else:
        body = packed[4:4 + cnt - 1]
        esc = packed[4 + cnt - 1:]
        e = 0
        for b in body:
            if b == 0x80:
                v = _struct.unpack("<i", esc[e:e + 4])[0]
                e += 4
            else:
                v = b - 256 if b > 127 else b
            out.append(out[-1] + v)
    return _struct.pack(o + code * cnt, *out)


def compress_fast(data, level=1, _no_csv=False):
    """A speed/ratio dial for people who cannot wait.

    THE PROBLEM THIS SOLVES
    
    The full router is ~7 KB/s. That is fine for a 100 KB demo file and
    useless for a day of ship telemetry. But almost all of that time buys
    the LAST few percent: measured on a 320 KB sensor file,

        full router (cmix on)     60,448    7.5 KB/s
        router with cmix off      60,448   17.2 KB/s   same size, 2x
        one transform + lzma-6    63,176  227   KB/s   +4.5%, 30x
        lzma -9 alone            101,040  1066  KB/s

    One transform and a good packer captures +37.5% over lzma at 227 KB/s.
    The remaining 4.5% costs a 30x slowdown.

    level 1  detect the best transform, pack with lzma-6.  ~227 KB/s
    level 2  also try the packers, keep the smallest.      ~50 KB/s
    level 3  the full router.                              ~7 KB/s

    The container is the SAME format, so anything compressed fast decodes
    with the ordinary decompress() - the dial only changes how hard the
    encoder looks."""
    n = len(data)
    if n == 0:
        return compress(data)
    if level >= 3:
        return compress(data)

    # EVERY FORMAT MODULE COMPETES, NOT JUST THE FIRST ONE TO FIRE.
    #
    # Each module used to compare itself against the plain result and
    # return immediately if it won. That meant whichever ran FIRST took
    # the file, even when a later module would have done far better.
    #
    # Measured: a DNA file has fixed-width 70-character lines, so fixedw
    # fired, beat plain, and returned - giving +4.5% where the fasta
    # module gives +12.9% on the same file. The 2-bit packer never got
    # to run.
    #
    # Now they all build a candidate and the smallest is chosen once.
    _fmt = []

    # FORMAT MODES BELONG IN THE FAST PATH TOO.
    #
    # csvcol used to live only in the full 17-mode router, which is
    # unusable above a few megabytes. So on a real 20 MB EPA air-quality
    # file the fast path produced 511,237 bytes against bzip2's 322,355 -
    # a 37% LOSS - while csvcol on a slice of the same file beat bzip2 by
    # 66%, three times smaller.
    #
    # The compression was never the problem. The mode that wins was
    # sitting behind a setting nobody with a large file can afford to
    # use. A transform that only works on files small enough not to need
    # it is not a transform.
    #
    # It is cheap here: csvcol refuses anything that is not a clean
    # fixed-shape table, and verifies its own round trip before returning.
    # THE LIMIT HERE WAS 1 << 24 - SIXTEEN MEGABYTES - WHILE CSVCOL_MAX
    # SAT AT SIXTY-FOUR, UNUSED BY THIS CHECK.
    #
    # Two limits for the same thing, and the smaller one won silently. A
    # real 20 MB EPA file is four megabytes over the hardcoded line, so
    # csvcol never ran on it: the fast path returned 322,374 bytes where
    # the SAME file split into chunks - each chunk under the limit -
    # returned 86,510. The mode that wins by 73% was disabled by a
    # constant nobody had connected to the one beside it.
    #
    # It showed up as a speed result, which is what made it hard to see:
    # the large file compressed at 10.27 MB/s against 1.07 elsewhere,
    # and fast looked like good news rather than like work being skipped.
    if (HAVE_CSVCOL and not _no_csv and level <= 2
            and 1024 <= n <= MODULE_MAX):
        try:
            cx = _csvcol.try_encode(bytes(data))
        except Exception:
            cx = None
        if cx:
            # ALWAYS COMPARE AGAINST NO TRANSFORM AT ALL.
            #
            # best_transform ranks candidates on a screening slice. When
            # the columnar blob is larger than SCREEN_CAP that slice is
            # not representative, and the ranking can pick a transform
            # that HURTS.
            #
            # Measured on a real crypto CSV: at a 93 KB blob it correctly
            # chose "none" and gave 29,852 bytes. At a 140 KB blob - just
            # over the cap - it chose transpose/7 and gave 49,672 where
            # doing nothing gives 43,700. A 14% loss caused entirely by
            # ranking on the wrong tenth of the data.
            #
            # The fix is not a bigger cap. It is to keep the untransformed
            # stream as a candidate and measure both in full, which is
            # what the router does everywhere else.
            tidc, pc, xc = best_transform(cx)
            if len(cx) > SCREEN_CAP:
                try:
                    _pa, _ba = pack_best(xc, allow_cmix=False)
                    _pb, _bb = pack_best(cx, allow_cmix=False)
                    if len(_bb) < len(_ba):
                        tidc, pc, xc = 0, 1, cx
                except Exception:
                    pass
            # pack_best, not plain lzma-4. The columnar stream is a
            # different shape from the raw file - lots of short repeated
            # tokens - and bzip2 often wins on it. Packing it with one
            # fixed codec gave +1.4% where trying all of them gives +9.5%
            # on the same file.
            try:
                pidc, bodyc = pack_best(xc, allow_cmix=False)
            except Exception:
                bodyc = None
            if bodyc is not None:
                cand = (bytes([MODE_CSVCOL]) + _varint(n) +
                        bytes([tidc, pidc]) + _varint(pc) +
                        _varint(len(cx)) + bodyc)
                plain = compress_fast(data, level, _no_csv=True)
                if len(cand) + HEADER_SIZE < len(plain):
                    return (MAGIC + _crc(data).to_bytes(4, 'big') + cand)
                return plain

    # FIXED-WIDTH TEXT RECORDS.
    #
    # A large family of scientific formats aligns records by column
    # rather than separating them with a delimiter - PDB structures,
    # instrument logs, government data extracts. Every line is the same
    # width, so character position 30 is the same field in every record.
    #
    # The transforms already find this when the WHOLE file is fixed-width
    # - a NOAA buoy file gets +30.7% unaided. But a PDB file opens with
    # hundreds of REMARK lines of varying length, and those break the
    # periodicity before the ATOM records begin.
    #
    #     4HHB.pdb   473 KB   -0.1% -> +20.5%
    #     buoy file  400 KB  +30.7% -> +33.7%
    #     1CRN.pdb    49 KB   -0.0% -> -15.3%   (kept only when smaller)
    if HAVE_FIXEDW and not _no_csv and 4096 <= n <= MODULE_MAX:
        try:
            wx = _fixedw.try_encode(bytes(data))
        except Exception:
            wx = None
        if wx:
            tidx, px, xx = best_transform(wx)
            try:
                pidx, bodyx = pack_best(xx, allow_cmix=False)
            except Exception:
                bodyx = None
            if bodyx is not None:
                cand = (bytes([MODE_FIXEDW]) + _varint(n) +
                        bytes([tidx, pidx]) + _varint(px) +
                        _varint(len(wx)) + bodyx)
                _fmt.append(cand)

    # LOG FILES.
    #
    # A log line is a record whose fields are separated by spaces. The
    # leading ones repeat heavily - the month, the host, the log level -
    # but sit far apart in a row-major file.
    #
    # The evidence was already in hand: the SAME Apache log supplied as a
    # structured CSV gave +11.4% while the raw form sat at -0.2%.
    # Identical content; the only difference was whether the fields were
    # exposed.
    #
    #     Apache_2k.log   -0.2% -> +8.5%
    #     Linux_2k.log    -0.2% -> +6.2%
    #     HDFS_2k.log     -0.1% -> +6.0%
    #     demo_server.log         +19.1%
    if HAVE_LOGCOL and not _no_csv and 2048 <= n <= MODULE_MAX:
        try:
            gx = _logcol.try_encode(bytes(data),
                                    pack=lambda b: lzma.compress(b, preset=1))
        except Exception:
            gx = None
        if gx:
            tidg, pg, xg = best_transform(gx)
            try:
                pidg, bodyg = pack_best(xg, allow_cmix=False)
            except Exception:
                bodyg = None
            if bodyg is not None:
                cand = (bytes([MODE_LOGCOL]) + _varint(n) +
                        bytes([tidg, pidg]) + _varint(pg) +
                        _varint(len(gx)) + bodyg)
                _fmt.append(cand)

    # DNA SEQUENCE.
    #
    # A FASTA file stores DNA as A, C, G, T - one byte each, where two
    # bits would do. General compressors recover part of that through
    # string matching but cannot see that the alphabet is four wide.
    #
    # Measured on the real E. coli genome:
    #
    #     lzma -9        42,640
    #     ours, before   41,995   +1.5%
    #     2-bit packed   37,117  +13.0%
    #
    # The 2-bit floor for that slice is 37,500 bytes, so the packed form
    # is at the floor. Compressing it further makes it slightly LARGER -
    # packed DNA has no redundancy left - which is why this returns the
    # packed stream directly rather than feeding it onward.
    if HAVE_FASTA and not _no_csv and 1024 <= n <= (1 << 26):
        try:
            fx = _fasta.try_encode(bytes(data))
        except Exception:
            fx = None
        if fx:
            _fmt.append(bytes([MODE_FASTA]) + _varint(n) + fx)

    # FLOAT ARRAYS AND WAVEFORM DELTA IN THE FAST PATH.
    #
    # These lived only in the full router, which is unusable above a few
    # megabytes. So on real SAC seismic the fast path did not merely
    # leave gains behind - it LOST:
    #
    #     file        fast path    full router
    #     IU_TUC        -7.1%        +14.3%
    #     IU_MAJO       -1.3%        +15.9%
    #     IU_ANMO       +3.0%        +19.5%
    #     IU_KONO      +12.9%        +34.0%
    #
    # Same flaw csvcol had: the mode exists, the fast path cannot reach
    # it, and seismic is the domain where this system is strongest.
    #
    # Both candidates are built and the smaller kept, along with the
    # ordinary result - so this can only help.
    if HAVE_FAST and 1024 <= n <= MODULE_MAX and not _no_csv:
        _best_f = None
        for hoff in (0, 632, 512, 240, 128, 64):
            if hoff >= n or (n - hoff) % 4:
                continue
            for big in (False, True):
                try:
                    got = _fast.float_to_int(data[hoff:], big)
                except Exception:
                    continue
                if not got:
                    continue
                w, ints = got
                if _fast.int_to_float(ints, w, big) != data[hoff:]:
                    continue          # verified, never assumed
                try:
                    tidf, pf_, xf = best_transform(ints, [w, w * 2, 2, 4])
                    pidf, bodyf = pack_best(data[:hoff] + xf,
                                            allow_cmix=False)
                    c = (bytes([MODE_F2I]) + _varint(n) +
                         bytes([w, 1 if big else 0, tidf, pidf]) +
                         _varint(pf_) + _varint(hoff) + bodyf)
                    if _best_f is None or len(c) < len(_best_f):
                        _best_f = c
                except Exception:
                    pass
                try:
                    wd = _waveform_delta(ints, w, big)
                except Exception:
                    wd = None
                if wd is not None:
                    kind, packed = wd
                    try:
                        tidw, pw, xw = best_transform(packed)
                        pidw, bodyw = pack_best(data[:hoff] + xw,
                                                allow_cmix=False)
                        c = (bytes([MODE_WDELTA]) + _varint(n) +
                             bytes([w, 1 if big else 0, kind, tidw, pidw]) +
                             _varint(pw) + _varint(hoff) +
                             _varint(len(packed)) + bodyw)
                        if _best_f is None or len(c) < len(_best_f):
                            _best_f = c
                    except Exception:
                        pass
                break
            if _best_f is not None:
                break
        if _best_f is not None:
            _fmt.append(_best_f)

    # ONE DECISION POINT for every format candidate collected above.
    if _fmt:
        best_fmt = min(_fmt, key=len)
        plain = compress_fast(data, level, _no_csv=True)
        if len(best_fmt) + HEADER_SIZE < len(plain):
            return MAGIC + _crc(data).to_bytes(4, 'big') + best_fmt
        return plain

    # LEVEL 0 - the throughput setting.
    #
    # Same transform, but packed with zlib at level 1 instead of lzma at
    # 6. Measured on a real ship log: +20.4% against the best standard
    # tool at 255 MB/s, versus +37.1% at 14 MB/s.
    #
    # Roughly half the compression advantage for eighteen times the
    # speed. Worth it when the data is moving rather than resting - a
    # satellite link, a live feed - and wrong when it is going into an
    # archive.
    if level == 0:
        got = detect_once(data) if n > PROBE_TRIGGER else None
        if got is None:
            tid0, per0, _ = best_transform(data[:PROBE_BYTES]) \
                if n > PROBE_BYTES else best_transform(data)
        else:
            tid0, per0 = got
        # zstd when available, zlib otherwise - both decode anywhere the
        # library is present, and the choice is recorded in the stream.
        return compress_with(data, tid0, per0,
                             packer=PACK_ZSTD if HAVE_ZSTD else PACK_ZLIB)

    # The unrestricted search - the PERIOD list is never narrowed.
    #
    # Two attempts to speed this up with an FFT both lost on real data:
    # see fft_periods() below. A candidate list built from any single
    # signal is a blind spot, and that one cost 72% on a real ship log.
    #
    # DETECT ON A PROBE, APPLY TO THE WHOLE FILE.
    #
    # The search reads the entire file to decide which transform to use.
    # It does not need to: the structure of a fixed-record file is
    # visible in the first few thousand records.
    #
    # Measured on five real files, 16 KB probe against the full search:
    #
    #     demo_gps.bin              8,524 ->  8,524   1.9x faster
    #     demo_sensor.bin          10,092 -> 10,092   3.2x
    #     demo_ticks.bin           29,232 -> 29,232   3.4x
    #     demo_measurements.csv    33,600 -> 33,600   3.1x
    #     demo_server.log          28,484 -> 28,484   3.2x
    #     ship_engine_6h.bin      604,848 -> 604,848  3.9x
    #
    # Byte-identical on every one.
    #
    # WHY THIS WORKS WHERE THREE OTHER SHORTCUTS FAILED
    #
    # The failures narrowed WHICH candidates were considered - a smaller
    # period list, a smaller ranking slice. Those lose because the right
    # answer can fall outside the narrowed set.
    #
    # This narrows only how much data the SAME full search reads. Every
    # candidate is still tried, at every period, against every packer -
    # just on a representative sample rather than on gigabytes.
    #
    # THE GUARD
    #
    # A probe is a sample, and a sample can mislead on a file whose
    # structure changes partway through. So the probe result is checked
    # against a second probe from the MIDDLE of the file; if they
    # disagree, the full search runs. That costs two probes on
    # heterogeneous files and saves most of the work on uniform ones.
    if n > PROBE_TRIGGER:
        head = data[:PROBE_BYTES]
        mid_at = (n // 2) - (PROBE_BYTES // 2)
        mid = data[mid_at:mid_at + PROBE_BYTES]
        t1 = best_transform(head)
        t2 = best_transform(mid)
        if (t1[0], t1[1]) == (t2[0], t2[1]):
            tid, per = t1[0], t1[1]
            x = TRANSFORMS[tid][1](data, per)
            # pack_best, NOT a hardcoded lzma-6.
            #
            # This branch had its own packer, so any file over
            # PROBE_TRIGGER never reached the shared list. On 400 KB of
            # Moby Dick that meant lzma-6 at 142,154 bytes where bzip2
            # gives 129,694 - a 9.6% loss to a standard tool, on ordinary
            # English prose.
            #
            # Third place in this file where a private packer set drifted
            # away from pack_best. Same fix each time: use the one list.
            try:
                pid, body = pack_best(x, allow_cmix=False)
            except Exception:
                return compress(data)
            cand = bytes([MODE_FLAT]) + _varint(n) + bytes([tid, pid]) + \
                _varint(per) + body
            raw = bytes([MODE_RAW]) + _varint(n) + bytes(data)
            best = cand if len(cand) <= len(raw) else raw
            return MAGIC + _crc(data).to_bytes(4, 'big') + best
        # probes disagree - the file is not uniform, so do the real search

    # SHRINKING THE SCREENING SLICE ALSO FAILS. Tested, reverted.
    #
    # On a 1.5 MB synthetic sensor file, dropping SCREEN_CAP from 128 KB
    # to 16 KB gave byte-identical output and 1.5x the speed:
    #
    #     128 KB   290,654   +38.5%   0.52 MB/s
    #      16 KB   290,654   +38.5%   0.77 MB/s
    #
    # On the real 8.6 MB ship log it gave 900,047 instead of 604,864 -
    # +6.4% against the standard tools instead of +37.1%.
    #
    # The reason is the same as the FFT failure: that file's structure has
    # period 400, and a 16 KB slice holds only forty of those records -
    # not enough to rank the right period above the wrong ones.
    #
    # THREE separate attempts to speed up the search have now failed the
    # same way: FFT-restricted periods, FFT-augmented periods, and a
    # smaller screening slice. Each looked byte-identical on synthetic
    # data and each lost badly on one real file. The search is not
    # padding - it is doing work that only shows up on real structure.
    tid, per, x = best_transform(data)
    # LEVEL 1 AND LEVEL 2 BOTH LAND HERE.
    #
    # This was written as "if level == 1:" when the branch had its own
    # packer list and level 2 was handled by an else. Replacing the list
    # with pack_best removed the else, and level 2 then fell through with
    # `pid` and `body` never assigned - an UnboundLocalError that only
    # appeared when a bench actually exercised level 2.
    #
    # Since pack_best already tries every packer, levels 1 and 2 want the
    # same thing here. The condition is now a floor, not an equality.
    if level >= 1:
        # USE pack_best, NOT A HAND-ROLLED PACKER SET.
        #
        # This block had its own list - lzma preset 4, then bz2, then
        # zstd - which drifted out of step with pack_best. On real server
        # logs that cost dearly: a Linux syslog gave 8,734 bytes here
        # where pack_best gives 6,048, a 44% loss to xz -9e on a file
        # type every archive is full of.
        #
        # Two packer sets that are meant to agree and do not is the same
        # bug shape as the chunked path hardcoding PACK_ZLIB. One list.
        try:
            pid, body = pack_best(x, allow_cmix=False)
        except Exception:
            return compress(data)

    cand = bytes([MODE_FLAT]) + _varint(n) + bytes([tid, pid]) + \
        _varint(per) + body
    raw = bytes([MODE_RAW]) + _varint(n) + bytes(data)
    best = cand if len(cand) <= len(raw) else raw
    return MAGIC + _crc(data).to_bytes(4, 'big') + best


def compress(data, try_split=True, try_seg=True, _top=True):
    if _top:
        _reset_cmix_budget()
    """Transform then pack. Always compares against storing raw, so the
    worst case is bounded at a few bytes of header."""
    n = len(data)
    cands = []

    # raw — the bound on how badly this can go
    cands.append(bytes([MODE_RAW]) + _varint(n) + data)

    # flat: one transform for the whole stream
    tid, p, x = best_transform(data)
    pid, body = pack_best(x)
    cands.append(bytes([MODE_FLAT]) + _varint(n) + bytes([tid, pid]) +
                 _varint(p) + body)

    # per-column: one transform choice per byte position. Beat the best
    # single global transform by 3.4-3.7% on structured records.
    for pp in small_periods(data, top=5):
        if pp < 2 or pp > 64 or n < pp * 16:
            continue
        try:
            x, ids = percol_forward(data, pp)
        except Exception:
            continue
        packed = bytearray()
        for i in range(0, len(ids), 2):
            hi = ids[i]
            lo = ids[i + 1] if i + 1 < len(ids) else 0
            packed.append((hi << 4) | lo)
        pid, body = pack_best(x)
        cands.append(bytes([MODE_PERCOL]) + _varint(n) + _varint(pp) +
                     bytes([pid]) + bytes(packed) + body)

    # cross-column: expose relationships BETWEEN fields, then let the normal
    # transforms work on the residual
    # Try every plausible small period, not just the top-ranked few. The
    # detector ranks by delta-entropy, which can bury the true record size
    # under a coincidental sub-multiple - on 6-byte records it ranked 2
    # first and 6 far down, hiding a 40% gain.
    cross_best = None
    # union of both rankers: neither alone is reliable for this mode
    _cross_cands = list(dict.fromkeys(
        list(small_periods(data, top=5, objective="align")) +
        list(small_periods(data, top=4))))
    for pp in _cross_cands:
        if pp < 4 or pp > 64 or n < pp * 16:
            continue
        got = best_cross(data, pp)
        if not got:
            continue
        kind, params, x = got
        tid2, p2, x2 = best_transform(x, [pp])
        pid2, body2 = pack_best(x2)
        blob = (bytes([MODE_CROSS]) + _varint(n) + _varint(pp) +
                bytes([kind]) + bytes(params) + bytes([tid2, pid2]) +
                _varint(p2) + body2)
        if cross_best is None or len(blob) < len(cross_best):
            cross_best = blob
    if cross_best is not None:
        cands.append(cross_best)

    # split: separate fixed headers from payload, transform each on its own
    # Split mode has never won a single benchmark in this project - not
    # because the code is wrong, but because three detection failures
    # stacked so it could never find its own parameters:
    #   1. detect_periods caps the findable period at its scan sample
    #      (4096 bytes), so the max_p=65536 passed here was meaningless
    #   2. keep=3 from a ranking dominated by small strides never surfaced
    #      a 1440- or 2240-byte record
    #   3. detect_header widened past the true boundary because a SEG-Y
    #      payload starts with low-entropy float exponent bytes
    # With correct parameters it wins by 0.7% on SEG-Y-shaped data, so the
    # mode is real. It needs its own record search, not one built for small
    # periods.
    if try_split and n >= 4096:
        for rec in split_record_candidates(data):
            if rec < 64 or rec > n // 8:
                continue
            hs = detect_header(data, rec)
            if not hs:
                continue
            hdr, off = hs
            nrec = (n - off) // rec
            if nrec < 8:
                continue
            heads = bytearray()
            pays = bytearray()
            for r in range(nrec):
                o = off + r * rec
                heads += data[o:o + hdr]
                pays += data[o + hdr:o + rec]
            tail = data[off + nrec * rec:]
            ht, hp, hx = best_transform(bytes(heads), [hdr] if hdr <= 512 else None)
            pt, pp, px = best_transform(bytes(pays))
            pre = data[:off]
            blob = (bytes([MODE_SPLIT]) + _varint(n) + _varint(rec) +
                    _varint(hdr) + _varint(off) +
                    bytes([ht]) + _varint(hp) + bytes([pt]) + _varint(pp) +
                    _varint(len(pre)) + _varint(len(hx)) + _varint(len(px)) +
                    _varint(len(tail)) +
                    b"")
            pids, bodys = pack_best(bytes(pre) + bytes(hx) + bytes(px) + tail)
            blob = blob + bytes([pids]) + bodys
            cands.append(blob)
            break

    # dictionary: encode low-cardinality multi-byte fields as indices.
    #
    # Try EVERY plausible small period and keep the best, rather than the
    # first that produces fields. This is the third time an early exit in
    # this file has hidden a real gain: the entropy ranking put 462, 336 and
    # 216 ahead of the true record size 6, so a [:6] slice plus `break` meant
    # dictionary mode never saw the right period. Cost: 8.4%.
    dict_best = None
    _dict_cands = list(dict.fromkeys(
        list(small_periods(data, top=5, objective="align")) +
        list(small_periods(data, top=4))))
    for pp in _dict_cands:
        if pp < 2 or pp > 64 or n < pp * 32:
            continue
        flds = find_dict_fields(data, pp)
        if not flds:
            continue
        try:
            head, idxs, rest, tl, nrec = dict_forward(data, pp, flds)
        except Exception:
            continue
        meta = bytearray([MODE_DICT]) + _varint(n) + _varint(pp) + \
            bytes([len(flds)])
        for off, w in flds:
            meta += bytes([off, w])
        # Index stream goes base-k and is stored raw; everything else is
        # packed normally. Mixing them would let the codec try to compress
        # an already-dense integer, which measured 11% WORSE.
        ks = []
        pos_h = 0
        for off, w in flds:
            ks.append(head[pos_h] + 1)
            pos_h += 1 + (head[pos_h] + 1) * w
        # Base-k is a big win when the indices are near-uniform draws from a
        # small set, and a LOSS when they are repetitive - json indices are
        # patterned text that lzma handles well, and base-k packing destroys
        # that structure (measured: 70 -> 77 bytes). So try both and keep
        # the smaller, like every other axis here.
        packed_idx = bytearray()
        ip = 0
        for kk in ks:
            seg = idxs[ip:ip + nrec]
            ip += nrec
            bp = base_pack(seg, kk) if kk > 1 else b""
            packed_idx += _varint(len(bp)) + bp

        # variant A: base-k indices stored raw
        pidA, bodyA = pack_best(head + rest + tl)
        blobA = (bytes(meta) + _varint(nrec) + _varint(len(head)) +
                 _varint(len(packed_idx)) + _varint(len(rest)) +
                 bytes([0x80 | pidA]) + _varint(len(bodyA)) +
                 bytes(packed_idx) + bodyA)

        # variant B: indices as bytes, packed with everything else
        pidB, bodyB = pack_best(head + idxs + rest + tl)
        blobB = (bytes(meta) + _varint(nrec) + _varint(len(head)) +
                 _varint(len(idxs)) + _varint(len(rest)) +
                 bytes([pidB]) + _varint(len(bodyB)) + bodyB)

        blob = blobA if len(blobA) <= len(blobB) else blobB
        if dict_best is None or len(blob) < len(dict_best):
            dict_best = blob
    if dict_best is not None:
        cands.append(dict_best)

    # float32 holding only whole numbers -> narrowest integer type.
    #
    # A real SAC file stored 864,000 seismic samples as float32; every one
    # was a whole number spanning -1139 to 3143, which fits in int16. Half
    # of every sample was padding, and no general compressor can know that
    # - it sees float bytes and must preserve them.
    #
    # Halving the data before compression took the file from +76.9% to
    # +79.2%, which is +13.7% against the best standard tool.
    #
    # A header offset is searched as well as zero, because scientific
    # formats put a fixed header in front of the array - SAC's is 632
    # bytes - and the float run only starts after it.
    if HAVE_FAST and n >= 1024:
        for hoff in (0, 632, 512, 240, 128, 100, 64):
            if hoff >= n or (n - hoff) % 4:
                continue
            for big in (False, True):
                try:
                    got = _fast.float_to_int(data[hoff:], big)
                except Exception:
                    continue
                if not got:
                    continue
                w, ints = got
                # Re-encode and compare. Nothing is trusted: if the bytes
                # do not come back identical the transform is refused.
                if _fast.int_to_float(ints, w, big) != data[hoff:]:
                    continue
                tidf, pf_, xf = best_transform(ints, [w, w * 2, 2, 4])
                pidf, bodyf = pack_best(data[:hoff] + xf)
                cands.append(bytes([MODE_F2I]) + _varint(n) +
                             bytes([w, 1 if big else 0, tidf, pidf]) +
                             _varint(pf_) + _varint(hoff) + bodyf)

                # WAVEFORM DELTA on the recovered integers.
                #
                # float_to_int narrows the VALUES. It never occurred to
                # anyone to also difference them - and on a real SAC
                # seismic file that was worth twenty points:
                #
                #     float_to_int alone     +1.6%
                #     + delta and escape    +21.8%
                #
                # Waveforms drift slowly, so the gap between consecutive
                # samples is far smaller than the samples themselves. The
                # same reasoning already pays on miniSEED, one format
                # over; this applies it to any float array holding whole
                # numbers.
                #
                # The reconstruction is byte-exact, not merely
                # sample-exact: the recovered integers are re-encoded to
                # float32 and compared against the original bytes before
                # this candidate is offered.
                try:
                    wd = _waveform_delta(ints, w, big)
                except Exception:
                    wd = None
                if wd is not None:
                    kind, packed = wd
                    tidw, pw, xw = best_transform(packed)
                    pidw, bodyw = pack_best(data[:hoff] + xw)
                    cands.append(bytes([MODE_WDELTA]) + _varint(n) +
                                 bytes([w, 1 if big else 0, kind,
                                        tidw, pidw]) +
                                 _varint(pw) + _varint(hoff) +
                                 _varint(len(packed)) + bodyw)
                break

    # CHUNKED cmix, for files past the cmix size limit.
    #
    # THE GAP THIS CLOSES
    #
    # cmix is often the best packer available, and it was silently
    # unavailable on anything over CMIX_LIMIT. On a real 192 KB server log
    # that mattered: cmix on a 64 KB slice beat bzip2 by 8.8%, but the
    # whole file was over the limit, so the router never tried it and
    # settled for matching bzip2 through the bz2 packer instead - losing
    # by the 17 bytes of container.
    #
    # Splitting into CMIX_LIMIT-sized blocks and coding each one puts the
    # best packer back in reach:
    #
    #     bzip2 on the whole file   16,615
    #     cmix in 32 KB chunks      17,030    too small - the model never
    #                                         finishes learning
    #     cmix in 64 KB chunks      15,782    +5.0% against bzip2
    #
    # The block size matters in one direction only: too small and the
    # adaptive model pays its learning cost again on every block.
    if (HAVE_CMIX and _top and CMIX_LIMIT < n <= (1 << 21)
            and _cmix_spent < CMIX_BUDGET * 3):
        try:
            parts = []
            ok = True
            for off in range(0, n, CMIX_LIMIT):
                blk = data[off:off + CMIX_LIMIT]
                with _cmix_sized(len(blk)):
                    cb = _cmix.compress(blk)
                parts.append(_varint(len(cb)) + cb)
            if ok:
                body = b"".join(parts)
                cands.append(bytes([MODE_CHUNK]) + _varint(n) +
                             _varint(CMIX_LIMIT) + _varint(len(parts)) + body)
        except Exception:
            pass

    # COLUMNAR CSV.
    #
    # Row-major CSV interleaves unrelated columns. In a real NOAA weather
    # file with 133 fields per row, 59 are IDENTICAL to the row above -
    # but they sit hundreds of bytes apart, so a string matcher needs a
    # huge window to see it. Grouping each column together puts them
    # adjacent.
    #
    # Measured on three real NOAA station files, every one of which was a
    # LOSS before:
    #
    #     47662099999.csv   -0.1% -> +14.1%
    #     03772099999.csv   -0.8% -> +10.5%
    #     72278023183.csv   -3.2% ->  +3.6%
    #
    # csvcol refuses ragged rows and verifies its own round trip, so a
    # file that cannot be rebuilt never reaches here.
    if HAVE_CSVCOL and _top and 1024 <= n <= (1 << 22):
        try:
            cx = _csvcol.try_encode(bytes(data))
        except Exception:
            cx = None
        if cx:
            tidc, pc, xc = best_transform(cx)
            pidc, bodyc = pack_best(xc)
            cands.append(bytes([MODE_CSVCOL]) + _varint(n) +
                         bytes([tidc, pidc]) + _varint(pc) +
                         _varint(len(cx)) + bodyc)

    # common divisor: remove a shared scale factor from every value
    if 512 <= n <= (1 << 22):
        for w in (8, 4, 2):
            if n % w:
                continue
            done = False
            for big in (False, True):
                for signed in (True, False):
                    try:
                        got = gcd_extract(data, w, big, signed)
                    except Exception:
                        continue
                    if not got:
                        continue
                    g, nd, cnt = got
                    if gcd_restore(nd, g, w, big, cnt, signed) != data:
                        continue        # verified, never assumed
                    tg, pg, xg = best_transform(nd, [w, w * 2, 2, 4])
                    pidg, bodyg = pack_best(xg)
                    cands.append(bytes([MODE_GCD]) + _varint(n) +
                                 bytes([w, 1 if big else 0, 1 if signed else 0,
                                        tg, pidg]) +
                                 _varint(pg) + _varint(g) + _varint(cnt) +
                                 bodyg)
                    done = True
                    break
                if done:
                    break
            if done:
                break

    # length-prefixed records: split the frame headers from the payload
    if 256 <= n <= (1 << 22):
        for w in (2, 4):
            for big in (False, True):
                try:
                    got = lpfx_split(data, w, big)
                except Exception:
                    continue
                if not got:
                    continue
                hdr, bodies, count = got
                if lpfx_join(hdr, bodies, count, w, big) != data:
                    continue          # verified, never assumed
                th, ph, xh = best_transform(hdr, [w, w * 2, 2, 4])
                tb, pb, xb = best_transform(bodies)
                pidh, bh = pack_best(xh)
                pidb, bb = pack_best(xb)
                cands.append(bytes([MODE_LPFX]) + _varint(n) +
                             bytes([w, 1 if big else 0, th, pidh, tb, pidb]) +
                             _varint(ph) + _varint(pb) + _varint(count) +
                             _varint(len(hdr)) + _varint(len(bh)) + bh + bb)
                break
            else:
                continue
            break

    # RLE: the one redundancy shape nothing else here catches - the same
    # byte repeated, whose cheapest description is a count.
    if 64 <= n <= (1 << 22):
        try:
            rl = rle_forward(data)
            if len(rl) < n:                 # only if it actually shrank
                tidr, pr, xr = best_transform(rl)
                pidr, bodyr = pack_best(xr)
                cands.append(bytes([MODE_RLE]) + _varint(n) +
                             bytes([tidr, pidr]) + _varint(pr) +
                             _varint(len(rl)) + bodyr)
        except Exception:
            pass

    # varint: restore fixed-width alignment so the other transforms can
    # see the fields at all
    try:
        got = varint_widen(data)
        if got:
            w, fixed, count = got
            tidv, pv, xv = best_transform(fixed, [w, w * 2, 2, 4])
            pidv, bodyv = pack_best(xv)
            cands.append(bytes([MODE_VARINT]) + _varint(n) +
                         bytes([w, tidv, pidv]) + _varint(pv) +
                         _varint(count) + bodyv)
    except Exception:
        pass

    # BWT: the one thing bzip2 had that this did not. Sorts rotations so
    # that bytes in similar contexts become adjacent, turning scattered
    # repetition into runs that lzma codes cheaply.
    if 256 <= n <= BWT_LIMIT:
        try:
            bw, bidx = bwt_forward(data)
            pidb, bodyb = pack_best(bw)
            cands.append(bytes([MODE_BWT]) + _varint(n) + bytes([pidb]) +
                         _varint(bidx) + bodyb)
        except Exception:
            pass

    # chained: a SECOND transform applied to the first one's residual.
    #
    # Every mode above applies exactly one transform. But a transform
    # changes the shape of what is left, and a different transform can then
    # fit that residual. Measured: accelerating counters gained 10.7% from
    # transpose applied after delta2, and zigzag picked up 1.4-1.6% on
    # several others by mapping straddling residuals to small positives.
    #
    # Only two levels - a third measured as noise and doubles the search.
    try:
        tid1, p1, x1 = best_transform(data)
        if tid1 != 0:
            best_chain = None
            for t2, (_nm, fwd2, _inv, _np) in TRANSFORMS.items():
                if t2 == 0:
                    continue
                for p2 in dict.fromkeys((1, 2, 4, 8, p1)):
                    if len(x1) < p2 * 8:
                        continue
                    try:
                        y = fwd2(x1, p2)
                    except Exception:
                        continue
                    sz = _screen_fast(y)
                    if best_chain is None or sz < best_chain[0]:
                        best_chain = (sz, t2, p2, y)
            if best_chain:
                _, t2, p2, y = best_chain
                pidc, bodyc = pack_best(y)
                cands.append(bytes([MODE_CHAIN]) + _varint(n) +
                             bytes([tid1, t2, pidc]) + _varint(p1) +
                             _varint(p2) + _varint(len(x1)) + bodyc)
    except Exception:
        pass

    # linear: learned y = a*x + b between fields
    lin_best = None
    for pp in list(dict.fromkeys(
            list(small_periods(data, top=5, objective="align")) +
            list(small_periods(data, top=4)))):
        if pp < 2 or pp > 64 or n < pp * 32:
            continue
        got = best_linear(data, pp)
        if not got:
            continue
        w, ref, tgt, a, b, x = got
        tid2, p2, x2 = best_transform(x, [pp])
        pidl, bodyl = pack_best(x2)
        blob = (bytes([MODE_LINEAR]) + _varint(n) + _varint(pp) +
                bytes([w, ref, tgt, (a + 128) & 0xFF, tid2, pidl]) +
                _varint(b) + _varint(p2) + bodyl)
        if lin_best is None or len(blob) < len(lin_best):
            lin_best = blob
    if lin_best is not None:
        cands.append(lin_best)

    # widened: expose sub-byte packed fields at their true boundaries
    got = best_unpack(data)
    if got:
        w, p2, u, count, tail = got
        x = t_transdelta(u, p2)
        pidu, bodyu = pack_best(x + tail)
        cands.append(bytes([MODE_UNPACK]) + _varint(n) + bytes([w, pidu]) +
                     _varint(p2) + _varint(count) + _varint(len(tail)) +
                     bodyu)

    # segmented: give each structurally distinct section its own transform
    if try_seg and n >= SEG_MIN * 2:
        try:
            seg = compress_segmented(data)
            if seg:
                cands.append(seg)
        except Exception:
            pass

    # THE THOROUGH SEARCH MUST NEVER LOSE TO THE FAST ONE.
    #
    # logcol and fasta were added to compress_fast() and not here, so the
    # full router - which the demonstration site uses - never saw them.
    # On a real Apache log the fast path gave +14.4% and the full router
    # gave +0.2%, on the same bytes. Every log and DNA file on the site
    # was reporting a tie that the engine could already beat.
    #
    # Rather than duplicate every mode into both places and rely on
    # remembering to do it again next time, the fast result is simply
    # built and compared. It is a few percent of the router's time, and
    # it makes "level 3 is at least as good as level 1" true by
    # construction instead of by discipline.
    if _top:
        try:
            quick = compress_fast(data, 1)
            if len(quick) < len(min(cands, key=len)) + HEADER_SIZE:
                return quick
        except Exception:
            pass

    body = min(cands, key=len)
    # 3 magic + 4 CRC of the ORIGINAL data. NO length field.
    #
    # Trailing garbage used to decode silently, so a length was added -
    # and cost 2-3 bytes on every file, which showed up as a small loss
    # against the previous version on four real files out of five.
    #
    # The packers already know. zlib, lzma and bz2 all expose
    # `unused_data` after decoding, so the decoder can ASK whether it
    # consumed everything instead of being told in advance. Same
    # protection, zero bytes.
    return MAGIC + _crc(data).to_bytes(4, 'big') + body


def period_of(blob):
    """The record period a stream was compressed at, or None.

    The site displayed "period 0" for every file because the caller had
    no way to read it back and hard-coded a zero. The period is the most
    interesting thing the detector found - an 8-byte sensor record, a
    400-byte sensor cycle - so it is worth exposing properly.

    Only the modes that store a period at a known offset are decoded
    here; the rest return None rather than a wrong number."""
    m = mode_of(blob)
    if m is None:
        return None
    try:
        pos = HEADER_SIZE + 1
        _, pos = _unvarint(blob, pos)          # original length
        if m in (MODE_FLAT, MODE_PERCOL, MODE_CROSS, MODE_LINEAR):
            if m == MODE_FLAT:
                pos += 2                        # tid, pid
            per, _ = _unvarint(blob, pos)
            return per or None
    except Exception:
        return None
    return None


def mode_of(blob):
    """The mode id of a compressed stream.

    Callers used to read blob[7] directly, which was correct only while
    the header was a fixed 3+4 bytes. Adding the varint length field moved
    it, and every display that read blob[7] started showing nonsense - the
    site raised KeyError on a real file.

    A format that anyone reads by hand-indexing will break the next time
    the header changes, so the accessor lives here instead."""
    # HEADER_SIZE, not a hand-written 7 or a stale varint skip.
    #
    # This accessor was added because callers read blob[7] directly and
    # broke when a length field was inserted. Then the length field was
    # removed again and THIS FUNCTION broke, because it still skipped
    # past one - returning mode 176. The same bug, in the fix for the bug,
    # inside one session.
    #
    # Anything that needs to know where the body starts reads HEADER_SIZE.
    if len(blob) < HEADER_SIZE + 1 or blob[:3] != MAGIC:
        return None
    return blob[HEADER_SIZE]


def decompress(blob, verify=True):
    """Decode and verify. Raises on any corruption rather than returning
    plausible-looking wrong data.

    KNOWN LIMITATION: TRAILING BYTES ARE IGNORED.

    Appending data to a valid stream - or concatenating two of them -
    decodes without error and returns the FIRST stream's data. That is not
    corruption: what comes back is correct. But a reader that ignores
    trailing bytes cannot be used to store several streams end to end
    without silently losing all but the first.

    A length field in the header fixes it completely and costs 2-3 bytes
    on EVERY file. Measured on five real files, that showed up as a small
    but consistent regression against the previous version, on data where
    the whole point is the last few bytes. The packers can report leftover
    bytes themselves via unused_data, which is free - but only lzma, zlib
    and bz2 expose it, and the payloads that most often win here are cmix
    and raw, which do not.

    So the honest position: single streams are safe and verified by CRC32.
    If you ever store several in one file, frame them yourself with a
    length - do not rely on this decoder to find the boundary.

    THE STREAM LENGTH IS CHECKED, NOT JUST THE CONTENT.

    The CRC covers the ORIGINAL bytes, so it says nothing about the
    compressed stream. Without a length field the decoder could not tell
    where the stream was supposed to end: appending a second blob, or a
    page of garbage, decoded without complaint and returned the first
    file's data. Two concatenated files silently became one.

    That is not corruption - the data returned was correct - but a decoder
    that ignores trailing bytes cannot be used in a stream or an archive
    without losing data quietly. The length is now written into the header
    and checked on the way out."""
    if len(blob) < 8:
        raise ValueError("stream too short to contain a header")
    if blob[:3] != MAGIC:
        raise ValueError("not a PF stream")
    want = int.from_bytes(blob[3:7], 'big')
    body = blob[HEADER_SIZE:]
    # No length check here - the packers report leftovers themselves via
    # unused_data, which costs nothing to store.
    # BOUND THE DECLARED LENGTH BEFORE DECODING.
    #
    # Every mode reads an original-length varint and builds an output of
    # that size. A corrupted length field therefore asks the decoder to
    # construct something enormous: measured, a single flipped bit made a
    # 105-byte blob take over twenty seconds, and others three.
    #
    # On a public server that is a denial of service - one malformed
    # upload holds a worker forever - and the CRC cannot help, because it
    # is only checked AFTER the output exists.
    #
    # The bound is deliberately generous. A megabyte of zeros compresses
    # to about 30 bytes, so ratios in the thousands are legitimate; only
    # absurd claims are refused.
    _hdr_n, _ = _unvarint(body, 1)
    _cap = max(1 << 20, len(body) * 20000)
    if _hdr_n > _cap:
        raise ValueError(
            f"declared length {_hdr_n} is implausible for a "
            f"{len(body)}-byte stream - corrupt header")

    out = _decode_inner(body)
    if verify and _crc(out) != want:
        raise ValueError("checksum mismatch: stream is corrupt or truncated")
    return out


def _decode_inner(b):
    mode = b[0]
    n, pos = _unvarint(b, 1)

    # The same bound as decompress(), applied HERE because every mode
    # passes through this function - including the nested calls that
    # segmented and chained modes make on their own sub-streams. Bounding
    # only the outer header left byte 20 of a 105-byte blob hanging for
    # over twenty seconds, because the corruption was in an inner length.
    if n > max(1 << 20, len(b) * 20000):
        raise ValueError(f"declared length {n} implausible for "
                         f"{len(b)} bytes - corrupt stream")

    if mode == MODE_RAW:
        out = bytes(b[pos:pos + n])
        if len(out) != n:
            raise ValueError(
                f"truncated stream: need {n} bytes, have {len(out)}")
        return out

    if mode == MODE_FLAT:
        tid = b[pos]; pid = b[pos + 1]
        p, pos = _unvarint(b, pos + 2)
        x = unpack_with(pid, b[pos:], strict=True)
        return TRANSFORMS[tid][2](x, p, n)

    if mode == MODE_PERCOL:
        p_, pos = _unvarint(b, pos)
        pid = b[pos]; pos += 1
        nbytes = (p_ + 1) // 2
        packed = b[pos:pos + nbytes]
        pos += nbytes
        ids = []
        for byte in packed:
            ids.append(byte >> 4)
            ids.append(byte & 0xF)
        ids = ids[:p_]
        x = unpack_with(pid, b[pos:], strict=True)
        return percol_inverse(x, p_, ids, n)

    if mode == MODE_CHUNK:
        blk, pos = _unvarint(b, pos)
        cnt, pos = _unvarint(b, pos)
        out = bytearray()
        for _ in range(cnt):
            ln, pos = _unvarint(b, pos)
            piece = b[pos:pos + ln]
            pos += ln
            n_in = int.from_bytes(piece[:4], "big")
            if n_in > max(1 << 20, len(piece) * 20000):
                raise ValueError("chunk claims an implausible length")
            with _cmix_sized(n_in):
                out += _cmix.decompress(piece)
        if len(out) != n:
            raise ValueError("chunked stream length mismatch")
        return bytes(out)

    if mode == MODE_GCD:
        w = b[pos]; big = bool(b[pos + 1]); signed = bool(b[pos + 2])
        tg = b[pos + 3]; pidg = b[pos + 4]
        pg, pos = _unvarint(b, pos + 5)
        g, pos = _unvarint(b, pos)
        cnt, pos = _unvarint(b, pos)
        xg = unpack_with(pidg, b[pos:], strict=True)
        nd = TRANSFORMS[tg][2](xg, pg, cnt * w)
        return gcd_restore(nd, g, w, big, cnt, signed)

    if mode == MODE_LPFX:
        w = b[pos]; big = bool(b[pos + 1])
        th = b[pos + 2]; pidh = b[pos + 3]
        tb = b[pos + 4]; pidb = b[pos + 5]
        ph, pos = _unvarint(b, pos + 6)
        pb, pos = _unvarint(b, pos)
        count, pos = _unvarint(b, pos)
        hlen, pos = _unvarint(b, pos)
        bhlen, pos = _unvarint(b, pos)
        xh = unpack_with(pidh, b[pos:pos + bhlen])
        xb = unpack_with(pidb, b[pos + bhlen:])
        hdr = TRANSFORMS[th][2](xh, ph, hlen)
        bodies = TRANSFORMS[tb][2](xb, pb, n - hlen)
        return lpfx_join(hdr, bodies, count, w, big)

    if mode == MODE_RLE:
        tidr = b[pos]; pidr = b[pos + 1]
        pr, pos = _unvarint(b, pos + 2)
        rlen, pos = _unvarint(b, pos)
        xr = unpack_with(pidr, b[pos:], strict=True)
        rl = TRANSFORMS[tidr][2](xr, pr, rlen)
        return rle_inverse(rl)

    if mode == MODE_FIXEDW:
        tidx = b[pos]; pidx = b[pos + 1]
        px, pos = _unvarint(b, pos + 2)
        wlen, pos = _unvarint(b, pos)
        xx = unpack_with(pidx, b[pos:], strict=True)
        wx = TRANSFORMS[tidx][2](xx, px, wlen)
        return _fixedw.decode(wx)

    if mode == MODE_LOGCOL:
        tidg = b[pos]; pidg = b[pos + 1]
        pg, pos = _unvarint(b, pos + 2)
        glen, pos = _unvarint(b, pos)
        xg = unpack_with(pidg, b[pos:], strict=True)
        gx = TRANSFORMS[tidg][2](xg, pg, glen)
        return _logcol.decode(gx)

    if mode == MODE_FASTA:
        return _fasta.decode(bytes(b[pos:]))

    if mode == MODE_CSVCOL:
        tidc = b[pos]; pidc = b[pos + 1]
        pc, pos = _unvarint(b, pos + 2)
        clen, pos = _unvarint(b, pos)
        xc = unpack_with(pidc, b[pos:], strict=True)
        cx = TRANSFORMS[tidc][2](xc, pc, clen)
        return _csvcol.decode(cx)

    if mode == MODE_WDELTA:
        w = b[pos]; big = bool(b[pos + 1]); kind = b[pos + 2]
        tidw = b[pos + 3]; pidw = b[pos + 4]
        pw, pos = _unvarint(b, pos + 5)
        hoff, pos = _unvarint(b, pos)
        plen, pos = _unvarint(b, pos)
        blob = unpack_with(pidw, b[pos:], strict=True)
        head, xw = blob[:hoff], blob[hoff:]
        packed = TRANSFORMS[tidw][2](xw, pw, plen)
        # The SAMPLE count comes from the original float32 array - four
        # bytes each - not from the narrowed integer width. Dividing by w
        # gave the wrong count and struct raised on a real SAC file.
        cnt = (n - hoff) // 4
        ints = _waveform_undelta(packed, kind, cnt, w, big)
        return head + _fast.int_to_float(ints, w, big)

    if mode == MODE_F2I:
        w = b[pos]; big = bool(b[pos + 1]); tidf = b[pos + 2]; pidf = b[pos + 3]
        pf_, pos = _unvarint(b, pos + 4)
        hoff, pos = _unvarint(b, pos)
        raw = unpack_with(pidf, b[pos:], strict=True)
        head, xf = raw[:hoff], raw[hoff:]
        nints = (n - hoff) // 4
        ints = TRANSFORMS[tidf][2](xf, pf_, nints * w)
        return head + _fast.int_to_float(ints, w, big, nints)

    if mode == MODE_VARINT:
        w = b[pos]; tidv = b[pos + 1]; pidv = b[pos + 2]
        pv, pos = _unvarint(b, pos + 3)
        count, pos = _unvarint(b, pos)
        xv = unpack_with(pidv, b[pos:], strict=True)
        fixed = TRANSFORMS[tidv][2](xv, pv, count * w)
        return varint_narrow(fixed, w, count)

    if mode == MODE_BWT:
        pidb = b[pos]
        bidx, pos = _unvarint(b, pos + 1)
        bw = unpack_with(pidb, b[pos:], strict=True)
        return bwt_inverse(bw, bidx)

    if mode == MODE_CHAIN:
        tid1 = b[pos]; t2 = b[pos + 1]; pidc = b[pos + 2]
        pos += 3
        p1, pos = _unvarint(b, pos)
        p2, pos = _unvarint(b, pos)
        # Store the intermediate length rather than recomputing it. The
        # first version transformed a dummy buffer to derive it, which
        # happened to work for length-preserving transforms and would have
        # silently broken on any transform that changes size (widening
        # does exactly that). One varint is cheaper than that class of bug.
        x1_len, pos = _unvarint(b, pos)
        y = unpack_with(pidc, b[pos:], strict=True)
        x1 = TRANSFORMS[t2][2](y, p2, x1_len)
        return TRANSFORMS[tid1][2](x1, p1, n)

    if mode == MODE_LINEAR:
        pp, pos = _unvarint(b, pos)
        w = b[pos]; ref = b[pos + 1]; tgt = b[pos + 2]
        a = b[pos + 3] - 128
        tid2 = b[pos + 4]; pidl = b[pos + 5]
        pos += 6
        bb, pos = _unvarint(b, pos)
        p2, pos = _unvarint(b, pos)
        x2 = unpack_with(pidl, b[pos:], strict=True)
        x = TRANSFORMS[tid2][2](x2, p2, n)
        return linear_inverse(x, pp, w, ref, tgt, a, bb, n)

    if mode == MODE_DICT:
        pp, pos = _unvarint(b, pos)
        nf = b[pos]; pos += 1
        flds = []
        for _ in range(nf):
            flds.append((b[pos], b[pos + 1])); pos += 2
        nrec, pos = _unvarint(b, pos)
        hl, pos = _unvarint(b, pos)
        il, pos = _unvarint(b, pos)
        rl, pos = _unvarint(b, pos)
        # variant flag is folded into the high bit of the packer id, so it
        # costs nothing - a whole extra byte was showing up as a 2-byte
        # regression on 70-byte outputs
        flag = b[pos]; pos += 1
        variant = 1 if (flag & 0x80) else 0
        pidd = flag & 0x7F
        blen, pos = _unvarint(b, pos)
        if variant == 1:
            idx_blob = b[pos:pos + il]; pos += il
            raw = unpack_with(pidd, b[pos:pos + blen])
            head = raw[:hl]
            rest = raw[hl:hl + rl]
            tl = raw[hl + rl:]
            ks = []
            ph = 0
            for off, w in flds:
                ks.append(head[ph] + 1)
                ph += 1 + (head[ph] + 1) * w
            idxs = bytearray()
            q = 0
            for kk in ks:
                ln, q = _unvarint(idx_blob, q)
                seg = idx_blob[q:q + ln]; q += ln
                idxs += base_unpack(seg, kk, nrec) if kk > 1 else bytes(nrec)
            idxs = bytes(idxs)
        else:
            raw = unpack_with(pidd, b[pos:pos + blen])
            head = raw[:hl]
            idxs = raw[hl:hl + il]
            rest = raw[hl + il:hl + il + rl]
            tl = raw[hl + il + rl:]
        return dict_inverse(head, idxs, rest, tl, pp, flds, nrec, n)

    if mode == MODE_UNPACK:
        w = b[pos]; pidu = b[pos + 1]
        p2, pos = _unvarint(b, pos + 2)
        count, pos = _unvarint(b, pos)
        tlen, pos = _unvarint(b, pos)
        raw = unpack_with(pidu, b[pos:], strict=True)
        xlen = len(raw) - tlen
        x, tail = raw[:xlen], raw[xlen:]
        u = t_transdelta_inv(x, p2, count * 2)
        return repack_bits(u, w, count, tail)

    if mode == MODE_SEG:
        nparts, pos = _unvarint(b, pos)
        lens = []
        for _ in range(nparts):
            v, pos = _unvarint(b, pos)
            lens.append(v)
        out = bytearray()
        for ln in lens:
            out += decompress(b[pos:pos + ln])
            pos += ln
        assert len(out) == n, f"segment length mismatch {len(out)} != {n}"
        return bytes(out)

    if mode == MODE_CROSS:
        pp, pos = _unvarint(b, pos)
        kind = b[pos]; pos += 1
        if kind == 1:
            w, op, ref = b[pos], b[pos + 1], b[pos + 2]; pos += 3
        else:
            w, op, r1, r2, tg = (b[pos], b[pos + 1], b[pos + 2],
                                 b[pos + 3], b[pos + 4]); pos += 5
        tid2 = b[pos]; pid2 = b[pos + 1]
        p2, pos = _unvarint(b, pos + 2)
        x2 = unpack_with(pid2, b[pos:], strict=True)
        x = TRANSFORMS[tid2][2](x2, p2, n)
        if kind == 1:
            return cross_inverse(x, pp, w, ref, n, op)
        return cross2_inverse(x, pp, w, op, r1, r2, tg, n)

    if mode == MODE_SPLIT:
        rec, pos = _unvarint(b, pos)
        hdr, pos = _unvarint(b, pos)
        off, pos = _unvarint(b, pos)
        ht = b[pos]
        hp, pos = _unvarint(b, pos + 1)
        pt = b[pos]
        pp, pos = _unvarint(b, pos + 1)
        prelen, pos = _unvarint(b, pos)
        hlen, pos = _unvarint(b, pos)
        plen, pos = _unvarint(b, pos)
        tlen, pos = _unvarint(b, pos)
        pids = b[pos]; pos += 1
        raw = unpack_with(pids, b[pos:], strict=True)
        pre = raw[:prelen]
        hx = raw[prelen:prelen + hlen]
        px = raw[prelen + hlen:prelen + hlen + plen]
        tail = raw[prelen + hlen + plen:]
        nrec = (n - off) // rec
        heads = TRANSFORMS[ht][2](hx, hp, nrec * hdr)
        pays = TRANSFORMS[pt][2](px, pp, nrec * (rec - hdr))
        out = bytearray(pre)
        plen_rec = rec - hdr
        for r in range(nrec):
            out += heads[r * hdr:(r + 1) * hdr]
            out += pays[r * plen_rec:(r + 1) * plen_rec]
        out += tail
        assert len(out) == n, f"split length mismatch: {len(out)} != {n}"
        return bytes(out)

    raise ValueError(f"unknown mode {mode}")


def _decode_body(blob):
    return _decode_inner(blob)
