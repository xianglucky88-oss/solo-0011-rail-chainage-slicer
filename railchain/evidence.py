"""Evidence-backed daily survey releases.

The daily release answers two separate questions for every section:

* which parts already carry an observation, and
* whether the section has reached its independent-crew requirement.

Those are deliberately not the same question.  A section can be fully
*observed* by one crew and still fail a two-crew requirement.  Likewise a
resubmitted receipt, a reverse re-run by the same crew, or a half-section pass
cannot manufacture the missing independence.
"""
from dataclasses import dataclass
from typing import Optional

from .coverage import covers_full, merge


@dataclass(frozen=True)
class Observation:
    source: str          # "history" or "receipt"
    ref: str             # pass or receipt identifier
    row: int             # 1-based row within its source document
    section: str
    start: int           # local section millimetres, half open
    end: int
    result: str          # "usable" or "occluded"
    crew: Optional[str]  # None never establishes independent evidence
    job: Optional[str] = None


def history_observations(history, alignment):
    if history is None:
        return []
    if isinstance(history, dict):
        rows = history.get('passes', [])
    else:
        rows = history
    if not isinstance(rows, list):
        raise ValueError('history passes must be a list')
    observations, seen = [], set()
    for row, record in enumerate(rows, 1):
        if not isinstance(record, dict):
            raise ValueError(f'bad history pass at row {row}')
        ref = _identifier(record, 'pass', row, 'history')
        section = _identifier(record, 'section', row, 'history')
        result = record.get('result')
        if ref in seen:
            raise ValueError(f'duplicate history pass at row {row}')
        seen.add(ref)
        start, end = _local_span(alignment, section, record, row)
        crew = record.get('crew')
        if crew is not None and (not isinstance(crew, str) or not crew.strip()):
            raise ValueError(f'bad crew at history row {row}')
        observations.append(Observation('history', ref, row, section, start, end,
                                        _result(result, row), crew))
    return observations


def receipt_observations(receipts, signed_jobs, alignment):
    """Validate receipts against signed job scope.

    Pieces outside the signed job's section range are not usable evidence.
    They are reported in ``excluded`` instead of silently widening the job.
    """
    if receipts is None:
        return [], []
    if not isinstance(receipts, list):
        raise ValueError('receipts must be a list')
    jobs = {}
    for index, job in enumerate(signed_jobs, 1):
        if not isinstance(job, dict):
            raise ValueError(f'bad signed job at row {index}')
        job_id = _identifier(job, 'job', index, 'signed job')
        section = _identifier(job, 'section', index, 'signed job')
        start, end = _local_span(alignment, section, job, index)
        if job_id in jobs:
            raise ValueError(f'duplicate signed job {job_id!r}')
        crew = job.get('crew')
        if crew is not None and (not isinstance(crew, str) or not crew.strip()):
            raise ValueError(f'bad crew on signed job {job_id!r}')
        jobs[job_id] = {'job': job_id, 'section': section, 'start': start, 'end': end,
                        'crew': crew}
    observations, excluded, seen = [], [], set()
    originals = {}
    for row, record in enumerate(receipts, 1):
        if not isinstance(record, dict):
            raise ValueError(f'bad receipt at row {row}')
        ref = _identifier(record, 'receipt', row, 'receipt')
        result = _result(record.get('result'), row)
        if ref in seen:
            excluded.append({'receipt': ref, 'row': row, 'reason': 'duplicate receipt'})
            continue
        seen.add(ref)
        parent = record.get('resubmission_of')
        if parent is not None:
            if not isinstance(parent, str) or not parent.strip():
                raise ValueError(f'bad resubmission reference at receipt row {row}')
            if parent in originals or parent in seen:
                excluded.append({'receipt': ref, 'row': row, 'reason': 'resubmission'})
                continue
        job_id = record.get('job')
        if not isinstance(job_id, str) or job_id not in jobs:
            excluded.append({'receipt': ref, 'row': row, 'reason': 'unsigned job'})
            continue
        job = jobs[job_id]
        section = record.get('section', job['section'])
        if not isinstance(section, str) or section != job['section']:
            excluded.append({'receipt': ref, 'row': row, 'reason': 'section outside signed job'})
            continue
        start, end = _local_span(alignment, section, record, row)
        clip_start, clip_end = max(start, job['start']), min(end, job['end'])
        if clip_start >= clip_end:
            excluded.append({'receipt': ref, 'row': row, 'reason': 'outside signed scope',
                             'job': job_id})
            continue
        crew = record.get('crew', job['crew'])
        if crew is not None and (not isinstance(crew, str) or not crew.strip()):
            raise ValueError(f'bad crew at receipt row {row}')
        if job['crew'] is not None and crew is not None and crew != job['crew']:
            # A crew cannot borrow another crew's signed job to fabricate the
            # independence the job was meant to supply.
            excluded.append({'receipt': ref, 'row': row, 'reason': 'crew outside signed job',
                             'job': job_id})
            continue
        originals[ref] = row
        observations.append(Observation('receipt', ref, row, section, clip_start, clip_end,
                                        result, crew, job_id))
        if (clip_start, clip_end) != (start, end):
            excluded.append({'receipt': ref, 'row': row, 'reason': 'clipped to signed scope',
                             'job': job_id, 'kept_mm': clip_end - clip_start})
    return observations, excluded


