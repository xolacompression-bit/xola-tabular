"""vs_pcodec.py — the one competitor most likely to beat us.

    pip install pcodec
    python vs_pcodec.py FOLDER_OF_CSVS

WHY THIS ONE
------------
Pcodec compresses numerical sequences and its own benchmarks show it
beating zstd, Parquet and Blosc on numeric columns. That is exactly
where our delta encoding lives - and where the bytes are, since on a
sensor archive the numeric columns are 99% of the bundle.

If Pcodec wins on those columns, the honest thing is to say so, and
possibly to call it rather than compete with it. That is what this
project already does with Lepton for photographs.

If it does not win, the table gets a row that a reviewer would
otherwise ask for.

WHAT IS COMPARED
Each numeric column on its own, four ways:

    bzip2 -9        what a general tool does
    zstd -19
    our delta       differences, stored as fixed-point integers
    pcodec          purpose-built for numerical sequences

Note that Pcodec compresses NUMBERS, not the text of numbers. To be
fair, the comparison is on what it would take to store the column and
get the original text back - so a column of "6.7" and "6" costs
Pcodec something too, because it has to record which is which.

That asymmetry is the point. Our transform declines on such a column;
Pcodec would need the same information. Where the formatting is
uniform, both can drop the text entirely.
"""

import sys
import os
import bz2
import time

import numpy as np

try:
    import pcodec
    # The submodules are listed in dir(pcodec) but are not attached as
    # attributes until imported - so pcodec.standalone raises
    # AttributeError unless you ask for it by name first.
    from pcodec import standalone as pco_standalone
except ImportError:
    print("\n  needs:  pip install pcodec\n")
    raise SystemExit(1)


def _pco_compress(ints):
    """Compress with whichever API this version of pcodec exposes.

    The Python bindings have moved between versions - simple_compress
    has lived at the top level, under standalone, and taken different
    arguments. Guessing wrong prints AttributeError for every column
    and reports a total of zero, which is what happened the first
    time.

    So: try each shape, and if none work, say what IS available rather
    than failing silently."""
    cfg = None
    for maker in (lambda: pcodec.ChunkConfig(),
                  lambda: pcodec.standalone.ChunkConfig(),
                  lambda: None):
        try:
            cfg = maker()
            break
        except Exception:
            continue
    attempts = [
        lambda: pco_standalone.simple_compress(ints, cfg),
        lambda: pco_standalone.simple_compress(ints),
        lambda: pco_standalone.auto_compress(ints),
    ]
    errs = []
    for fn in attempts:
        try:
            return len(fn())
        except Exception as e:
            errs.append(f"{type(e).__name__}")
    names = [n for n in dir(pco_standalone) if not n.startswith("_")]
    raise RuntimeError(
        f"no entry point worked ({', '.join(errs[:3])}). "
        f"standalone has: {names}")

try:
    import zstandard as zstd
except ImportError:
    zstd = None


def columns_of(folder, limit=400):
    files = sorted(f for f in os.listdir(folder)
                   if f.lower().endswith((".csv", ".tsv")))[:limit]
    rows = []
    header = None
    for f in files:
        text = open(os.path.join(folder, f), encoding="utf-8",
                    errors="replace").read()
        lines = text.split("\n")
        if header is None:
            header = [h.strip().strip('"') for h in lines[0].split(",")]
        for line in lines[1:]:
            if line.strip():
                rows.append(line.split(","))
    cols = {}
    for i, name in enumerate(header or []):
        cols[name] = [r[i] for r in rows if len(r) > i]
    return cols


def uniform_decimals(vals):
    """The decimal count if every value shares one, else None."""
    dec = -1
    for v in vals:
        t = v.strip()
        if not t:
            return None
        body = t[1:] if t[0] == "-" else t
        if not body:
            return None
        dot = body.find(".")
        if dot < 0:
            if not body.isdigit():
                return None
            d = 0
        else:
            if body.count(".") > 1:
                return None
            if not body.replace(".", "", 1).isdigit():
                return None
            d = len(body) - dot - 1
        if dec < 0:
            dec = d
        elif d != dec:
            return None
    return dec


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else "."
    cols = columns_of(folder)
    if not cols:
        print(f"\n  no CSV files in {folder}\n")
        return 1

    numeric = {}
    for name, vals in cols.items():
        if len(vals) < 1000:
            continue
        dec = uniform_decimals(vals)
        if dec is not None:
            numeric[name] = (vals, dec)

    print(f"\n  {len(cols)} columns, {len(numeric)} of them uniformly "
          f"numeric\n")
    if not numeric:
        print("  Nothing here has uniform numeric formatting, so neither")
        print("  our delta nor Pcodec can drop the text. That is a real")
        print("  property of the data, not a limitation of either tool.\n")
        return 0

    print(f"  {'column':22s} {'as text':>10s} {'bzip2':>9s} {'zstd':>9s} "
          f"{'our delta':>10s} {'pcodec':>9s} {'winner':>10s}")
    print("  " + "-" * 88)

    tot = {"bzip2": 0, "zstd": 0, "delta": 0, "pcodec": 0}
    for name, (vals, dec) in numeric.items():
        raw = "\n".join(vals).encode()
        scale = 10 ** dec
        ints = np.round(np.array(vals, dtype=np.float64)
                        * scale).astype(np.int64)

        b = len(bz2.compress(raw, 9))
        z = (len(zstd.ZstdCompressor(level=19).compress(raw))
             if zstd else 10 ** 12)
        d = np.diff(ints, prepend=ints[0])
        dd = len(bz2.compress(d.astype("<i8").tobytes(), 9))
        try:
            pc = _pco_compress(ints)
        except Exception as e:
            print(f"  {name[:20]:22s} {e}")
            continue

        tot["bzip2"] += b
        tot["zstd"] += min(z, b)
        tot["delta"] += min(dd, b)
        tot["pcodec"] += pc
        best = min((b, "bzip2"), (z, "zstd"), (dd, "delta"), (pc, "pcodec"))
        print(f"  {name[:20]:22s} {len(raw):>10,} {b:>9,} "
              f"{z if zstd else 0:>9,} {dd:>10,} {pc:>9,} {best[1]:>10s}")

    print("  " + "-" * 88)
    print(f"  {'TOTAL':22s} {'':>10s} {tot['bzip2']:>9,} {tot['zstd']:>9,} "
          f"{tot['delta']:>10,} {tot['pcodec']:>9,}")
    print()
    if not tot["pcodec"]:
        print("  Pcodec produced nothing - see the errors above.\n")
        return 1
    ours = min(tot["delta"], tot["zstd"], tot["bzip2"])
    if tot["pcodec"] < ours:
        print(f"  Pcodec wins by {100 * (1 - tot['pcodec'] / ours):.1f}% on "
              f"the numeric columns.")
        print("  It is purpose-built for numerical sequences and it shows.")
        print("  The honest move is to call it rather than compete with it,")
        print("  the way this project already calls Lepton for photographs.")
    else:
        print(f"  Ours is {100 * (1 - ours / tot['pcodec']):.1f}% smaller "
              f"than Pcodec on these columns.")
        print("  Worth reporting either way - a reviewer will ask.")
    print()
    print("  One caveat that matters: Pcodec stores NUMBERS. Getting the")
    print("  original TEXT back needs the formatting too, which is free")
    print("  only when every value is written the same way. The columns")
    print("  above were selected for exactly that, so this comparison is")
    print("  on Pcodec's best ground.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
