"""Dynamic-programming page packer (brief §11 rule 4).

Chunks keep their order and are never split. Among all ways to cut the chunk
sequence into pages, choose the one with the fewest pages; among those, the one
whose pages are most evenly filled (minimum sum of squared slack).
"""
from __future__ import annotations

from dataclasses import dataclass


class ChunkTooTall(Exception):
    def __init__(self, label: str, height_mm: float, capacity_mm: float):
        self.label, self.height_mm, self.capacity_mm = label, height_mm, capacity_mm
        super().__init__(
            f"Section '{label}' is {height_mm:.0f} mm tall but a page holds at most "
            f"{capacity_mm:.0f} mm. Shorten it or split it into smaller rows — it cannot be "
            f"printed without clipping."
        )


@dataclass
class PackItem:
    label: str
    height: float  # mm, including its bottom spacing
    keep_with_next: bool = False
    group: str | None = None
    # height when it directly follows the previous row-group on the same page
    # (header hidden, the gap above it removed) — always <= height
    joined_height: float | None = None


def pack(items: list[PackItem], first_capacity: float, capacity: float) -> list[list[int]]:
    """Return pages as lists of item indices."""
    n = len(items)
    if n == 0:
        return [[]]
    for it in items:
        if it.height > max(first_capacity, capacity):
            raise ChunkTooTall(it.label, it.height, max(first_capacity, capacity))

    def cap(page_index: int) -> float:
        return first_capacity if page_index == 0 else capacity

    INF = (float("inf"), float("inf"))
    # best[i][p] impossible to index by page number cheaply, but page index only matters
    # for page 0 vs the rest; state = (items consumed i, on_first_page flag).
    # best[i] = (pages, cost, prev) for the prefix items[:i] ending a page at i.
    best: list[tuple[float, float]] = [INF] * (n + 1)
    prev: list[int] = [-1] * (n + 1)
    best[0] = (0, 0.0)
    for i in range(n):
        if best[i] == INF:
            continue
        pages_so_far, cost_so_far = best[i]
        c = cap(int(pages_so_far))
        h = 0.0
        for j in range(i, n):
            it = items[j]
            joined = j > i and it.group is not None and items[j - 1].group == it.group and it.joined_height is not None
            h += it.joined_height if joined else it.height
            if h > c + 1e-6:
                break
            # never end a page right after a keep_with_next item (unless it is the last)
            if items[j].keep_with_next and j < n - 1:
                continue
            slack = c - h
            cand = (pages_so_far + 1, cost_so_far + slack * slack)
            if cand < best[j + 1]:
                best[j + 1] = cand
                prev[j + 1] = i
    if best[n] == INF:
        # only possible if keep_with_next glues more than a page; relax and retry
        relaxed = [PackItem(it.label, it.height, False, it.group, it.joined_height) for it in items]
        return pack(relaxed, first_capacity, capacity)
    pages: list[list[int]] = []
    j = n
    while j > 0:
        i = prev[j]
        pages.append(list(range(i, j)))
        j = i
    return pages[::-1]
