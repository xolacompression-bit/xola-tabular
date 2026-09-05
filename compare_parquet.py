"""Compare against Parquet and ORC on YOUR data, not ours.

    pip install pyarrow pandas
    python compare_parquet.py yourfile.csv [more.csv ...]

Reads each CSV, writes it as Parquet and ORC with every codec each
supports, compresses it with this tool, and prints the sizes side by
side. It also checks whether each format gives the ORIGINAL FILE back,
byte for byte, which is the part that usually decides things.

Nothing is uploaded. Nothing leaves the machine.
"""
import sys
import os
import time
import hashlib
import zlib
import lzma
import bz2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import prefilter_v31 as pf
except ImportError:
    print("\n  prefilter_v31.py must be in this folder.\n")
    raise SystemExit(1)


def sha(b):
    return hashlib.sha256(b).hexdigest()


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def run(path):
    raw = open(path, "rb").read()
    print(f"\n  {os.path.basename(path)}   {len(raw):,} bytes\n")
    rows = []

    best_free = min(len(zlib.compress(raw, 9)),
                    len(bz2.compress(raw, 9)),
                    len(lzma.compress(raw, preset=9)))
    rows.append(("gzip / bzip2 / lzma", best_free, True, ""))

    try:
        import pandas as pd
        import pyarrow as pa
        import pyarrow.parquet as pq
        df = pd.read_csv(path, low_memory=False)
        table = pa.Table.from_pandas(df)
        best = None
        for codec in ("zstd", "gzip", "brotli", "snappy"):
            try:
                pq.write_table(table, "_tmp.parquet", compression=codec)
                s = os.path.getsize("_tmp.parquet")
                if best is None or s < best[0]:
                    best = (s, codec)
            except Exception:
                pass
        if best:
            # does it give the original file back?
            pq.write_table(table, "_tmp.parquet", compression=best[1])
            back = pq.read_table("_tmp.parquet").to_pandas()
            back.to_csv("_tmp_back.csv", index=False)
            same = sha(open("_tmp_back.csv", "rb").read()) == sha(raw)
            rows.append((f"Parquet ({best[1]})", best[0], same,
                         "" if same else "  <-- NOT the original file"))
    except ImportError:
        print("    (pyarrow/pandas not installed - skipping Parquet)")
    except Exception as e:
        print(f"    (Parquet failed: {type(e).__name__})")

    try:
        import pyarrow.orc as orc
        best = None
        for codec in ("zstd", "zlib", "snappy"):
            try:
                orc.write_table(table, "_tmp.orc", compression=codec)
                s = os.path.getsize("_tmp.orc")
                if best is None or s < best[0]:
                    best = (s, codec)
            except Exception:
                pass
        if best:
            rows.append((f"ORC ({best[1]})", best[0], False,
                         "  <-- NOT the original file"))
    except Exception:
        pass

    t = time.perf_counter()
    out = pf.compress_fast(raw, 2)
    enc = time.perf_counter() - t
    t = time.perf_counter()
    back = pf.decompress(out)
    dec = time.perf_counter() - t
    exact = sha(back) == sha(raw)
    rows.append(("this tool", len(out), exact, ""))

    rows.sort(key=lambda r: -r[1])
    print(f"    {'format':22s} {'size':>12s} {'of original':>12s} "
          f"{'exact?':>8s}")
    print("    " + "-" * 62)
    for name, size, ok, note in rows:
        print(f"    {name:22s} {size:>12,} {100*size/len(raw):>11.1f}% "
              f"{('yes' if ok else 'NO'):>8s}{note}")

    win = [r for r in rows if r[0] != "this tool"]
    if win:
        smallest_other = min(r[1] for r in win)
        d = 100 * (1 - len(out) / smallest_other)
        print(f"\n    this tool is {d:+.1f}% against the best of the rest")
    print(f"    compressed at {len(raw)/enc/1e6:.2f} MB/s, "
          f"restores at {len(raw)/dec/1e6:.1f} MB/s")

    for f in ("_tmp.parquet", "_tmp.orc", "_tmp_back.csv"):
        try:
            os.remove(f)
        except OSError:
            pass


def main():
    files = [f for f in sys.argv[1:] if os.path.exists(f)]
    if not files:
        print(__doc__)
        return 1
    for f in files:
        try:
            run(f)
        except Exception as e:
            print(f"\n  {os.path.basename(f)}: {type(e).__name__}: {e}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
