"""vs_parquet.py — the comparison that decides whether any of this is a
product.

    pip install pyarrow zstandard
    python vs_parquet.py FOLDER_OF_CSVS

WHY THIS ONE MATTERS MORE THAN THE OTHER SEVEN BASELINES
--------------------------------------------------------
gzip, bzip2, xz and zstd all see rows. That is why they lose - a CSV
stores rows and the redundancy lives in columns, so a general-purpose
tool is reading the data the wrong way round.

**Parquet does not have that problem.** It is columnar by design, it
applies a codec per column, it dictionary-encodes low-cardinality
strings, and it delta-encodes integers. Everything this tool does,
Parquet already does, from a format used by every data lake in the
world.

So this is the honest test. Beating xz proves the premise. Beating
Parquet proves the product.

**And the failure mode is specific:** if Parquet with zstd lands
within about 20% of us, there is no product here - the pitch collapses
into a feature request for someone's existing stack. Better to know
that now than after writing a C core.

WHAT IS AND IS NOT A FAIR COMPARISON
------------------------------------
Parquet stores TYPED VALUES. This tool stores the original TEXT and
returns it byte for byte.

That difference cuts both ways and the script reports both, because
hiding it would be the kind of benchmark that gets a project dismissed:

    round trip to bytes    can the original CSV be reproduced exactly?
                           we can; Parquet cannot, because "6.70"
                           and "6.7" are the same float

    round trip to values   can the NUMBERS be recovered?
                           both can, and this is the comparison
                           Parquet was designed for

If the archive exists to be queried, Parquet's answer is the right
one and byte-exactness is a cost rather than a feature. If it exists
to satisfy a regulator who wants the original file back, it is the
other way round.

Say which one the customer needs before quoting either number.
"""

import sys
import os
import io
import time
import bz2
import lzma
import tarfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ".")

try:
    import pyarrow as pa
    import pyarrow.csv as pacsv
    import pyarrow.parquet as pq
except ImportError:
    print("\n  needs:  pip install pyarrow\n")
    raise SystemExit(1)

import groupcol


def load(folder, limit=2000):
    files = sorted(f for f in os.listdir(folder)
                   if f.lower().endswith((".csv", ".tsv")))[:limit]
    if not files:
        return None, None
    paths = [os.path.join(folder, f) for f in files]
    blobs = [open(p, "rb").read() for p in paths]
    return paths, blobs


def as_one_table_strings(paths, verbose=True):
    """Every file read with every column as TEXT.

    WHY THIS IS THE FAIR COMPARISON AND THE TYPED ONE IS NOT.
    ---------------------------------------------------------
    Arrow infers a type per column per file. On a real EPA archive
    that fails outright: the Qualifier column is empty in most files,
    so it is inferred as int64, and holds text in a few, so those are
    inferred as string. A thousand schemas that will not merge.

        ArrowTypeError: Field Qualifier has incompatible types:
                        int64 vs string

    That is not Parquet being bad. It is what happens when a format
    that stores TYPED VALUES meets a thousand CSVs written over
    several years by software that did not agree with itself.

    Reading everything as text sidesteps it, and it is also the
    like-for-like test: this tool stores the original text, so Parquet
    storing text is the comparison where both sides carry the same
    information and both can return the original file.

    The typed version is measured separately. It is smaller and it is
    lossy - 6.70 comes back as 6.7 - so the two numbers answer
    different questions and both are reported."""
    tables = []
    for p in paths:
        try:
            t = pacsv.read_csv(p)
            opts = pacsv.ConvertOptions(
                column_types={n: pa.string() for n in t.schema.names})
            tables.append(pacsv.read_csv(p, convert_options=opts))
        except Exception as e:
            if verbose:
                print(f"  parquet: {os.path.basename(p)} would not read "
                      f"({type(e).__name__}: {str(e)[:80]})")
            return None
    if not tables:
        return None
    try:
        return pa.concat_tables(tables, promote_options="permissive")
    except TypeError:
        pass
    except Exception:
        pass
    try:
        schema = pa.unify_schemas([t.schema for t in tables])
        return pa.concat_tables([t.cast(schema) for t in tables])
    except Exception as e:
        if verbose:
            print(f"  parquet: schemas will not unify even as text "
                  f"({type(e).__name__}: {str(e)[:80]})")
        return None


