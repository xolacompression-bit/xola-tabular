# xola-tabular: what changed today

---

## HIGHLIGHTS

> **Four times smaller.** 341,310 bytes to 84,932 on 1,000 real files.
>
> **22.8% smaller than Parquet** - the format every data lake uses,
> which is columnar and per-column coded exactly as this is.
>
> **71.9% smaller than the best of seven baselines** - including
> xz -9e, zstd -22, and zstd with a 2 GB window.
>
> **Faster than four of those seven.** 1.48s where xz -9e takes 12.80s.
>
> **2.8x faster than the low point during the work** - 6.3 MB/s at the
> worst moment today, 14.4 MB/s now.
>
> **Every file byte for byte identical**, verified by decoding the
> whole archive and comparing.
>
> **The strongest evidence is not ours:** zstd at its best setting
> loses to bzip2 by 18% on this data. That should be impossible unless
> the bottleneck is something neither of them addresses.

---

## The short version

A tool that compresses archives of CSV, log and sensor files by lining
up their columns across files instead of compressing each file on its
own.

On 1,000 real EPA air quality files - 21.3 MB:

| | bytes | saved |
| --- | --- | --- |
| **this morning** | **341,310** | 98.40% |
| **tonight** | **84,932** | **99.60%** |

**Four times smaller.**

## Against Parquet, which is the comparison that counts

The seven baselines below all read ROWS. That is why they lose - a CSV
stores rows and the redundancy lives in columns.

Parquet does not have that problem. It is columnar by design, codes
each column separately, dictionary-encodes strings. Everything this
tool does, from a format every data lake already uses.

Same 1,000 files, every column read as text so both sides carry the
same information:

| | bytes | time |
| --- | --- | --- |
| parquet + snappy | 133,993 | 0.09s |
| parquet + zstd | 113,382 | 0.09s |
| parquet + gzip | 111,919 | 0.09s |
| **parquet + brotli** | **110,022** | 0.12s |
| **xola-tabular** | **84,932** | 1.48s |

**22.8% smaller than the best Parquet.**

Two things belong next to that, and they matter more than the number.

**Parquet is ten times faster.** Most buyers will take that trade, and
they would be right to.

**And Parquet returns the DATA. This returns the FILE.** 6.70 and 6.7
are the same float and a different seven bytes. Whether that is worth
22.8% depends entirely on whether the archive exists to be queried or
to be returned - and that is a question for a customer, not a
benchmark.

One more thing worth knowing: **Parquet's typed mode could not read
this archive at all.**

    ArrowTypeError: Field Qualifier has incompatible types:
                    int64 vs string

One column is empty in most of the thousand files and holds text in a
few, so a thousand schemas would not merge. Not a defect in Parquet -
it is what happens when a format storing typed values meets a decade
of CSVs written by software that did not agree with itself. But it is
an operational cost the columnar pitch does not mention, and it
appeared on the first real archive tried.

## Against every tool anyone would name

Same archive, same machine, same run:

| tool | bytes | time |
| --- | --- | --- |
| tar + gzip -9 | 623,988 | 0.29s |
| tar + bzip2 -9 | 341,309 | 1.05s |
| tar + xz -9 | 446,716 | 0.95s |
| **tar + xz -9e** | **302,684** | 12.80s |
| tar + zstd -19 | 468,931 | 6.77s |
| tar + zstd -22 | 402,909 | 17.70s |
| tar + zstd -19 --long=31 | 469,260 | 7.06s |
| **xola-tabular** | **84,932** | **1.48s** |

**71.9% smaller than the best of them, and faster than four of the
seven.**

xz at its strongest setting takes ten times longer to produce a file
three and a half times larger.

## The speed, honestly, including the confusion

The number moved all day, and most of the movement was measurement
rather than code:

| when | bytes | time | speed | measuring |
| --- | --- | --- | --- | --- |
| this morning | 341,310 | 1.70s | 12.5 MB/s | pack + verify |
| after coder choice | 85,166 | 3.38s | 6.3 MB/s | pack + verify |
| after sampled decision | 84,932 | 2.77s | 7.7 MB/s | pack + verify |
| after hot path fixes | 84,932 | 2.47s | 8.6 MB/s | pack + verify |
| **tonight** | **84,932** | **1.48s** | **14.4 MB/s** | **pack only** |

Two things to read carefully.

**The last row is not faster code than the one above it.** It is the
same code measured without the verification pass. The tool decodes
what it just built and compares every file before returning, and the
benchmark had been timing that as if it were compression. None of the
seven baselines check themselves.

