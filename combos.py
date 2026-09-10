"""combos.py — every combination of the stages we have, measured.

We have three stages that can each be done several ways:

    ROUND 1   how the files are arranged
              one at a time / one stream / columns aligned

    ROUND 2   what codes the result
              gzip / bzip2 / xz, plus cmix and prefilter if they
              happen to be importable - neither is in the shipped
              tool, and both were measured and left out

    ROUND 3   whether the coder is chosen per column
              one for all / the best for each

Rather than guess which combination is best, this runs all of them on
the same data and records the answer. The interesting output is not the
winner but WHICH DATA changes the winner, because that is what a router
needs to know.

    python combos.py FOLDER
"""

import sys
import os
import io
import time
import gzip
import bz2
import lzma
import tarfile
import itertools

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CODERS = {
    "gzip": (lambda d: gzip.compress(d, 9), gzip.decompress),
    "bzip2": (lambda d: bz2.compress(d, 9), bz2.decompress),
    "xz": (lzma.compress, lzma.decompress),
}
try:
    import cmix_v3
    CODERS["cmix"] = (cmix_v3.compress, cmix_v3.decompress)
except Exception:
    pass
# prefilter is left out of the sweep on purpose.
#
# It won once, on a smooth sensor column, by 8% - and took 24 seconds
# against cmix's 0.75. Including it in a sweep that runs every coder
# against every arrangement turns a two-minute measurement into an
# hour, and the answer was already known.
if "--slow" in sys.argv:
    try:
        import prefilter_v31
        CODERS["prefilter"] = (prefilter_v31.compress,
                               prefilter_v31.decompress)
    except Exception:
        pass


def load(folder, limit=None):
    files = sorted(f for f in os.listdir(folder)
                   if f.lower().endswith((".csv", ".tsv", ".log", ".txt")))
    if limit:
        files = files[:limit]
    return [open(os.path.join(folder, f), "rb").read() for f in files]


def as_tar(blobs):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for i, b in enumerate(blobs):
            ti = tarfile.TarInfo(f"{i:06d}")
            ti.size = len(b)
            t.addfile(ti, io.BytesIO(b))
    return buf.getvalue()


def columns_of(blobs, sep=","):
    """Every column, gathered across every file.

    This is the arrangement that matters: column 3 from all thousand
    files in one place, because column 3 means the same thing in all of
    them."""
    rows = []
    ncol = 0
    for b in blobs:
        lines = b.decode("utf-8", "replace").split("\n")
        for line in lines[1:]:
            if line.strip():
                parts = line.split(sep)
                rows.append(parts)
                ncol = max(ncol, len(parts))
    cols = []
    for i in range(ncol):
        cols.append("\n".join(r[i] if len(r) > i else "" for r in rows)
                    .encode())
    return cols


def measure(name, data, coder):
    comp, decomp = CODERS[coder]
    t = time.perf_counter()
    out = comp(data) if isinstance(data, (bytes, bytearray)) else None
    dt = time.perf_counter() - t
    return len(out), dt


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else "."
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) \
        if "--limit" in sys.argv else None
    blobs = load(folder, limit)
    if not blobs:
        print(f"\n  no text or CSV files in {folder}\n")
        return 1
    orig = sum(len(b) for b in blobs)
    print(f"\n  {len(blobs)} files, {orig:,} bytes")
    print(f"  coders available: {', '.join(CODERS)}\n")

    results = []

    # ROUND 1a: each file on its own
    for coder in CODERS:
        t = time.perf_counter()
        try:
            total = sum(len(CODERS[coder][0](b)) for b in blobs)
        except Exception:
            continue
        dt = time.perf_counter() - t
        results.append(("one file at a time", coder, "one coder", total, dt))

    # ROUND 1b: all in one stream
    tar = as_tar(blobs)
    for coder in CODERS:
        t = time.perf_counter()
        try:
            total = len(CODERS[coder][0](tar))
        except Exception:
            continue
        dt = time.perf_counter() - t
        results.append(("one stream", coder, "one coder", total, dt))

    # ROUND 1c: columns aligned across files
    try:
        cols = columns_of(blobs)
    except Exception as e:
        print(f"  could not split into columns: {e}")
        cols = None

    if cols:
        joined = b"\x00COL\x00".join(cols)
        for coder in CODERS:
            t = time.perf_counter()
            try:
                total = len(CODERS[coder][0](joined))
            except Exception:
                continue
            dt = time.perf_counter() - t
            results.append(("columns aligned", coder, "one coder",
                            total, dt))

        # ROUND 3: the best coder chosen separately for each column
        t = time.perf_counter()
        total = 0
        picks = []
        for i, c in enumerate(cols):
            best = None
            for coder in CODERS:
                try:
                    n = len(CODERS[coder][0](c))
                except Exception:
                    continue
                if best is None or n < best[0]:
                    best = (n, coder)
            if best:
                total += best[0]
                picks.append(best[1])
        dt = time.perf_counter() - t
        results.append(("columns aligned", "best per column",
                        "chosen per column", total, dt))

    results.sort(key=lambda r: r[3])
    print(f"  {'arrangement':22s} {'coder':16s} {'bytes':>10s} "
          f"{'saved':>8s} {'time':>8s}")
    print("  " + "-" * 70)
    for arr, coder, _mode, total, dt in results:
        print(f"  {arr:22s} {coder:16s} {total:>10,} "
              f"{100 * (1 - total / orig):>+7.1f}% {dt:>7.2f}s")

    best = results[0]
    print(f"\n  best: {best[0]} + {best[1]} at {best[3]:,} "
          f"({100 * (1 - best[3] / orig):+.1f}%)")
    if cols:
        print(f"  per-column picks: {picks}")

    # what each stage was worth, in isolation
    def find(arr, coder):
        for r in results:
            if r[0] == arr and r[1] == coder:
                return r[3]
        return None
    a = find("one file at a time", "bzip2")
    b = find("one stream", "bzip2")
    c = find("columns aligned", "bzip2")
    d = find("columns aligned", "best per column")
    print("\n  what each step is worth:")
    if a and b:
        print(f"    putting them in one stream      {100 * (1 - b / a):>+6.1f}%")
    if b and c:
        print(f"    lining the columns up           {100 * (1 - c / b):>+6.1f}%")
    if c and d:
        print(f"    a coder chosen per column       {100 * (1 - d / c):>+6.1f}%")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
