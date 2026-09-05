# Lossless compression for archived tabular data

Compresses CSV and similar tabular files 24-72% smaller than Parquet and
51% smaller than ORC. Returns the original file byte for byte, which
Parquet and ORC do not.

All numbers below were measured on public datasets. The script that
produces them is in this repository and runs on your own files.

Related: [xola-compression](https://github.com/xolacompression-bit/xola-compression),
a codec for seismic waveforms that is 21.5-34.8% smaller than Steim-2.
Same approach, different data.

---

## Results

| dataset | original | Parquet | ORC | this tool |
|---|---:|---:|---:|---:|
| EPA air quality | 20,971,464 | 118,952 | 170,890 | **75,411** |
| NOAA weather | 10,371,489 | 765,084 | — | **455,048** |
| NOAA weather | 6,792,888 | 493,225 | — | **309,702** |
| NYC taxi trips | 869,349 | 142,735 | 179,159 | **80,517** |
| Diamond prices | 2,772,143 | 416,066 | — | **316,777** |

Parquet was written with all four codecs it supports (zstd, gzip, brotli,
snappy) and the smallest kept. Same for ORC.

Your numbers will be close but not identical. The tool changes, and the
table was measured on specific slices.

## Parquet does not return your file

Parquet reads a CSV into typed columns and stores the values. It does not
store the file.

Read the EPA file back out of Parquet and you get 18,043,963 bytes from a
20,971,464-byte original. 2.9 MB of quoting, number formatting and
whitespace is gone.

This is not a bug. Parquet is built for analytics, where you want the
values, not the bytes.

It does mean Parquet cannot be used where the file itself is the record:

- regulatory submissions kept as filed
- data behind a published result
- audit trails
- anything you may have to produce later

This tool returns the original file. Every test checks it with SHA-256.

## Speed

| | |
|---|---|
| compress | ~1.7 MB/s |
| decompress | ~120 MB/s |

About 6x slower than `xz -9`, and about the same as `brotli -11`.

It is built for archives that are written once and read rarely. For a
live pipeline it is the wrong tool.

Files compress independently, so 32 cores handle a terabyte in about five
hours.

## Try it on your own data

```
pip install pyarrow pandas
python compare_parquet.py yourfile.csv
```

Writes your file as Parquet and ORC with every codec, compresses it with
this tool, and prints the sizes. Also checks whether each format returns
your original file.

Nothing is uploaded. It runs locally.

If it loses on your data, please tell me. Five public datasets is five
datasets.

## How it works

The file is not compressed directly. It is first rearranged into a form
that a general compressor handles better, then packed with whichever of
lzma, bzip2 or zlib gives the smallest result.

The rearrangement is chosen by measuring, not by rule. Eleven modules
each build a candidate - columnar transposition, fixed-width splitting,
integer delta coding, log tokenisation and others - and the smallest
verified output wins. A module that cannot beat plain compression on a
file loses.

Two things it does that a columnar format cannot:

**Relationships between columns.** Columnar formats compress each column
on its own. So none of them can see that a dropoff time is a pickup time
plus a small number, or that a total is three other columns added up.
Found by trying combinations and measuring. Worth 7.9% on the taxi data.

**Shared vocabularies.** `pickup_zone` and `dropoff_zone` use the same
200 names. Stored once, not twice.

## Verification

Nothing is returned that has not been decompressed and compared to the
input. Each module checks its own round trip before its candidate is
considered. The container carries a CRC.

```
python verify_all.py yourfile.csv
```

Round trips, edge cases (empty files, single bytes, random data, ragged
rows, quoted delimiters, UTF-8) and a size sweep.

```
python quality_test.py /path/to/test/files
```

Checks each file still beats a stated floor. This catches a change that
improves one file and quietly ruins another.

## What it is bad at

**Already-compressed data.** JPEG, MP4, ZIP, quantised model weights.
Around forty approaches were tried on 4-bit quantised weights and all
lost. The file grows by the container size.

**Narrow tables.** Under about ten columns there is less to work with.

**Data you read often.** Decompression is 120 MB/s. Parquet gives random
access and column pruning; this does not. Reading one column out of a
20 GB file is instant in Parquet and requires decompressing everything
here.

**Replacing Parquet.** It is not a replacement. It is for the copy you
keep, not the copy you query.

## Where the datasets came from

- EPA air quality - https://aqs.epa.gov/aqsweb/airdata/download_files.html
- NOAA integrated surface database - https://www.ncei.noaa.gov/data/global-hourly/
- NYC taxi trips - the `taxis` dataset in seaborn
- Diamond prices - the `diamonds` dataset in seaborn and ggplot2

## Licence

AGPL-3.0. Commercial licences available.

## Contact

Ximon Paul M. Rodriguez - xola.compression@gmail.com

If this loses on your data, say so. A result that contradicts the table
is worth more to me than one that confirms it, and it is the only way
the claim gets tested on files I have not seen.
