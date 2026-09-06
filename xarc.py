"""xarc.py — turn a directory of same-schema files into a compressed archive.

WHY THIS EXISTS

groupcol.archive() takes a list of file CONTENTS in memory:

    archive([open(f, "rb").read() for f in files])

That is fine for a demo and useless for an archive. Measured, it uses
about 20x the input size in RAM - a 10 GB bundle would want 200 GB, and
a petabyte cannot be expressed at all. Until this module existed, the
largest thing the tool had ever processed was 2.5 MB.

WHAT THIS DOES INSTEAD

Reads a directory in batches. Each batch becomes one self-contained
bundle; bundles are written to an output file one after another with an
index at the end. Memory is bounded by the batch, not the archive.

    python xarc.py pack   ARCHIVE.xarc  DIRECTORY [--batch 500]
    python xarc.py list   ARCHIVE.xarc
    python xarc.py get    ARCHIVE.xarc  NAME > file
    python xarc.py unpack ARCHIVE.xarc  DIRECTORY
    python xarc.py verify ARCHIVE.xarc  DIRECTORY

THE COST OF BATCHING, AND IT IS REAL

Splitting an archive into bundles loses ratio, because a bundle can only
exploit repetition it can see. Measured on one real dataset:

    1 bundle of 256 files    124,758
    2 bundles of 128         134,297   -7.6%
    4 bundles of 64          146,319  -17.3%

So the batch should be as large as memory allows, not as small as
convenient. The default of 500 files is a guess at a machine with a few
gigabytes free; `--batch` exists because that guess will be wrong for
somebody.

WHAT IS STILL NOT HERE

No parallel batches - one bundle at a time, though the columns inside
each are threaded. No resume after a crash. No update-in-place; adding a
file means repacking. These are honest gaps, not oversights, and they
matter before anyone runs this on something they cannot afford to lose.
"""

import sys
import os
import io
import time
import json
import struct
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import groupcol

MAGIC = b"XARC0001"


def _human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def _batches(names, root, batch, max_bytes):
    """Yield lists of (name, bytes), bounded by count AND by size.

    The count alone is not enough: 500 files of 2 KB and 500 files of
    200 MB are very different demands on a machine."""
    cur, size = [], 0
    for nm in names:
        p = os.path.join(root, nm)
        try:
            b = open(p, "rb").read()
        except OSError:
            continue
        if cur and (len(cur) >= batch or size + len(b) > max_bytes):
            yield cur
            cur, size = [], 0
        cur.append((nm, b))
        size += len(b)
    if cur:
        yield cur


def pack(out_path, root, batch=500, max_bytes=512 * 1024 * 1024, quiet=False):
    names = sorted(f for f in os.listdir(root)
                   if os.path.isfile(os.path.join(root, f)))
    if not names:
        print(f"  nothing to pack in {root}")
        return 1

    index = []
    raw_total = 0
    t0 = time.perf_counter()
    with open(out_path, "wb") as out:
        out.write(MAGIC)
        out.write(struct.pack(">Q", 0))          # index offset, filled later
        for bi, group in enumerate(_batches(names, root, batch, max_bytes)):
            blobs = [b for _, b in group]
            raw_total += sum(len(b) for b in blobs)
            body, route = groupcol.archive(blobs)
            off = out.tell()
            out.write(body)
            index.append({"offset": off, "length": len(body), "route": route,
                          "names": [n for n, _ in group]})
            if not quiet:
                done = sum(len(x["names"]) for x in index)
                print(f"  bundle {bi + 1}: {len(group)} files, "
                      f"{_human(len(body))}, {route}   ({done}/{len(names)})")
        idx_off = out.tell()
        payload = json.dumps(index).encode()
        out.write(struct.pack(">I", len(payload)))
        out.write(payload)
        out.seek(len(MAGIC))
        out.write(struct.pack(">Q", idx_off))

    dt = time.perf_counter() - t0
    size = os.path.getsize(out_path)
    print()
    print(f"  {len(names)} files, {_human(raw_total)} -> {_human(size)} "
          f"({100 * size / max(1, raw_total):.2f}%)")
    print(f"  {len(index)} bundles, {dt:.1f}s, "
          f"{raw_total / dt / 1e6:.2f} MB/s")
    return 0


