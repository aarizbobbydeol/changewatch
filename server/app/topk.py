"""Top-K selection utility using heapq.nlargest.

Maintains O(n log k) complexity instead of sorting full collections O(n log n).
"""
from __future__ import annotations

import heapq
from typing import Any, Callable, Iterable, List, Optional


def top_k_items(
    items: Iterable[Any],
    k: int = 3,
    key: Optional[Callable[[Any], Any]] = None,
) -> List[Any]:
    """
    Select top k elements from an iterable using heapq.nlargest.
    Avoids full sorting overhead when selecting a small subset (e.g. top 3 issues).
    """
    if k <= 0:
        return []
    if key is None:
        key = lambda x: x.get("count", 0) if isinstance(x, dict) else x
    return heapq.nlargest(k, items, key=key)