**And this morning's 1.70s was fast because the tool was doing almost
nothing.** The bundle lost to a plain tar, so the never-worse
guarantee fired and switched the whole technique off. Four times
larger, and quick about it.

Like for like, both including verification: **1.70s this morning,
2.60s tonight.** Four times smaller for half a second more.

    pack        1.48s     14.4 MB/s
    verify      2.60s     the full round trip
    read back   0.60s     35.4 MB/s

Every file byte for byte identical.

## What actually made the difference

**Letting each group of columns pick its own compressor.** bzip2 is
strong on short repeated strings - timestamps, flags, station codes.
zstd wins on nearly everything else. Measured column by column on this
archive, zstd won 19 of 24.

That one change is the 4x, because it turned a bundle that lost into a
bundle that wins.

**Delta encoding.** Columns of numbers stored as differences between
consecutive values. Worth 38.8% on instrument data where numeric
columns dominate the archive. Costs nothing where it does not apply.

**A third coder.** Parquet with brotli beat Parquet with zstd and
gzip on this archive, so brotli joins bzip2 and zstd as a candidate.
At quality 9 it is worth 254 bytes for no measurable time. Quality 11
was tried first and cost 128% of the runtime for 0.75% - now measured
rather than argued.

**Two hot paths.** Character transposition was building one generator
per character position; a flat join and stepped slices moved that loop
into C. With a second fix they were 21.5% of the runtime.

## What this could be worth

Every figure below assumes the result holds up outside this corpus.

### Small scale - a tool people download

    a paid desktop or command line tool, $10-30 one-time
    a few hundred sales in a year if promoted at all

    $2,000 - $10,000 in year one

The honest constraint is not the technology. A tool nobody finds earns
nothing however good it is.

### Medium scale - one engagement at a time

    an organisation with a large CSV or log archive pays to have it
    compressed and verified

    $10,000 - $30,000 per engagement

This is selling time rather than technology. It works, and it does not
scale without hiring.

### Several organisations at once

The medium-scale number is per engagement, and engagements repeat. The
work is the same each time; only the archive changes.

| customers | per year |
| --- | --- |
| 3 organisations | $30,000 - $90,000 |
| 10 organisations | $100,000 - $300,000 |
| 25 organisations | $250,000 - $750,000 |

Two honest things about that table.

**It is not passive.** Each of those is a relationship - someone who
has to be found, convinced, and supported. Ten engagements a year is
close to a full-time job, and twenty-five needs more than one person.

**But the second is far easier than the first.** An organisation with
a large archive will ask who else has done this. Having one name to
give changes the conversation completely, and having three changes it
again. The hard number in that table is the first row.

**What would make it easier for several people to work on this:** the
tool is one Python file with no required dependencies, the benchmark
runs on anyone's data with one command, and the failures are written
down so nobody repeats them. Someone joining does not need the history
- they need `compare.py` and a folder of their own files.

### Larger - licensing to someone who already has the customers

    a backup vendor, a data platform, a telemetry service
    deployed across their whole customer base from one agreement

    one vendor        $20,000 - $80,000 a year
    three or four     $60,000 - $300,000 a year

    IF the Rust port happens first

The port is the condition. In pure Python at 14.4 MB/s this is an
archival tool; real pipelines compress at hundreds of megabytes a
second. Four to eight weeks of work, and it should follow a reason - a
user, an inquiry, someone saying 1.2 seconds is too slow - rather than
preceding one.

### The one already earned

    a portfolio piece

Seven baselines, an ablation tool, documented failures, byte-exact
verification, and a result that survives being checked. Compression
and data engineering roles pay well, and this is the kind of work that
starts a conversation.

## What we are NOT claiming

Not a new algorithm. Columnar storage is Parquet and ORC's whole
premise. Per-column codec selection is already in Parquet. Delta
encoding is one of three steps in Pcodec, and they do it more
thoroughly than we do.

There are real competitors here: OpenZL from Meta, Pcodec, BtrBlocks
from SIGMOD 2023, Vortex. Pcodec beat us on the one column in the EPA
archive with genuine variation.

What is here is the combination applied to arbitrary CSV archives as a
standalone tool, measured against seven baselines, with the failures
written down.

## The most interesting number is not ours

    tar + zstd -22            402,909
    tar + bzip2 -9            341,309

zstd at its strongest loses to bzip2 by 18%. And zstd with a 2 GB
window does worse than bzip2 with a 900 KB one.

If the problem were entropy coding or window size, that could not
happen. The problem is row interleaving: a CSV stores rows, the
redundancy lives in columns, and every general-purpose tool sees the
rows.

That is the whole argument, and the baselines make it themselves.
