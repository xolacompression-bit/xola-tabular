"""Quality regression tests: does the router still BEAT the standard tools?

Correctness and quality are different bars, and only one of them was
being tested. Every check in this project asks "does it round-trip".
None asked "is it as good as it should be".

That gap is not theoretical. Four bugs this week were correct and
silently worse:

  the chunked path hardcoding PACK_ZLIB          21% worse
  a 16 MB gate that skipped columnar mode         4x worse
  first-module-wins ordering                    +4.5% where +12.9% was available
  a brotli cap that made 6 workers slower than 4

All four round-tripped perfectly. All four would have shipped.
"""
import sys, os, zlib, lzma, bz2, subprocess
sys.path.insert(0, '/home/claude/cmix')
import prefilter_v31 as PF


def _xz(x):
    try:
        return len(subprocess.run(['xz', '-9e', '-c'], input=x,
                                  capture_output=True).stdout)
    except Exception:
        return None


def best_standard(d):
    """Smallest of every standard tool available on this machine."""
    cands = [len(zlib.compress(d, 9)), len(bz2.compress(d, 9)),
             len(lzma.compress(d, preset=9))]
    x = _xz(d)
    if x:
        cands.append(x)
    try:
        import zstandard as zs
        cands.append(len(zs.ZstdCompressor(level=19).compress(d)))
    except Exception:
        pass
    try:
        import brotli
        cands.append(len(brotli.compress(d, quality=11)))
    except Exception:
        pass
    return min(cands)


# file, minimum gain we must still achieve. Set BELOW the measured value
# so ordinary variation does not fail the build - these are floors that
# only a real regression can breach.
EXPECT = [
    ('demo_sensor.bin',                      40.0),
    ('demo_gps.bin',                         25.0),
    ('demo_ticks.bin',                       25.0),
    ('IU_KONO_00_BHZ_2024_92_000000_1.SAC',  30.0),
    ('IU_COLA_00_BHZ_2024_61_000000_1.SAC',  30.0),
    ('IU_TUC_00_BHZ_2024_122_000000_1.SAC',  12.0),
    ('47662099999.csv',                       7.0),
    ('Apache_2k_log.txt',                    10.0),
    ('genome.fna',                            8.0),
    ('41001h2023.txt',                       25.0),
    ('Bitstamp_BTCUSD_1h.csv',               15.0),
    ('diamonds.csv',                          8.0),
]

# Files where we must simply not LOSE. Parity is the correct answer on
# nested formats and already-compressed data; a real loss is not.
NO_LOSS = ['2701-0.txt', 'large-file.json', '_pydecimal.py', '0002.DCM']


# Where to look for the test files.
#
# Pass a folder on the command line, or set XOLA_TESTDIR. Without either,
# a few common locations are tried. Files that cannot be found are simply
# skipped - the test reports how many it actually checked, so "0/0 files"
# means it found nothing, not that everything passed.
SEARCH = [os.environ.get("XOLA_TESTDIR", ""),
          '/mnt/user-data/uploads/', '/tmp/allfiles/', '/tmp/newset/',
          '.', './testfiles', '../testfiles']


def _key(name):
    """A loose identity for a test file.

    The same file is named IU_KONO_00_BHZ_2024_92_000000_1.SAC in one
    place and IU.KONO.00.BHZ.2024.92.000000.1.SAC in another. Matching on
    the exact string found nothing and the test reported 0/0 PASSED - a
    check that verifies nothing and announces success, which is precisely
    the failure this file exists to catch.
    """
    return ''.join(c for c in name.lower() if c.isalnum())


def _find(name):
    want = _key(name)
    for base in SEARCH:
        if not base or not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for f in files:
                if _key(f) == want:
                    return os.path.join(root, f)
            # do not descend forever on a huge tree
            if root.count(os.sep) - base.count(os.sep) > 3:
                _dirs[:] = []
    return None


def run(limit=400000, verbose=True):
    fails = []
    checked = 0
    for name, floor in EXPECT:
        p = _find(name)
        if not p:
            continue
        d = open(p, 'rb').read()[:limit]
        if name.lower().endswith(('.csv', '.txt', '.log', '.fna')):
            cut = d.rfind(b'\n')
            if cut > 0:
                d = d[:cut + 1]
        free = best_standard(d)
        blob = PF.compress_fast(d, 2)
        if PF.decompress(blob) != d:
            fails.append(f"{name}: NOT LOSSLESS")
            continue
        gain = 100 * (1 - len(blob) / free)
        checked += 1
        mark = 'ok ' if gain >= floor else 'FAIL'
        if gain < floor:
            fails.append(f"{name}: {gain:+.1f}% but floor is {floor:+.1f}%")
        if verbose:
            print(f"  {mark} {name[:30]:32s} {gain:>+7.1f}%  "
                  f"floor {floor:>+5.1f}%")

    for name in NO_LOSS:
        p = _find(name)
        if not p:
            continue
        d = open(p, 'rb').read()[:limit]
        if name.lower().endswith(('.csv', '.txt', '.log', '.py')):
            cut = d.rfind(b'\n')
            if cut > 0:
                d = d[:cut + 1]
        free = best_standard(d)
        blob = PF.compress_fast(d, 2)
        if PF.decompress(blob) != d:
            fails.append(f"{name}: NOT LOSSLESS")
            continue
        gain = 100 * (1 - len(blob) / free)
        checked += 1
        # 20 bytes of container is acceptable; anything more is a loss.
        worst = -100.0 * 20 / free
        mark = 'ok ' if gain >= worst else 'FAIL'
        if gain < worst:
            fails.append(f"{name}: {gain:+.1f}%, worse than the container")
        if verbose:
            print(f"  {mark} {name[:30]:32s} {gain:>+7.1f}%  "
                  f"(must not lose)")

    print()
    if checked == 0:
        print("  NOTHING WAS CHECKED - no test files were found.")
        print("  Point it at them:")
        print('      python quality_test.py "C:\\path\\to\\your files"')
        print("  A test that checks nothing and reports success is worse")
        print("  than no test at all, so this is an ERROR, not a pass.")
        return 2
    if fails:
        print(f"  {len(fails)} QUALITY REGRESSION(S) on {checked} files:")
        for f in fails:
            print(f"    {f}")
        return 1
    print(f"  {checked}/{checked} files still meet their quality floor")
    return 0


if __name__ == '__main__':
    if len(sys.argv) > 1:
        SEARCH.insert(0, sys.argv[1])
    rc = run()
    sys.exit(rc)
