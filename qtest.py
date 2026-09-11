"""qtest.py — is brotli worth its time, and at which quality?

    python qtest.py FOLDER

Measures the whole archive at each brotli quality, including off.
The question is not which is smallest - it is which trade is worth
making.
"""
import sys, os, time, importlib
sys.path.insert(0, '.')
import groupcol

folder = sys.argv[1] if len(sys.argv) > 1 else '.'
files = sorted(f for f in os.listdir(folder)
               if f.lower().endswith(('.csv', '.tsv')))
blobs = [open(os.path.join(folder, f), 'rb').read() for f in files]
orig = sum(len(b) for b in blobs)
print(f'\n  {len(blobs)} files, {orig:,} bytes\n')
print(f'  {"brotli":12s} {"bytes":>10s} {"vs off":>8s} {"time":>7s} {"vs off":>8s}')
print('  ' + '-' * 50)
base_b = base_t = None
for q in (0, 5, 7, 9, 11):
    groupcol.BROTLI_Q = q
    ts = []
    for _ in range(2):
        t = time.perf_counter()
        g, m = groupcol.archive(blobs, verify=False)
        ts.append(time.perf_counter() - t)
    dt = min(ts)
    if base_b is None:
        base_b, base_t = len(g), dt
    ok = groupcol.unarchive(g) == blobs
    label = 'off' if q == 0 else f'quality {q}'
    print(f'  {label:12s} {len(g):>10,} {100*(1-len(g)/base_b):>+7.2f}% '
          f'{dt:>6.2f}s {100*(dt/base_t-1):>+7.0f}%  exact={ok}')
print()
print('  A gain under about 1% for more than double the time is not')
print('  worth making. The constant is BROTLI_Q in groupcol.py.')
