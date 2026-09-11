# xola-tabular

Compresses archives of CSV, TSV and log files by lining up the columns
across every file, so that a thousand station codes sit together
instead of being scattered through a thousand rows.

**71.9% smaller than the best of seven general-purpose baselines**, and
never worse - it builds a plain tar as well and keeps whichever is
smaller.

## Install this too

    pip install zstandard brotli

Neither is a hard dependency: without them everything works and uses
bzip2 throughout. But it matters more than it sounds. On a real 1,000-file
EPA archive:

    bzip2 alone       341,310 bytes - and the bundle LOSES, so it
                      falls back to a plain tar
    with zstd          84,932 bytes - the bundle wins

Four times better, from letting each group of columns pick its own
coder. bzip2's Burrows-Wheeler transform is strong on short repeated
strings - timestamps, flags, station codes - and zstd wins on almost
everything else. Measured per column on that archive, zstd won 19 of
24.

## Against Parquet

Parquet is the comparison that matters. It is columnar by design, it
codes each column separately, it dictionary-encodes strings and
delta-encodes integers - everything this tool does, from a format
every data lake already uses.

Same 1,000 EPA files, every column read as text so both formats carry
the same information and both can return the original file:

| | bytes | saved |
| --- | --- | --- |
| parquet + snappy | 133,993 | 99.4% |
| parquet + zstd | 113,382 | 99.5% |
| parquet + gzip | 111,919 | 99.5% |
| **parquet + brotli** | **110,022** | **99.5%** |
| **xola-tabular** | **84,932** | **99.6%** |

**22.8% smaller than the best Parquet.**

Two honest things next to that number.

**Parquet is more than ten times faster** - 0.09s against 1.48s. A buyer who
does not care about the last 22% should use Parquet, and most do not.

**And Parquet's TYPED mode could not read this archive at all.**

    ArrowTypeError: Field Qualifier has incompatible types:
                    int64 vs string

The Qualifier column is empty in most of the thousand files, so Arrow
infers int64, and holds text in a few, so those infer as string. A
thousand schemas that will not merge. That is not Parquet being bad -
it is what happens when a format storing typed values meets a decade
of CSVs written by software that did not agree with itself. But it is
an operational cost the columnar pitch does not mention, and it
appeared on the first real archive tried.

`vs_parquet.py` runs this on your own data.

## What it gets

1,000 real EPA air quality files, 21,291,144 bytes:

| method | bytes | saved | time |
| --- | --- | --- | --- |
| tar + gzip -9 | 623,988 | 97.1% | 0.29s |
| tar + bzip2 -9 | 341,309 | 98.4% | 1.05s |
| tar + xz -9 | 446,716 | 97.9% | 0.95s |
| **tar + xz -9e** | **302,684** | 98.6% | 12.80s |
| tar + zstd -19 | 468,931 | 97.8% | 6.77s |
| tar + zstd -22 | 402,909 | 98.1% | 17.70s |
| tar + zstd -19 --long=31 | 469,260 | 97.8% | 7.06s |
| **xola-tabular** | **84,932** | **99.6%** | **1.48s** |

**71.9% smaller than the best of them, and faster than four of the
seven.** xz at its strongest takes ten times longer to produce a file
three and a half times larger.

Round trip verified byte for byte on every file.

Run it on your own data - that is the only measurement that matters to
you:

    python compare.py FOLDER

## Why it works

A CSV column means the same thing in every file. Column 3 is the
station code in all thousand of them. Compress each file separately and
the compressor has to learn that vocabulary a thousand times over.

Line the columns up and it learns once.

Measured separately. **What each step is worth depends entirely on
the data** - that is the whole reason the tool measures rather than
assumes:

| step | instrument data | air quality data |
| --- | --- | --- |
| putting the files in one stream | +14.7% | **+40.1%** |
| **lining the columns up** | **+21.2%** | +2.8% |
| a coder chosen per group | +18.5% | +6.4% |

On instrument data - one station per file, a constant station code,
a smoothly drifting reading - alignment is the big win. On air quality
data, where every row picks a different station, alignment finds
little and simply bundling does the work.

Two more, measured on the corpora where they apply:

| | worth |
| --- | --- |
| a coder chosen per group, on the EPA archive | **4x** |
| delta coding, where numeric columns dominate | +38.8% |

`combos.py` runs this breakdown on your own files.

## The most interesting number is not ours

    tar + zstd -22            402,909
    tar + bzip2 -9            341,309

zstd at its strongest setting loses to bzip2 by 18%. And zstd with a
2 GB window does worse than bzip2 with a 900 KB one.

If the problem were entropy coding or window size, that could not
happen. The problem is row interleaving: a CSV stores rows, the
redundancy lives in columns, and every general-purpose tool sees the
rows.

## Speed

    pack        1.48s     14.4 MB/s
    verify      2.60s     the full round trip
    read back   0.60s     35.4 MB/s

**A note on the timing column.** `archive()` verifies itself by
default - it decodes what it just built and compares every file before
returning. That guarantee caught a float-drift bug that would have
silently corrupted archives, and it should stay on.

But it means the tool does roughly twice the work of the baselines,
none of which check themselves. Reporting that as the compression time
turned a 1.48 second pack into 2.6 and sent two people chasing a
regression that was never there. So the tables report both.

## Using it

```python
import groupcol

blobs = [open(f, "rb").read() for f in my_csv_files]
archive, method = groupcol.archive(blobs)      # "bundle" or "tar"

restored = groupcol.unarchive(archive)
assert restored == blobs
```

`archive()` checks every file before returning. Pass `verify=False`
only if you are going to verify the whole archive yourself afterwards.

## What it will not help with

Photographs, video, HEIC, PDFs, anything already compressed. Those have
had their redundancy removed already, and a second pass makes them
bigger - measured at -0.6%.

Files whose columns genuinely differ from each other. A taxi export
with unrelated columns came out 9.1% WORSE as a bundle, which is why
the tar is always built too and the smaller one kept.

## Honest notes

**This is not a new algorithm.** Columnar storage is Parquet and ORC's
whole premise. Per-column codec selection is already in Parquet. Delta
encoding is one of three steps in Pcodec, and they do it more
thoroughly.

**There are real competitors:** OpenZL from Meta, Pcodec, BtrBlocks
(SIGMOD 2023), Vortex. Pcodec beat us on the one column in the EPA
archive with genuine numeric variation - see `vs_pcodec.py`, which
runs that comparison on your own data.

**lrzip is not evaluated.** It is built for long-range sequential
redundancy, which this workload does not have - the redundancy here is
columnar. `compare.py` will run it if it is installed.

**What is here** is the combination applied to arbitrary CSV archives
as a standalone tool, measured against seven baselines, with the
failures written down in FINDINGS.md.

## The tools in this repo

    groupcol.py     the compressor
    compare.py      seven general-purpose baselines, on your data
    vs_parquet.py   against Parquet, which is the one that matters
    vs_pcodec.py    against Pcodec, on the numeric columns
    ablate.py       what each feature actually costs
    qtest.py        which brotli quality is worth its time
    combos.py       which arrangement wins on your data
    logtest.py      whether the log ideas help on your logs
    split.py        cut one big CSV into many

Every one of them runs on your own files. That is the point - none of
the numbers in this README need to be taken on trust.

## Licence

MIT. Use it, change it, sell it.