def analyze(alignment, requirements, observations):
    """Summarise observed spans, independent groups and remaining gaps."""
    sections, spans = [], []
    for section_name, section in alignment.items():
        local = [obs for obs in observations if obs.section == section_name]
        required = requirements[section_name]
        section_spans, observed, usable, occluded = _partition_section(
            section_name, section, local)
        spans.extend(section_spans)
        groups = {}
        for obs in local:
            if obs.result != 'usable' or obs.crew is None:
                continue
            groups.setdefault(obs.crew, []).append((obs.start, obs.end))
        crew_rows = []
        for crew in sorted(groups):
            crew_spans = merge(groups[crew])
            crew_rows.append({'crew': crew,
                              'usable_spans': [{'start_mm': a, 'end_mm': b} for a, b in crew_spans],
                              'complete': covers_full(crew_spans, section.length)})
        qualified = [row['crew'] for row in crew_rows if row['complete']]
        independent = len(qualified) >= required
        uncovered = _complement(merge(usable), section.length)
        sections.append({
            'section': section_name,
            'offset_mm': section.offset,
            'length_mm': section.length,
            'direction': 'reverse' if section.reverse else 'forward',
            'required_groups': required,
            'observed_spans': [{'start_mm': a, 'end_mm': b} for a, b in merge(observed)],
            'usable_spans': [{'start_mm': a, 'end_mm': b} for a, b in merge(usable)],
            'occluded_spans': [{'start_mm': a, 'end_mm': b} for a, b in merge(occluded)],
            'uncovered_spans': [{'start_mm': a, 'end_mm': b} for a, b in uncovered],
            'crew_groups': crew_rows,
            'qualified_groups': qualified,
            'independent_ok': independent,
            # A retest is a whole-section unit: finishing half of it never
            # satisfies a requirement that is stated per section.
            'retest_spans': [] if independent else [{'start_mm': 0, 'end_mm': section.length}],
            'gap_mm': 0 if independent else section.length,
            'observed_mm': sum(b - a for a, b in merge(observed)),
            'usable_mm': sum(b - a for a, b in merge(usable)),
            'uncovered_mm': sum(b - a for a, b in uncovered),
        })
    return sections, spans


def gap_sections(sections):
    return [section for section in sections if not section['independent_ok']]


def total_gap_mm(sections):
    return sum(section['gap_mm'] for section in sections)


def _partition_section(section_name, section, local):
    cuts = {0, section.length}
    for obs in local:
        cuts.add(obs.start)
        cuts.add(obs.end)
    cuts = sorted(cuts)
    spans = []
    for left, right in zip(cuts, cuts[1:]):
        active = sorted((obs for obs in local
                         if obs.start <= left and obs.end >= right),
                        key=lambda obs: (obs.source, obs.row, obs.ref))
        if not active:
            status = 'unobserved'
        elif any(obs.result == 'usable' for obs in active):
            status = 'covered'
        else:
            status = 'occluded'
        global_start, global_end = section.project(left, right)
        evidence = []
        for obs in active:
            entry = {'source': obs.source, 'ref': obs.ref, 'row': obs.row,
                     'crew': obs.crew, 'result': obs.result}
            if obs.job is not None:
                entry['job'] = obs.job
            evidence.append(entry)
        spans.append({
            'section': section_name,
            'start_mm': left,
            'end_mm': right,
            'global_start_mm': global_start,
            'global_end_mm': global_end,
            'status': status,
            'usable_count': sum(obs.result == 'usable' for obs in active),
            'occluded_count': sum(obs.result == 'occluded' for obs in active),
            'evidence': evidence,
        })
    return spans, [(obs.start, obs.end) for obs in local], \
        [(obs.start, obs.end) for obs in local if obs.result == 'usable'], \
        [(obs.start, obs.end) for obs in local if obs.result == 'occluded']


def _complement(intervals, length):
    gaps, cursor = [], 0
    for start, end in intervals:
        start, end = max(0, start), min(length, end)
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < length:
        gaps.append((cursor, length))
    return gaps


def _identifier(record, key, row, kind):
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'missing {key} at {kind} row {row}')
    return value


def _result(value, row):
    if value not in ('usable', 'occluded'):
        raise ValueError(f'bad result at row {row}')
    return value


def _local_span(alignment, section, record, row):
    if section not in alignment:
        raise ValueError(f'unknown section at row {row}')
    start, end = record.get('from_mm'), record.get('to_mm')
    section_obj = alignment[section]
    if any(type(value) is not int for value in (start, end)) \
            or start < 0 or start >= end or end > section_obj.length:
        raise ValueError(f'invalid local span at row {row}')
    return start, end
