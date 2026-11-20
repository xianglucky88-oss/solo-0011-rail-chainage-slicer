"""Produce a coverage partition retaining active source-row identities."""
from collections import defaultdict

from .reference import name, reference


def partition(plan):
    alignment = reference(plan['sections'])
    events = defaultdict(lambda: ([], []))
    for item in alignment.values():
        events[item.offset]
        events[item.offset + item.length]
    source = {}
    for row, record in enumerate(plan['passes'], 1):
        key, section, result = name(record['pass']), name(record['section']), record['result']
        if section not in alignment or key in source or result not in ('usable', 'occluded'):
            raise ValueError(f'bad survey pass at row {row}')
        start, end = alignment[section].project(record['from_mm'], record['to_mm'])
        source[key] = {'pass': key, 'row': row, 'result': result}
        events[start][0].append(key)
        events[end][1].append(key)
    section_at = {section.offset: name for name, section in alignment.items()}
    active, spans = set(), []
    cuts = sorted(events)
    for left, right in zip(cuts, cuts[1:]):
        if left in section_at:
            section_name = section_at[left]
        starts, stops = events[left]
        active.difference_update(stops)
        active.update(starts)
        evidence = [source[key].copy() for key in sorted(active)]
        usable = sum(s['result'] == 'usable' for s in evidence)
        status = 'covered' if usable else ('occluded' if evidence else 'unobserved')
        spans.append({'section': section_name, 'start_mm': left, 'end_mm': right,
                      'status': status, 'usable_passes': usable, 'evidence': evidence})
    return spans
