"""Speed and ratio against every standard tool, on your own machine.

Sandbox numbers understate everything - one slow core distorts absolute
speeds and makes threading look useless. This measures where it counts.

    python benchmark.py FILE [FILE ...]
"""
import sys
import os
import time
import zlib
import lzma
import bz2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prefilter_v31 as pf

files = [f for f in sys.argv[1:] if os.path.exists(f)]
if not files:
    print(__doc__)
    raise SystemExit(1)

LIMIT = 4_000_000

tools = [
    ("gzip -9",   lambda x: zlib.compress(x, 9),
                  lambda x: zlib.decompress(x)),
    ("bzip2 -9",  lambda x: bz2.compress(x, 9),
                  lambda x: bz2.decompress(x)),
    ("lzma -9",   lambda x: lzma.compress(x, preset=9),
                  lambda x: lzma.decompress(x)),
    ("prefilter", lambda x: pf.compress_fast(x, 2),
                  lambda x: pf.decompress(x)),
]
try:
    import zstandard as zs
    tools.insert(3, ("zstd -19",
                     lambda x: zs.ZstdCompressor(level=19).compress(x),
                     lambda x: zs.ZstdDecompressor().decompress(x)))
except Exception:
    pass
try:
    import brotli
    tools.insert(-1, ("brotli -11",
                      lambda x: brotli.compress(x, quality=11),
                      lambda x: brotli.decompress(x)))
except Exception:
    pass

agg = {n: [0, 0.0, 0, 0.0] for n, _, _ in tools}   # in, enc s, out, dec s

print(f"\n  {os.cpu_count()} cores\n")
for path in files:
    d = open(path, "rb").read(LIMIT)
    print(f"  {os.path.basename(path)}  ({len(d):,} bytes)")
    for name, enc, dec in tools:
        t = time.perf_counter()
        o = enc(d)
        te = time.perf_counter() - t
        t = time.perf_counter()
        back = dec(o)
        td = time.perf_counter() - t
        ok = back == d
        a = agg[name]
        a[0] += len(d); a[1] += te; a[2] += len(o); a[3] += td
        flag = "" if ok else "   NOT LOSSLESS"
        print(f"    {name:12s} {len(o):>10,}  {len(d)/te/1e6:>6.2f} MB/s in  "
              f"{len(d)/td/1e6:>7.1f} MB/s out{flag}")
    print()

print("  " + "=" * 66)
print("  TOTALS\n")
base = agg["prefilter"]
pfs = base[0] / base[1] / 1e6
best_free = min(a[2] for n, a in agg.items() if n != "prefilter")
print(f"  {'tool':12s} {'MB/s in':>9s} {'MB/s out':>9s} {'relative':>13s} "
      f"{'output':>12s} {'vs best':>9s}")
print("  " + "-" * 70)
for name, _, _ in tools:
    a = agg[name]
    sp = a[0] / a[1] / 1e6
    dsp = a[0] / a[3] / 1e6
    rel = "baseline" if name == "prefilter" else f"{sp/pfs:.1f}x faster"
    print(f"  {name:12s} {sp:>9.2f} {dsp:>9.1f} {rel:>13s} {a[2]:>12,} "
          f"{100*(1-a[2]/best_free):>+8.1f}%")
print()
