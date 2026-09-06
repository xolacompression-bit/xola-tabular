# xola-tabular

Lossless compression for tabular data. Two modes:

**One file** — 36.6% smaller than the best of Parquet, ORC, gzip, bzip2
and xz on a 20 MB dataset, and byte-exact where Parquet is not.

**A folder of similar files** — 18% to 69% smaller than `tar` + `bzip2`,
with any single file retrievable without unpacking the rest.

Python 3.8 or later. No dependencies outside the standard library.

---

## One file

```python
import prefilter_v31 as xola

packed = xola.compress(open("data.csv", "rb").read())
original = xola.decompress(packed)
```

On a 20 MB EPA air quality export:

| method | size | vs xola |
|---|---|---|
| gzip -9 | 3,779,860 | −71.4% |
| Parquet + zstd | 1,706,588 | −36.6% |
| ORC | 2,220,373 | −51.3% |
| **xola** | **1,081,752** | — |

**And Parquet does not give the file back.** It loses 2.9 MB of
formatting from that 20 MB file — quoting, number formatting, column
order. Where the file itself is the record, that rules it out. xola
returns the original bytes.

Verify it yourself with `compare_parquet.py`. Nothing is uploaded; it
reads your file and prints numbers.

---

## A folder of similar files

```
python xarc.py pack ARCHIVE.xarc DIRECTORY --batch 500
python xarc.py get  ARCHIVE.xarc day0150.csv
python xarc.py verify ARCHIVE.xarc DIRECTORY
python xarc.py unpack ARCHIVE.xarc OUTPUT_DIR
python xarc.py list ARCHIVE.xarc
```

Measured against `tar` + `bzip2 -9` on real files, with every file
checked back out by SHA-256:

| data | files | vs tar+bzip2 |
|---|---|---|
| EPA air quality | 1,000 | **+69.4%** |
| Synthetic call records | 2,000 | +24.0% |
| NOAA weather stations | 64 | +23 to +25% |
| Diamond prices | 64 | +13.5% |
| Bitstamp hourly crypto | 64 | +18.1% |
| Random bytes | 8 | −0.1% (falls back to tar) |

Throughput on a 32-core machine: **10.3 MB/s** on 1,000 files,
**22.9 MB/s** on a 2.9 GB folder. `tar` + `bzip2` does the same 1,000
files in 1.2 s against 2.1 s.

### Why the range is so wide

The tool gathers the same column from every file and compresses those
together, which only helps when the files share values.

EPA data repeats the same station codes, parameter names and units in
every file, so almost everything is shared - hence 69%. Bitstamp hourly
prices are nearly all distinct numbers, hence 18%.

**Telephone records, transaction logs, sensor exports and system logs
sit at the repetitive end. Price series and random identifiers sit at
the other.**

### Never worse than what you use now

Every archive is built both ways - bundled by column and as a plain
`tar` - and the smaller is kept. The worst result across adversarial
inputs is **−0.92%**, the container overhead when bundling cannot help.
Random bytes, already-compressed files and mixed schemas all correctly
fall back.

### What a compressed tar cannot do

**Pull out one file.** One of 2,000 takes 0.17 s and reads only the
bundle containing it. A `.tar.bz2` decompresses from the beginning.

**Verify itself.** Every file carries a CRC32, and `verify` checks all
of them against the originals.

---

## Check the claims yourself

```
python benchmark.py DIRECTORY_OF_SIMILAR_FILES
python benchmark.py --split BIG.csv --files 1000
```

It compares against the **system** `tar` and `bzip2`, not our own
implementations, and verifies every file with SHA-256.

## Testing

- 600 fuzz cases across 16 kinds of awkward input - ragged rows, quoted
  commas, CRLF, UTF-8, embedded nulls, byte-order marks, binary, empty
  files, 400-column tables: **600/600 exact**
- 1,500 corrupted archives: **1,284 rejected cleanly, 0 wrong silently**
- Threading from 0 to 16 threads: **byte-identical output**

## Known limitations

**Memory is bounded by the batch**, not the archive. `--batch` and
`--max-mb` control it. Larger batches compress better - one bundle of
256 files beats four of 64 by 17% - so use the largest your machine
allows.

**Threading reaches 1.25 to 1.75x on 32 cores**, well short of what
those cores could do. Something is serialising and we have not found it.

**No resume after a crash, no parallel batches, and adding a file means
repacking.**

## How it works

Files with the same schema are transposed: all of column 1 from every
file, then column 2, and so on. Each column is encoded by whichever of
three methods measures smallest - repeat-suppression, plain, or
character transposition - and compressed on its own.

Most of the source is comments recording what was tried and failed:
three-layout search, dictionary encoding, subfield splitting, front
coding, chunking, row-major traversal. They are documented so nobody
repeats them.

## A note on how this was built

Developed with AI assistance. Every figure here came from running the
code on real files and comparing against real tools, not from an
estimate.

## Licence

AGPL-3.0.

## Author

Ximon Paul M. Rodriguez · xola.compression@gmail.com
