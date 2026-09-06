"""benchmark.py — check the claims yourself.

Every number in the README came from this script or one like it. It
compares against the system `tar` and `bzip2` where available, not
against our own implementations, and it verifies each file back out
with SHA-256 rather than trusting the tool's own report.

    python benchmark.py DIRECTORY_OF_SIMILAR_FILES

or, to build a test set from one large CSV:

    python benchmark.py --split BIG.csv --files 1000

Nothing is uploaded anywhere. It reads your files and prints numbers.
"""

import sys
import os
import io
import time
import bz2
import lzma
import tarfile
import hashlib
import shutil
import subprocess
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import groupcol


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def make_tar(root, names):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for n in names:
            t.add(os.path.join(root, n), arcname=n)
    return buf.getvalue()


def split_csv(path, count, out_dir):
    """Turn one CSV into many, the way a daily export would arrive."""
    data = open(path, "rb").read()
    data = data[:data.rfind(b"\n") + 1]
    lines = data.split(b"\n")[:-1]
    head, rows = lines[0], lines[1:]
    per = max(1, len(rows) // count)
    os.makedirs(out_dir, exist_ok=True)
    for i in range(count):
        chunk = rows[i * per:(i + 1) * per if i < count - 1 else len(rows)]
        if not chunk:
            break
        open(os.path.join(out_dir, f"part{i:05d}.csv"), "wb").write(
            b"\n".join([head] + chunk) + b"\n")
    return sorted(os.listdir(out_dir))


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1

    tmp = None
    if args[0] == "--split":
        count = 1000
        if "--files" in args:
            count = int(args[args.index("--files") + 1])
        tmp = tempfile.mkdtemp()
        root = tmp
        names = split_csv(args[1], count, root)
        print(f"\n  split into {len(names)} files")
    else:
        root = args[0]
        names = sorted(f for f in os.listdir(root)
                       if os.path.isfile(os.path.join(root, f)))

    if not names:
        print("  nothing to test")
        return 1

    blobs = [open(os.path.join(root, n), "rb").read() for n in names]
    raw = sum(len(b) for b in blobs)
    print(f"  {len(names)} files, {human(raw)} on disk\n")

    results = []

    tarball = make_tar(root, names)
    t = time.perf_counter()
    b = bz2.compress(tarball, 9)
    results.append(("tar + bzip2 -9", len(b), time.perf_counter() - t))

    t = time.perf_counter()
    x = lzma.compress(tarball, preset=6)
    results.append(("tar + xz -6", len(x), time.perf_counter() - t))

    # the system tools, as an independent check on our own tar
    if shutil.which("tar") and shutil.which("bzip2"):
        with tempfile.TemporaryDirectory() as d:
            tf = os.path.join(d, "t.tar")
            subprocess.run(["tar", "cf", tf, "-C", root] + names,
                           capture_output=True)
            subprocess.run(["bzip2", "-9", tf], capture_output=True)
            if os.path.exists(tf + ".bz2"):
                results.append(("system tar + bzip2",
                                os.path.getsize(tf + ".bz2"), None))

    t = time.perf_counter()
    out, route = groupcol.archive(blobs)
    dt = time.perf_counter() - t
    results.append((f"groupcol ({route})", len(out), dt))

    base = results[0][1]
    print(f"  {'method':24s} {'size':>12s} {'vs tar+bz2':>11s} {'time':>8s}")
    print("  " + "-" * 60)
    for name, size, secs in results:
        tm = f"{secs:.1f}s" if secs is not None else "-"
        print(f"  {name:24s} {size:>12,} {100 * (1 - size / base):>+10.1f}% "
              f"{tm:>8s}")

    print()
    t = time.perf_counter()
    back = groupcol.unarchive(out)
    ok = len(back) == len(blobs) and all(
        hashlib.sha256(a).digest() == hashlib.sha256(b).digest()
        for a, b in zip(back, blobs))
    print(f"  all {len(names)} files verified by SHA-256: {ok} "
          f"({time.perf_counter() - t:.1f}s)")

    if route == "bundle":
        bundle = groupcol.try_encode(blobs)
        if bundle is not None:
            i = len(names) // 2
            t = time.perf_counter()
            one = groupcol.extract_one(bundle, i)
            print(f"  one file pulled from {len(names)}: "
                  f"{time.perf_counter() - t:.2f}s, "
                  f"identical: {one == blobs[i]}")

    print(f"\n  throughput: {raw / dt / 1e6:.2f} MB/s\n")

    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
