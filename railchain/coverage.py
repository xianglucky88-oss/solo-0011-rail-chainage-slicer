"""Small integer-interval helpers shared by planning and daily evidence."""


def clip(start, end, low, high):
    """Return the intersection of ``[start,end)`` with ``[low,high)``.

    ``None`` is returned when the two ranges do not overlap.  All values are
    integer millimetres, matching the rest of the package.
    """
    left, right = max(start, low), min(end, high)
    if left >= right:
        return None
    return left, right


def merge(intervals):
    """Merge half-open intervals, including touching ones."""
    ordered = sorted((int(a), int(b)) for a, b in intervals if a < b)
    merged = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def union_length(intervals):
    return sum(end - start for start, end in merge(intervals))


def covers_full(intervals, length):
    """Whether unioned intervals cover ``[0,length)`` without holes."""
    if length <= 0:
        return True
    pieces = merge(intervals)
    cursor = 0
    for start, end in pieces:
        if start > cursor:
            return False
        cursor = max(cursor, end)
        if cursor >= length:
            return True
    return cursor >= length


def complement(intervals, length):
    """Return the gaps in ``[0,length)`` not covered by ``intervals``."""
    gaps, cursor = [], 0
    for start, end in merge(intervals):
        start, end = max(0, start), min(length, end)
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < length:
        gaps.append((cursor, length))
    return gaps
