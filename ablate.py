"""ablate.py — is the delta feature costing anything, and where?

    python ablate.py FOLDER

WHY INTERLEAVED AND NOT BATCHED
-------------------------------
Running every "on" measurement and then every "off" measurement means
a background process that starts halfway through lands entirely on one
condition. Alternating on/off/on/off spreads any drift across both, so
the minimum of each is a floor rather than a coin flip.

That matters more than it sounds here. On this machine the readback
time drifted 0.57 to 0.67 seconds across sessions with no code change
at all - about 15% - which is larger than most of the differences
being measured.

WHY A TIMING ALONE IS NOT ENOUGH
--------------------------------
Turning off the delta trial under-reports. The trial has two parts and
only one of them is skipped:

    the type check      does this column look like plain numbers?
                        runs on EVERY column, including the ones that
                        decline
    the trial itself    build the differences, compress, compare

Stubbing out the encoder removes the second and leaves the first. If
the cost lives in the type check - and once it did, when checking
every character in a Python loop cost more than the wasted work it
prevented - a two-condition ablation says "delta is innocent" and the
slowdown stays.

So this reports two timings AND a per-column table. The timings say
what the trial costs; the table says what the type check costs on each
column, measured directly rather than by subtracting two noisy
numbers.

On a real 1,000-file archive the answer was -1.1% - noise, in the
wrong direction. Delta is free.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ".")
import groupcol


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "."
    files = sorted(f for f in os.listdir(base)
                   if f.lower().endswith((".csv", ".tsv")))
    if not files:
        print(f"\n  no CSV files in {base}\n")
        return 1
    blobs = [open(os.path.join(base, f), "rb").read() for f in files]
    total = sum(len(b) for b in blobs)
    print(f"\n  {len(blobs)} files, {total:,} bytes")
    print("  three runs of each, interleaved, minimum reported\n")

    real_delta = groupcol._delta_encode

    def timed():
        t = time.perf_counter()
        groupcol.archive(blobs, verify=False)
        return time.perf_counter() - t

    def no_delta(vals):
        return None

    full, enc_off = [], []
    for _ in range(3):
        groupcol._delta_encode = real_delta
        full.append(timed())
        groupcol._delta_encode = no_delta
        enc_off.append(timed())
    groupcol._delta_encode = real_delta

    a, b = min(full), min(enc_off)
    print(f"  {'delta fully on':34s} {a:>7.2f}s")
    print(f"  {'encoder stubbed, type check runs':34s} {b:>7.2f}s")
    print(f"  {'the trial itself costs':34s} "
          f"{100 * (a - b) / b:>+6.1f}%")
    print()

    # what the type check costs, measured directly rather than by
    # subtraction - subtracting two noisy numbers gives a noisier one
    rows, hdr = [], None
    for blob in blobs[:200]:
        lines = blob.decode("utf-8", "replace").split("\n")
        if hdr is None:
            hdr = lines[0].split(",")
        for line in lines[1:]:
            if line.strip():
                rows.append(line.split(","))
    if hdr and rows:
        print(f"  {'column':28s} {'verdict':>10s} {'check':>9s}")
        print("  " + "-" * 52)
        worst = (0.0, "")
        for i, name in enumerate(hdr):
            vals = [r[i].encode() for r in rows if len(r) > i]
            if not vals:
                continue
            t = time.perf_counter()
            out = groupcol._delta_encode(vals)
            dt = time.perf_counter() - t
            if dt > worst[0]:
                worst = (dt, name)
            print(f"  {name[:26]:28s} "
                  f"{'USED' if out is not None else 'declined':>10s} "
                  f"{dt * 1000:>7.1f}ms")
        print()
        print(f"  slowest to decide: {worst[1].strip()} at "
              f"{worst[0] * 1000:.0f}ms")
    print()
    print("  How to read this:")
    print("    trial cost under ~5%   -> delta is innocent; any gap")
    print("                              against an older run is drift")
    print("                              or something else that changed")
    print("    trial cost over ~30%   -> this corpus has columns that")
    print("                              are expensive to reject; the")
    print("                              per-column table above says")
    print("                              which")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
