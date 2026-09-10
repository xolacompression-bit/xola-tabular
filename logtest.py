"""logtest.py — do the log fixes help on REAL logs?

    python logtest.py FOLDER

Two ideas, both measured on invented log data first and both looking
good there:

    hex identifiers stored as bytes rather than text     +8.5%
    timestamps stored as differences                     +6.8%
    together, on a synthetic archive                    +10.5%

Then the same measurement on genuine Windows logs gave a very
different starting point: plain bzip2 already reaches 96.9%, against
86.1% on the invented ones. The synthetic logs carried a random trace
id on every line; the real ones do not.

So this checks whether the fixes survive contact with logs neither of
us wrote. If they do not, that is worth knowing before any of it is
built.
"""

import sys
import os
import io
import re
import bz2
import struct
import tarfile
import binascii

HEX = re.compile(rb"\b[0-9a-fA-F]{16,}\b")
TS = re.compile(rb"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]\d{1,7}\b")


def as_tar(blobs):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for i, b in enumerate(blobs):
            ti = tarfile.TarInfo(f"{i:04d}")
            ti.size = len(b)
            t.addfile(ti, io.BytesIO(b))
    return buf.getvalue()


def split_out(data):
    """Pull the hex runs and timestamps into their own streams.

    What is left keeps a marker where each one was, so the line can be
    put back together exactly."""
    hexes = []
    times = []

    def take_hex(m):
        s = m.group(0)
        if len(s) % 2:
            return s
        try:
            hexes.append(binascii.unhexlify(s))
        except Exception:
            return s
        return b"\x01H\x01"

    def take_ts(m):
        times.append(m.group(0))
        return b"\x01T\x01"

    body = TS.sub(take_ts, data)
    body = HEX.sub(take_hex, body)
    return body, b"".join(hexes), times


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else "."
    cap = int(sys.argv[sys.argv.index("--cap") + 1]) \
        if "--cap" in sys.argv else 5_000_000

    files = sorted(f for f in os.listdir(folder)
                   if f.lower().endswith((".log", ".txt")))
    if not files:
        print(f"\n  no .log files in {folder}\n")
        return 1
    blobs = [open(os.path.join(folder, f), "rb").read()[:cap]
             for f in files]
    orig = sum(len(b) for b in blobs)
    print(f"\n  {len(blobs)} log files, {orig:,} bytes "
          f"(first {cap:,} of each)\n")

    base = len(bz2.compress(as_tar(blobs), 9))
    print(f"  {'tar + bzip2 (the baseline)':44s} {base:>10,} "
          f"{100 * (1 - base / orig):>+7.1f}%")

    bodies, allhex, alltimes = [], [], []
    for b in blobs:
        body, hx, ts = split_out(b)
        bodies.append(body)
        allhex.append(hx)
        alltimes.extend(ts)

    hexblob = b"".join(allhex)
    print(f"\n    hex runs found:    {len(hexblob):,} bytes as raw bytes")
    print(f"    timestamps found:  {len(alltimes):,}")

    parts = [as_tar(bodies), hexblob]

    # timestamps as differences, when they parse
    tsbytes = b"\n".join(alltimes)
    packed = None
    try:
        import datetime
        ms = []
        for t in alltimes:
            s = t.decode().replace(",", ".").replace(" ", "T")
            d = datetime.datetime.strptime(s[:23], "%Y-%m-%dT%H:%M:%S.%f")
            ms.append(int(d.timestamp() * 1000))
        diffs = [ms[0]] + [ms[i] - ms[i - 1] for i in range(1, len(ms))]
        packed = b"".join(struct.pack("<q", x) for x in diffs)
    except Exception:
        packed = None

    a = len(bz2.compress(parts[0], 9))
    b_ = len(bz2.compress(parts[1], 9)) if hexblob else 0
    c_text = len(bz2.compress(tsbytes, 9)) if alltimes else 0
    c_diff = len(bz2.compress(packed, 9)) if packed else None

    print(f"\n  {'what is stored':44s} {'bytes':>10s}")
    print("  " + "-" * 58)
    print(f"  {'the lines with markers where they were':44s} {a:>10,}")
    print(f"  {'the hex runs, as bytes':44s} {b_:>10,}")
    print(f"  {'the timestamps, as text':44s} {c_text:>10,}")
    if c_diff is not None:
        print(f"  {'the timestamps, as differences':44s} {c_diff:>10,}")
    best_ts = min(x for x in (c_text, c_diff) if x is not None) \
        if alltimes else 0
    total = a + b_ + best_ts
    print("  " + "-" * 58)
    print(f"  {'total':44s} {total:>10,} "
          f"{100 * (1 - total / orig):>+7.1f}%")
    print(f"\n    against tar + bzip2: {100 * (1 - total / base):+.1f}%\n")
    if total >= base:
        print("  The fixes do not help on these logs. On the invented ones")
        print("  they gave 10.5%, because every line there carried a random")
        print("  trace id. These logs do not work that way.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
