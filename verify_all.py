"""Everything that must hold before today's changes can be trusted.

Six things changed: a missing numpy module restored, the extreme-lzma
gate, the quoted CSV path, vectorised period detection, the confirm
slice, and four hidden 16 MB limits unified.

The last one changed what runs on large files, so nothing about large
files has been checked since. This checks all of it.
"""
import sys
import os
import time
import random
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prefilter_v31 as pf

fails = []


def check(label, ok, note=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {label:44s} {note}")
    if not ok:
        fails.append(label)


print("\n  ROUND TRIPS ON REAL FILES\n")
for path in sys.argv[1:]:
    if not os.path.exists(path):
        print(f"  --   {os.path.basename(path)[:44]:44s} not found")
        continue
    d = open(path, "rb").read()
    t = time.perf_counter()
    o = pf.compress_fast(d, 2)
    dt = time.perf_counter() - t
    back = pf.decompress(o)
    ok = hashlib.sha256(d).hexdigest() == hashlib.sha256(back).hexdigest()
    check(os.path.basename(path)[:42], ok,
          f"{len(d):>12,} -> {len(o):>10,}  {len(d)/dt/1e6:>5.2f} MB/s")

print("\n  EDGE CASES\n")
rng = random.Random(17)
cases = {
    "empty": b"",
    "one byte": b"A",
    "all zeros 100 KB": bytes(100_000),
    "all 0xFF": b"\xff" * 50_000,
    "random 200 KB": bytes(rng.randrange(256) for _ in range(200_000)),
    "one long line": b"a" * 300_000,
    "only newlines": b"\n" * 100_000,
    "ragged csv": b"a,b\n1,2,3\n4\n" * 5_000,
    "quoted commas": b'"a,b",c\n"d,e",f\n' * 5_000,
    "utf8": ("héllo wörld " * 20_000).encode(),
}
for name, d in cases.items():
    try:
        o = pf.compress_fast(d, 2)
        ok = pf.decompress(o) == d
        check(name, ok, f"{len(d):>12,} -> {len(o):>10,}")
    except Exception as e:
        check(name, False, f"{type(e).__name__}: {e}")

print("\n  SIZES AROUND THE OLD 16 MB LIMIT\n")
base = open(sys.argv[1], "rb").read() if sys.argv[1:] and \
    os.path.exists(sys.argv[1]) else b"x,y\n1,2\n" * 100
for mb in (8, 15, 16, 17, 20, 24):
    n = mb * 1024 * 1024
    d = (base * (n // len(base) + 1))[:n]
    cut = d.rfind(b"\n")
    if cut > 0:
        d = d[:cut + 1]
    try:
        t = time.perf_counter()
        o = pf.compress_fast(d, 2)
        dt = time.perf_counter() - t
        ok = pf.decompress(o) == d
        check(f"{mb} MB", ok,
              f"{len(d):>12,} -> {len(o):>10,}  {len(d)/dt/1e6:>5.2f} MB/s")
    except Exception as e:
        check(f"{mb} MB", False, f"{type(e).__name__}")

print()
if fails:
    print(f"  {len(fails)} FAILURES: {fails}")
else:
    print("  everything passed")
print()