def _read_index(path):
    with open(path, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise ValueError("not an xarc archive")
        idx_off, = struct.unpack(">Q", f.read(8))
        if idx_off == 0:
            raise ValueError("archive is incomplete - packing did not finish")
        f.seek(idx_off)
        n, = struct.unpack(">I", f.read(4))
        return json.loads(f.read(n))


def list_files(path):
    index = _read_index(path)
    total = 0
    for b in index:
        for n in b["names"]:
            print(f"  {n}")
            total += 1
    print(f"\n  {total} files in {len(index)} bundles")
    return 0


def get(path, name, out=None):
    """Pull one file out without decompressing the rest.

    Only the bundle containing it is read and decoded - the others are
    never touched. Within a bundle every column is decoded, which is
    the cost recorded in groupcol.extract_one."""
    index = _read_index(path)
    for b in index:
        if name in b["names"]:
            i = b["names"].index(name)
            with open(path, "rb") as f:
                f.seek(b["offset"])
                body = f.read(b["length"])
            if b["route"] == "bundle":
                inner = groupcol.unpack(body[1:])
                data = groupcol.extract_one(inner, i)
            else:
                data = groupcol.unarchive(body)[i]
            if out:
                open(out, "wb").write(data)
                print(f"  wrote {out} ({len(data):,} bytes)")
            else:
                sys.stdout.buffer.write(data)
            return 0
    print(f"  {name} is not in this archive")
    return 1


def unpack(path, root):
    os.makedirs(root, exist_ok=True)
    index = _read_index(path)
    n = 0
    with open(path, "rb") as f:
        for b in index:
            f.seek(b["offset"])
            blobs = groupcol.unarchive(f.read(b["length"]))
            for nm, data in zip(b["names"], blobs):
                open(os.path.join(root, nm), "wb").write(data)
                n += 1
    print(f"  wrote {n} files to {root}")
    return 0


def verify(path, root):
    """Every file must come back byte for byte. Checked by SHA-256.

    This is the test that matters before anyone trusts an archive with
    data they cannot re-download."""
    index = _read_index(path)
    bad = 0
    checked = 0
    t0 = time.perf_counter()
    with open(path, "rb") as f:
        for b in index:
            f.seek(b["offset"])
            blobs = groupcol.unarchive(f.read(b["length"]))
            for nm, data in zip(b["names"], blobs):
                original = open(os.path.join(root, nm), "rb").read()
                if hashlib.sha256(data).digest() != hashlib.sha256(original).digest():
                    print(f"  MISMATCH  {nm}")
                    bad += 1
                checked += 1
    dt = time.perf_counter() - t0
    print(f"\n  {checked} files checked in {dt:.1f}s, "
          f"{'all exact' if not bad else str(bad) + ' WRONG'}")
    return 1 if bad else 0


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = a[0]
    try:
        if cmd == "pack":
            batch = 500
            mb = 512
            if "--batch" in a:
                batch = int(a[a.index("--batch") + 1])
            if "--max-mb" in a:
                mb = int(a[a.index("--max-mb") + 1])
            return pack(a[1], a[2], batch, mb * 1024 * 1024)
        if cmd == "list":
            return list_files(a[1])
        if cmd == "get":
            return get(a[1], a[2], a[3] if len(a) > 3 else None)
        if cmd == "unpack":
            return unpack(a[1], a[2])
        if cmd == "verify":
            return verify(a[1], a[2])
    except IndexError:
        print(__doc__)
        return 1
    print(f"  unknown command: {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
