"""compare.py — run this on your own files.

    python compare.py FOLDER

Compresses every CSV, TSV and log file in a folder against seven
general-purpose baselines - gzip, bzip2, xz at two settings, and zstd
at three - and shows what each one gets. Nothing is written and
nothing is changed; the folder is only read.

The point is not to trust our numbers. It is to see what happens on
your data, which is the only measurement that matters to you.
"""

import sys
import os
import glob
import gzip
import bz2
import lzma
import io
import tarfile
import time
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import groupcol


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else "."
    files = []
    for pat in ("*.csv", "*.tsv", "*.log", "*.txt", "*.CSV"):
        files.extend(glob.glob(os.path.join(folder, pat)))
    files = sorted(set(os.path.normcase(f) for f in files))
    if not files:
        print(f"\n  no CSV, TSV, log or text files in {folder}\n")
        return 1

    blobs = []
    for f in files:
        try:
            blobs.append(open(f, "rb").read())
        except OSError:
            pass
    orig = sum(len(b) for b in blobs)
    print(f"\n  {len(blobs)} files, {orig:,} bytes\n")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for i, b in enumerate(blobs):
            ti = tarfile.TarInfo(f"{i:08d}")
            ti.size = len(b)
            t.addfile(ti, io.BytesIO(b))
    tar = buf.getvalue()

    print(f"  {'method':28s} {'bytes':>12s} {'saved':>8s} {'time':>7s}")
    print("  " + "-" * 60)

    def run(name, fn, data=tar):
        t0 = time.perf_counter()
        out = fn(data)
        dt = time.perf_counter() - t0
        print(f"  {name:28s} {len(out):>12,} "
              f"{100 * (1 - len(out) / orig):>+7.1f}% {dt:>6.2f}s")
        return len(out)

    run("tar + gzip -9", lambda d: gzip.compress(d, 9))
    best = run("tar + bzip2 -9", lambda d: bz2.compress(d, 9))
    x = run("tar + xz -9", lambda d: lzma.compress(d, preset=9))
    best = min(best, x)
    xe = run("tar + xz -9e", lambda d: lzma.compress(
        d, preset=9 | lzma.PRESET_EXTREME))
    best = min(best, xe)
    # ZSTD, WITH EACH VARIANT ALLOWED TO FAIL ON ITS OWN.
    #
    # These were in one try block, so when the long-window call failed
    # - its API moved between versions - it took the plain -19 and -22
    # rows down with it AND printed a message saying zstd was not
    # installed, on a machine where it was.
    #
    # A benchmark that quietly drops rows is worse than one that
    # crashes, because a missing row looks like a result.
    try:
        import zstandard as _z
    except ImportError:
        print("  (install zstandard to compare against zstd:"
              "  pip install zstandard)")
        _z = None
    if _z is not None:
        for lvl in (19, 22):
            try:
                v = run(f"tar + zstd -{lvl}",
                        lambda d, L=lvl: _z.ZstdCompressor(level=L).compress(d))
                best = min(best, v)
            except Exception as e:
                print(f"  tar + zstd -{lvl}: failed ({type(e).__name__})")
        try:
            cp = _z.ZstdCompressionParameters.from_level(
                19, window_log=31, enable_ldm=True)
            cc = _z.ZstdCompressor(compression_params=cp)
            v = run("tar + zstd -19 --long=31", lambda d: cc.compress(d))
            best = min(best, v)
        except Exception as e:
            print(f"  tar + zstd -19 --long=31: unavailable "
                  f"({type(e).__name__})")

    # lrzip - the one baseline built for long-range redundancy across
    # many similar files, and therefore the fair test of whether the
    # redundancy here is positional or columnar.
    import shutil as _sh, subprocess as _sp, tempfile as _tf
    if _sh.which("lrzip"):
        try:
            with _tf.TemporaryDirectory() as td:
                src = os.path.join(td, "t.tar")
                open(src, "wb").write(tar)
                t0 = time.perf_counter()
                _sp.run(["lrzip", "-q", "-L", "9", "-o",
                         os.path.join(td, "t.lrz"), src], check=True,
                        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                dt = time.perf_counter() - t0
                sz = os.path.getsize(os.path.join(td, "t.lrz"))
                print(f"  {'lrzip -L9':28s} {sz:>12,} "
                      f"{100 * (1 - sz / orig):>+7.1f}% {dt:>6.2f}s")
                best = min(best, sz)
        except Exception as e:
            print(f"  lrzip: failed ({type(e).__name__})")
    else:
        print("  lrzip: not installed - the one baseline built for this")
        print("         shape of data, worth running before publishing")

    # THE PACK TIME, NOT THE PACK-AND-CHECK TIME.
    #
    # archive() verifies itself by default - it decodes what it just
    # built and compares every file before returning. That is the
    # right default and it is why this tool can be trusted.
    #
    # But timing it that way compares our pack-plus-verify against
    # everyone else's pack, and none of the baselines check
    # themselves. On a 21 MB archive that turned 1.16 seconds into
    # 2.8, and sent two people chasing a regression that was never
    # there.
    t0 = time.perf_counter()
    blob, method = groupcol.archive(blobs, verify=False)
    dt = time.perf_counter() - t0
    t0 = time.perf_counter()
    groupcol.archive(blobs)
    dtv = time.perf_counter() - t0
    print(f"  {'xola-tabular':28s} {len(blob):>12,} "
          f"{100 * (1 - len(blob) / orig):>+7.1f}% {dt:>6.2f}s   ({method})")
    if method != "bundle":
        print(f"  {'':28s} the bundle LOST on this corpus - the number")
        print(f"  {'':28s} above is a plain tar, and this tool added")
        print(f"  {'':28s} nothing. See FINDINGS.md for when that happens.")
    print(f"  {'  and again, self-verified':28s} {'':>12s} {'':>8s} "
          f"{dtv:>6.2f}s   (a feature, not overhead)")

    t0 = time.perf_counter()
    back = groupcol.unarchive(blob)
    dtu = time.perf_counter() - t0
    ok = (len(back) == len(blobs)
          and all(hashlib.sha256(a).digest() == hashlib.sha256(b).digest()
                  for a, b in zip(back, blobs)))

    print("  " + "-" * 60)
    print(f"\n  against the best of the others: "
          f"{100 * (1 - len(blob) / best):+.1f}%")
    print(f"  reading it back: {dtu:.2f}s, {orig / dtu / 1e6:.1f} MB/s")
    print(f"  every file byte for byte identical: {ok}\n")
    if method == "tar":
        print("  Your columns did not have enough in common to bundle, so")
        print("  it fell back to a plain tar. That is the never-worse")
        print("  guarantee doing its job.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
