"""split.py — cut one big CSV into many, the way an archive holds it.

    python split.py BIG.csv OUTFOLDER [rows-per-file]

Column alignment works ACROSS files. A single 20 MB CSV gives it
nothing to align, because there is only one of everything. Real
archives do not look like that - they hold one file per day, or per
station, or per run, and that is where the gain lives.

So this splits a large export the way it would actually have been
stored, which makes the comparison honest rather than flattering in
either direction.
"""

import sys
import os


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    src = sys.argv[1]
    dest = sys.argv[2]
    rows = int(sys.argv[3]) if len(sys.argv) > 3 else 500

    os.makedirs(dest, exist_ok=True)
    for old in os.listdir(dest):
        if old.endswith(".csv"):
            os.remove(os.path.join(dest, old))

    with open(src, encoding="utf-8", errors="replace") as fh:
        header = fh.readline().rstrip("\n")
        n = 0
        part = 0
        out = None
        for line in fh:
            if n % rows == 0:
                if out:
                    out.close()
                out = open(os.path.join(dest, f"part{part:05d}.csv"), "w",
                           encoding="utf-8")
                out.write(header + "\n")
                part += 1
            out.write(line)
            n += 1
        if out:
            out.close()

    total = sum(os.path.getsize(os.path.join(dest, f))
                for f in os.listdir(dest))
    print(f"\n  {n:,} rows -> {part} files, {total:,} bytes")
    print(f"  in {dest}\n")
    print("  now run:")
    print(f"      python compare.py {dest}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
