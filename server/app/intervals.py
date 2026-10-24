"""Interval search optimization using bisect.

Merges overlapping added_ranges into sorted disjoint intervals once per file diff,
enabling O(log n) frame_in_diff queries instead of O(n) brute force scans.
"""
from __future__ import annotations

import bisect
from typing import Dict, List, Tuple


def merge_intervals(ranges: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """
    Merge overlapping and contiguous 1-indexed line intervals into sorted disjoint intervals.
    e.g. [(10, 20), (15, 25), (30, 40)] -> [(10, 25), (30, 40)]
    """
    if not ranges:
        return []

    # Sort primarily by start line, secondarily by end line
    sorted_ranges = sorted(ranges, key=lambda r: (r[0], r[1]))
    merged: List[Tuple[int, int]] = []

    current_start, current_end = sorted_ranges[0]
    for start, end in sorted_ranges[1:]:
        if start <= current_end + 1:  # Overlapping or adjacent
            current_end = max(current_end, end)
        else:
            merged.append((current_start, current_end))
            current_start, current_end = start, end

    merged.append((current_start, current_end))
    return merged


class IntervalIndex:
    """O(log n) binary-search interval index."""

    def __init__(self, ranges: List[Tuple[int, int]]) -> None:
        self.intervals = merge_intervals(ranges)
        self.starts = [s for s, _ in self.intervals]

    def contains(self, line_number: int) -> bool:
        """Return True if line_number falls within any disjoint interval."""
        if not self.intervals or line_number < 1:
            return False

        # bisect_right finds the insertion point after line_number in starts
        idx = bisect.bisect_right(self.starts, line_number) - 1
        if idx >= 0:
            start, end = self.intervals[idx]
            if start <= line_number <= end:
                return True
        return False

    def __len__(self) -> int:
        return len(self.intervals)


def frame_in_diff(
    file_diff_intervals: Dict[str, IntervalIndex],
    filename: str,
    line_number: int,
) -> bool:
    """
    Check if a stack frame (filename, line_number) intersects git diff added ranges.
    O(log n) binary search on pre-merged intervals.
    """
    index = file_diff_intervals.get(filename)
    if index is None:
        return False
    return index.contains(line_number)


def brute_force_frame_in_diff(
    raw_ranges: Dict[str, List[Tuple[int, int]]],
    filename: str,
    line_number: int,
) -> bool:
    """Brute force O(n) scan used as a test oracle to verify bisect interval lookups."""
    ranges = raw_ranges.get(filename, [])
    for start, end in ranges:
        if start <= line_number <= end:
            return True
    return False
