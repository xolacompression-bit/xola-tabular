# What combination wins, and on what

Three stages, each of which can be done several ways. Rather than guess,
`combos.py` runs all of them on the same files and records the answer.

Run it on your own data:

    python combos.py FOLDER --limit 20

**One thing to know before reading its output:** `combos.py` is a
research sweep, not the shipped tool. It will use `cmix` or
`prefilter` if they happen to be importable, and neither is in
`groupcol.py` - both were measured and left out for the reasons in the
cost section below. The shipped tool chooses between bzip2 and zstd.

## The answer is the same on every dataset tried

**Columns aligned across files, with the coder chosen separately for
each group.** (Columns are grouped by shared vocabulary first; the
coder is picked per group, not per individual column.)

| dataset | saved |
| --- | --- |
| 20 instrument CSVs | 90.3% |
| 20 air quality CSVs | 88.4% |
| **1,000 real EPA files** | **99.6%** |

## But WHAT each step is worth changes completely

| step | instrument data | air quality data |
| --- | --- | --- |
| putting them in one stream | +14.7% | **+40.1%** |
| lining the columns up | **+21.2%** | +2.8% |
| a coder chosen per group | +18.5% | +6.4% |

On instrument data - one station per file, a constant station code, a
smoothly drifting reading - column alignment is the big win.

On air quality data, where every row picks a different station, there
is little for alignment to find, and simply putting the files together
does almost all the work.

**This is why a fixed pipeline is the wrong shape.** The same three
steps, applied to two archives of the same size, give their gains in
completely different proportions.

## Which coder wins which column

The shipped tool chooses between **bzip2 and zstd** per group. Measured
column by column on 1,000 real EPA files:

    zstd wins    19 of 24 columns
    bzip2 wins   Time Local, Time GMT, Sample Measurement,
                 Qualifier, Date of Last Change

Every one bzip2 keeps is a short repeated string. Its Burrows-Wheeler
transform is built for exactly that, and zstd wins everything else.

    choosing per group, against bzip2 everywhere     +7.1%
    and on the whole archive, because it turns a
    losing bundle into a winning one                 **4x**

An earlier sweep also tried `cmix` and `prefilter`, which are not in
the shipped tool. `cmix` won several columns and took 15 seconds where
bzip2 takes 0.04; `prefilter` won one column by 8% and took 24
seconds. Both were measured and both were left out - see the cost
section below.

## Delta encoding: evaluated on three corpora, ships anyway

A column of sensor readings drifts. Storing the difference between
consecutive values instead of the values themselves is worth a great
deal when that is true and nothing when it is not.

| corpus | columns where delta is chosen |
| --- | --- |
| instrument data, consistently written | **pm25, pm10** - 2 of 3 numeric columns |
| air quality, random station per row | value |
| EPA archive_test | POC, Latitude, Longitude, MDL |

It fires on all three. What it EARNS is another matter, and the table
further down separates the two - on the EPA archive it fires four
times and the total does not move.

The columns where it declines do so for a reason worth naming: the EPA
Sample Measurement column mixes decimal formats, so no scale returns
the original text.

**Two traps found while building it, both caught by the round-trip
check rather than by reasoning.**

Floats drift. Subtracting them looks exact; adding the differences
back does not. A column that said 12.35 returns as 12.349999999999997,
silently. So every value becomes a scaled integer first - 12.35 to
1235 - and integer arithmetic does not drift.

And real numeric columns are textually inconsistent. The EPA Sample
Measurement column holds 6.7 and 6 in the same column: 3,316 values
with one decimal, 384 with none. No single scale returns both as
themselves - one decimal pads the second, none truncates the first.
The number survives and the file does not.

A per-value flag saying which format each value used was measured and
rejected: 5.0% WORSE, because the flags cost more than the differences
save, and it still did not reconstruct exactly.

So the transform declines unless every value in the column is written
the same way, and verifies the whole column before accepting itself.

**Where the value actually is.** Delta fires on all three corpora,
but what it earns is not evenly spread:

| corpus | columns using delta | worth |
| --- | --- | --- |
| uniform instrument | pm25, pm10 | **38.8% better than tar+bzip2** |
| air quality | value | small |
| EPA archive_test | POC, Latitude, Longitude, MDL | **nothing measurable** |

On the EPA archive the total is 85,186 bytes with delta and 85,186
without. It fires four times and pays nothing, because those four
columns were already cheap.

So the honest sentence is not "validated on two corpora". It is:
**delta's value concentrates where numeric columns dominate the
bundle, and it fires for free everywhere else.**

**A methods note, learned the hard way twice.** A 200-file spot check
of this corpus reported that delta declined on every column. The full
1,000-file run says it wins on four. The sample did not span the
corpus, so it gave a false verdict - which is the same mistake that
nearly broke the format itself, when a 512-value sample missed a
decimal-place change halfway through the archive.

Column verdicts must be computed across the whole corpus. A diagnostic
that samples is a diagnostic that can lie.

**The cost of asking.** Checking for uniformity is a pass over every
value, which is expensive on columns that then decline. The check is
asymmetric and exploits that: a mixed SAMPLE proves a mixed column, so
it declines immediately. A uniform sample proves nothing - one file
changing format halfway through the archive is exactly the case that
first broke this - so the full scan still runs. Mixed declines cheap,
uniform pays to be sure.

## What this costs

`cmix` is slow - about 15 seconds where bzip2 takes 0.04 on the same
data. On a column stream that is acceptable because the streams are
small and it is only chosen where it wins. On a whole archive it is
not: `cmix` on a 2.2 MB tar took 170 seconds and still lost to column
alignment by 29%.

`prefilter` is left out of the sweep. It won once, on a smooth sensor
column, by 8% - and took 24 seconds against `cmix`'s 0.75.

## What does NOT work, measured

Compressing something twice. Every attempt lost:

    the aligned bundle, then cmix over it      +0.1%
    our JPEG output through bzip2              -0.60%
    our JPEG output through groupcol           -0.58%
    our JPEG output transposed across files    -0.69%

The gain comes from choosing the right coder for each PART, never from
running two coders over the same part.
