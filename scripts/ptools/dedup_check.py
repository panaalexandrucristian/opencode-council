#!/usr/bin/env python3
"""Audit verbatim repeats >=128 bytes inside each prompt (stdlib, offline, read-only).

Usage: dedup_check.py <run-dir>
Uses a suffix array and longest-common-prefix scan, then removes repeats wholly
covered by a larger reported block. Only non-overlapping occurrences count; no
whitespace, Unicode, JSON-escape or newline normalization is performed. Reports
byte offsets (zero-based), line numbers (one-based), and short previews, rather
than echoing whole proposals. A separate paragraph count checks the specification's
strict LF-blank-line (\\n\\n) paragraph observation. No imports from the council runtime.
"""

import argparse
from collections import defaultdict
from pathlib import Path


MIN_BYTES = 128


def repeated_blocks(data):
    """Suffix-array doubling + Kasai LCP; byte comparisons are authoritative."""
    n = len(data)
    suffixes, ranks, width = list(range(n)), list(data), 1
    while width < n:
        suffixes.sort(key=lambda i: (ranks[i], ranks[i + width] if i + width < n else -1))
        new, rank = [0] * n, 0
        for k in range(1, n):
            a, b = suffixes[k - 1], suffixes[k]
            if (ranks[a], ranks[a + width] if a + width < n else -1) != (ranks[b], ranks[b + width] if b + width < n else -1):
                rank += 1
            new[b] = rank
        ranks = new
        if rank == n - 1:
            break
        width *= 2
    order = [0] * n
    for rank, pos in enumerate(suffixes):
        order[pos] = rank
    edges, length = [], 0
    for a in range(n):
        rank = order[a]
        if not rank:
            length = 0
            continue
        b = suffixes[rank - 1]
        while a + length < n and b + length < n and data[a + length] == data[b + length]:
            length += 1
        if length >= MIN_BYTES:
            edges.append((length, rank))
        length = max(0, length - 1)
    # Merge LCP intervals longest-first. Min/max positions catch non-adjacent
    # non-overlapping repeats even in a long run of identical bytes.
    parent, low, high = list(range(n)), suffixes[:], suffixes[:]

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    candidates = []
    for length, rank in sorted(edges, reverse=True):
        a, b = root(rank - 1), root(rank)
        parent[b] = a
        low[a], high[a] = min(low[a], low[b]), max(high[a], high[b])
        size = min(length, high[a] - low[a])
        if size >= MIN_BYTES:
            candidates.append((size, low[a], high[a]))
    covered, results = [], []
    for size, a, b in sorted(candidates, key=lambda c: (-c[0], c[1], c[2])):
        block, offsets, pos = data[a:a + size], [], 0
        while True:
            pos = data.find(block, pos)
            if pos < 0:
                break
            offsets.append(pos)
            pos += size
        if all(any(lo <= pos and pos + size <= hi for lo, hi in covered) for pos in offsets):
            continue
        if len(offsets) >= 2:
            results.append((block, offsets))
            covered.extend((pos, pos + size) for pos in offsets)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0], usage="%(prog)s <run-dir>")
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    try:
        paths = sorted((args.run_dir / "prompts").glob("*.md"))
        if not paths:
            raise ValueError("expected <run-dir>/prompts/*.md")
        count, paragraph_count = 0, 0
        for path in paths:
            data = path.read_bytes()
            blocks = repeated_blocks(data)
            paragraphs = defaultdict(int)
            for block in data.split(b"\n\n"):
                if len(block) >= MIN_BYTES:
                    paragraphs[block] += 1
            paragraph_count += sum(1 for n in paragraphs.values() if n > 1)
            if blocks:
                print(path.name + ":")
            for block, offsets in blocks:
                count += 1
                locations = ", ".join("{} (line {})".format(p, data.count(b"\n", 0, p) + 1) for p in offsets)
                print("  {:,} bytes x {} at {}; preview {}".format(len(block), len(offsets), locations, repr(block[:100].decode("utf-8", errors="replace"))))
        print("{} prompts; {} repeated blocks >=128 bytes; {} repeated blank-line-delimited paragraphs >=128 bytes.".format(len(paths), count, paragraph_count))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
