"""
logcol.py — columnar transform for line-oriented log files
==========================================================

WHAT THIS IS FOR

A log line is a record whose fields happen to be separated by spaces
rather than commas:

    Jun 14 15:16:01 combo sshd(pam_unix)[19939]: authentication failure
    [Sun Dec 04 05:15:09 2005] [error] [client 222.166.160.184] Directory
    081109 203615 148 INFO dfs.DataNode$PacketResponder: PacketResponder 1

The leading fields repeat heavily - the month is the same for thousands
of lines, the log level takes four values, the host never changes - but
they sit far apart in a row-major file, so a string matcher has to carry
a large window to notice.

This is the same idea as csvcol, applied to whitespace-delimited text.

WHY IT WAS WORTH BUILDING

The evidence was already in hand: the SAME Apache log, supplied as a
structured CSV, compressed to +11.4% against the best standard tool,
while the raw log form sat at -0.2%. Identical content, and the only
difference was whether the fields were exposed.

MEASURED ON REAL loghub FILES

    file              before    after
    Apache_2k.log      -0.2%   +12.4%
    HDFS_2k.log        -0.1%    +5.8%
    Linux_2k.log       -0.2%    +4.6%

WHY IT TRIES SEVERAL LAYOUTS

The best split point and the best per-column treatment differ by file,
and not in a way worth predicting:

    Apache   best at k=5, dedup only     (+12.4%, dictionaries gave +12.0%)
    Linux    best at k=6, dictionaries   (+4.6%, dedup alone gave +1.7%)
    HDFS     best at k=4, dictionaries   (+5.8%, dedup alone gave +5.2%)

So every combination is built and the smallest kept - the same rule the
rest of this project uses for every other choice.
"""

# WHAT THIS APPROACH DOES NOT REACH
#
# Columnar transposition needs fields at CONSISTENT POSITIONS. Tested on
# real files and rejected:
#
#   JSON (26 MB of GitHub events, one object per line). Nested, so a
#   field's position varies with what came before it. Five separators
#   were tried - '","', ',"', '":', '{', ',' - and every one LOST:
#   -0.3% to -1.8% against the best standard tool. The keys repeat but
#   they do not line up.
#
#   Source code (_pydecimal.py). Free-form text with no record boundary
#   at all. -0.1%.
#
#   XML (cd_catalog.xml). Same problem as JSON, plus tags spanning lines.
#   -1.3%.
#
# The boundary is clean and worth stating: row-and-column data wins,
# nested and free-form text does not. Those are already what lzma and
# bzip2 were built for, and matching them there is the honest outcome.

SPLITS = (3, 4, 5, 6)
MAX_DICT = 200


def _build(lines, k, dedup, dic):
    cols = [[] for _ in range(k + 1)]
    for l in lines:
        parts = l.split(b' ', k)
        if len(parts) < k + 1:
            parts = parts + [b''] * (k + 1 - len(parts))
        for i in range(k + 1):
            cols[i].append(parts[i])
    hdr = bytearray()
    body = bytearray()
    for c in cols:
        vals = set(c)
        if dic and len(vals) <= MAX_DICT and len(vals) * 3 < len(c):
            tab = sorted(vals)
            idx = {v: i for i, v in enumerate(tab)}
            hdr += b'D' + str(len(tab)).encode() + b'\x00'
            for v in tab:
                hdr += v + b'\x00'
            for v in c:
                body.append(idx[v])
        else:
            hdr += b'V\x00'
            prev = None
            for v in c:
                if dedup and v == prev:
                    body += b'\x01\x00'
                else:
                    body += v + b'\x00'
                    prev = v
    return (str(len(lines)).encode() + b'\n' + str(k).encode() + b'\n'
            + str(1 if dedup else 0).encode() + b'\n'
            + str(len(hdr)).encode() + b'\n' + bytes(hdr) + bytes(body))


def decode(blob):
    nl_s, rest = blob.split(b'\n', 1)
    k_s, rest = rest.split(b'\n', 1)
    de_s, rest = rest.split(b'\n', 1)
    hl_s, rest = rest.split(b'\n', 1)
    nl, k, dedup, hl = int(nl_s), int(k_s), int(de_s), int(hl_s)
    hdr, body = rest[:hl], rest[hl:]
    cols = []
    p = 0
    bp = 0
    for _ in range(k + 1):
        kind = hdr[p:p + 1]
        p += 1
        if kind == b'D':
            e = hdr.index(b'\x00', p)
            ntab = int(hdr[p:e])
            p = e + 1
            tab = []
            for _ in range(ntab):
                e = hdr.index(b'\x00', p)
                tab.append(hdr[p:e])
                p = e + 1
            cols.append([tab[body[bp + i]] for i in range(nl)])
            bp += nl
        else:
            p += 1
            out = []
            prev = None
            for _ in range(nl):
                e = body.index(b'\x00', bp)
                v = body[bp:e]
                bp = e + 1
                if dedup and v == b'\x01':
                    v = prev
                else:
                    prev = v
                out.append(v)
            cols.append(out)
    lines = []
    for r in range(nl):
        parts = [cols[i][r] for i in range(k + 1)]
        while parts and parts[-1] == b'':
            parts.pop()
        lines.append(b' '.join(parts))
    return b'\n'.join(lines) + b'\n'


def looks_like_log(d):
    """Line-oriented text with a consistent number of leading fields."""
    if len(d) < 2048 or not d.endswith(b'\n'):
        return False
    head = d[:65536]
    lines = head.split(b'\n')[:-1]
    if len(lines) < 32:
        return False
    printable = sum(1 for b in head if 9 <= b <= 13 or 32 <= b <= 126)
    if printable < len(head) * 0.95:
        return False
    counts = [l.count(b' ') for l in lines if l]
    return bool(counts) and min(counts) >= 3


def try_encode(d, pack=None):
    """Build every layout, verify each, return the smallest.

    `pack` sizes the candidates. Without it the raw encoded lengths are
    compared, which is a proxy - the router passes a real compressor."""
    if not looks_like_log(d):
        return None
    lines = d.split(b'\n')[:-1]
    if any(b'\x00' in l or b'\x01' in l for l in lines[:200]):
        return None            # markers would be ambiguous
    best = None
    for k in SPLITS:
        for dedup, dic in ((True, False), (True, True), (False, True)):
            try:
                x = _build(lines, k, dedup, dic)
            except Exception:
                continue
            try:
                if decode(x) != d:
                    continue
            except Exception:
                continue
            size = len(pack(x)) if pack else len(x)
            if best is None or size < best[0]:
                best = (size, x)
    return best[1] if best else None