def as_one_table(paths, verbose=True):
    """Every file read into one Arrow table, which is what a data lake
    would actually do - one Parquet file per archive, not per CSV.

    THIS USED TO FAIL SILENTLY AND THAT WAS WORSE THAN FAILING.
    -----------------------------------------------------------
    Any exception returned None and the benchmark printed "could not
    parse these CSVs" - which reads like the data is malformed when
    the real cause might be one file with a different column, or a
    schema that needs unifying, or an Arrow version without
    promote_options.

    A benchmark that cannot say why a competitor was excluded is not a
    benchmark. So every failure is reported."""
    tables = []
    for p in paths:
        try:
            tables.append(pacsv.read_csv(p))
        except Exception as e:
            if verbose:
                print(f"  parquet: {os.path.basename(p)} would not read "
                      f"({type(e).__name__}: {str(e)[:90]})")
            return None
    if not tables:
        return None
    # schemas may differ between files - unify before concatenating,
    # which is what a data lake does when a column is added midway
    try:
        return pa.concat_tables(tables, promote_options="permissive")
    except TypeError:
        pass
    except Exception as e:
        if verbose:
            print(f"  parquet: permissive concat failed "
                  f"({type(e).__name__}: {str(e)[:90]})")
    try:
        return pa.concat_tables(tables, promote=True)
    except Exception:
        pass
    try:
        schema = pa.unify_schemas([t.schema for t in tables])
        return pa.concat_tables([t.cast(schema) for t in tables])
    except Exception as e:
        if verbose:
            print(f"  parquet: could not unify {len(tables)} schemas "
                  f"({type(e).__name__}: {str(e)[:90]})")
    try:
        return pa.concat_tables(tables)
    except Exception as e:
        if verbose:
            print(f"  parquet: concat failed "
                  f"({type(e).__name__}: {str(e)[:90]})")
        return None


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else "."
    paths, blobs = load(folder)
    if not paths:
        print(f"\n  no CSV files in {folder}\n")
        return 1
    orig = sum(len(b) for b in blobs)
    print(f"\n  {len(paths)} files, {orig:,} bytes\n")

    print(f"  {'method':34s} {'bytes':>12s} {'saved':>8s} {'time':>7s} "
          f"{'exact bytes':>12s}")
    print("  " + "-" * 80)

    # the general-purpose reference points
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for i, b in enumerate(blobs):
            ti = tarfile.TarInfo(f"{i:06d}.csv")
            ti.size = len(b)
            t.addfile(ti, io.BytesIO(b))
    tar = buf.getvalue()

    t0 = time.perf_counter()
    v = len(bz2.compress(tar, 9))
    print(f"  {'tar + bzip2 -9':34s} {v:>12,} "
          f"{100 * (1 - v / orig):>+7.1f}% {time.perf_counter() - t0:>6.2f}s "
          f"{'yes':>12s}")
    best_general = v

    t0 = time.perf_counter()
    v = len(lzma.compress(tar, preset=9 | lzma.PRESET_EXTREME))
    print(f"  {'tar + xz -9e':34s} {v:>12,} "
          f"{100 * (1 - v / orig):>+7.1f}% {time.perf_counter() - t0:>6.2f}s "
          f"{'yes':>12s}")
    best_general = min(best_general, v)

    # Parquet, the one that competes on design rather than on effort.
    # Text first, because that is the like-for-like comparison.
    parquet_best = None
    table = as_one_table_strings(paths)
    if table is None:
        print(f"  {'parquet (as text)':34s} could not read these CSVs")
    else:
        for codec, label in (("snappy", "parquet text + snappy"),
                             ("gzip", "parquet text + gzip"),
                             ("zstd", "parquet text + zstd"),
                             ("brotli", "parquet text + brotli")):
            out = io.BytesIO()
            t0 = time.perf_counter()
            try:
                pq.write_table(table, out, compression=codec,
                               use_dictionary=True,
                               data_page_version="2.0")
            except Exception as e:
                print(f"  {label:34s} failed ({type(e).__name__})")
                continue
            dt = time.perf_counter() - t0
            n = len(out.getvalue())
            if parquet_best is None or n < parquet_best:
                parquet_best = n
            print(f"  {label:34s} {n:>12,} {100 * (1 - n / orig):>+7.1f}% "
                  f"{dt:>6.2f}s {'yes':>12s}")

    # and the typed version, which is Parquet at its strongest and
    # cannot return the original file
    typed = as_one_table(paths, verbose=False)
    if typed is None:
        print(f"  {'parquet (typed)':34s} schemas will not merge - "
              f"see above")
    else:
        for codec, label in (("zstd", "parquet typed + zstd"),
                             ("brotli", "parquet typed + brotli")):
            out = io.BytesIO()
            t0 = time.perf_counter()
            try:
                pq.write_table(typed, out, compression=codec,
                               use_dictionary=True,
                               data_page_version="2.0")
            except Exception:
                continue
            dt = time.perf_counter() - t0
            n = len(out.getvalue())
            print(f"  {label:34s} {n:>12,} "
                  f"{100 * (1 - n / orig):>+7.1f}% {dt:>6.2f}s "
                  f"{'NO':>12s}")

    # ours
    t0 = time.perf_counter()
    blob, method = groupcol.archive(blobs, verify=False)
    dt = time.perf_counter() - t0
    back = groupcol.unarchive(blob)
    exact = back == blobs
    print(f"  {'xola-tabular':34s} {len(blob):>12,} "
          f"{100 * (1 - len(blob) / orig):>+7.1f}% {dt:>6.2f}s "
          f"{str(exact):>12s}   ({method})")
    print("  " + "-" * 80)

    print()
    print(f"  against the best general tool:  "
          f"{100 * (1 - len(blob) / best_general):+.1f}%")
    if parquet_best:
        margin = 100 * (1 - len(blob) / parquet_best)
        print(f"  against the best Parquet:      {margin:+.1f}%")
        print()
        if margin > 20:
            print("  Clear of Parquet by more than 20%. That is a product")
            print("  claim - Parquet is columnar, per-column coded and")
            print("  dictionary encoded, and it is still behind.")
        elif margin > 0:
            print("  Ahead of Parquet, but by less than 20%. That is a")
            print("  feature request for someone's existing stack, not a")
            print("  product. Worth knowing before writing a C core.")
        else:
            print("  Parquet wins. The right response is to say so, and")
            print("  to ask what this does that Parquet does not -")
            print("  byte-exact reconstruction is the honest answer, and")
            print("  it is worth something only to a customer who needs")
            print("  the original file back rather than the data in it.")
    print()
    print("  A caveat that belongs next to any number above: Parquet")
    print("  stores typed values and cannot return the original CSV")
    print("  byte for byte - 6.70 and 6.7 are the same float. Whether")
    print("  that matters depends entirely on whether the archive")
    print("  exists to be queried or to be returned.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
