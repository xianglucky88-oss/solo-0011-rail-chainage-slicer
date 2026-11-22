"""Publish the end-of-shift daily survey result.

The release is the hand-over artefact: it records which parts of each section
already carry an observation and which sections have reached their independent
crew requirement.  The two are reported separately so the next shift cannot
mistake "someone looked at it" for "the retest requirement is satisfied".
"""
from contextlib import closing
import json

from .evidence import (analyze, gap_sections, history_observations,
                       receipt_observations, total_gap_mm)
from .reference import name, reference
from .store import canonical, connect, digest, load_alignment_rows


def publish_daily(path, doc):
    if not isinstance(doc, dict):
        raise ValueError('daily release must be an object')
    route, shift = name(doc.get('route')), name(doc.get('shift'))
    alignment_ref, alignment = _alignment(doc, path, route)
    requirements = _requirements(doc.get('requirements'), alignment)

    prior_observations, prior_meta = _prior(doc, alignment, path)
    signed_jobs = doc.get('signed_jobs', [])
    if not isinstance(signed_jobs, list):
        raise ValueError('signed_jobs must be a list')
    receipts = doc.get('receipts', [])
    if not isinstance(receipts, list):
        raise ValueError('receipts must be a list')
    receipt_obs, excluded = receipt_observations(receipts, signed_jobs, alignment)
    observations = prior_observations + receipt_obs
    sections, _ = analyze(alignment, requirements, observations)
    plan_link = _plan_link(doc.get('plan'), path, route, shift)
    unknown_receipts = sum(1 for item in excluded if item['reason'] == 'unsigned job')

    basis = {'kind': 'daily', 'route': route, 'shift': shift,
             'alignment_digest': alignment_ref['digest'],
             'plan_digest': plan_link['digest'] if plan_link else None,
             'source': doc}
    basis_digest = digest(basis)
    facts = {
        'route': route, 'shift': shift,
        'alignment': alignment_ref,
        'plan': plan_link,
        'requirements': requirements,
        'prior': prior_meta,
        'sections': sections,
        'observations': [_freeze(obs) for obs in observations],
        'receipts': {'accepted': len(receipt_obs),
                     'excluded': excluded,
                     'unknown_receipts': unknown_receipts},
        'gap_sections': [section['section'] for section in gap_sections(sections)],
        'gap_mm': total_gap_mm(sections),
        'observed_mm': sum(section['observed_mm'] for section in sections),
        'usable_mm': sum(section['usable_mm'] for section in sections),
    }
    facts['basis_digest'] = basis_digest
    sha = digest(facts)
    with closing(connect(path)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        old = conn.execute('SELECT digest,facts FROM daily_release WHERE route=? AND shift=?',
                           (route, shift)).fetchone()
        if old:
            if old[0] != sha:
                raise ValueError('daily release is immutable')
            return {'status': 'replay', 'sha256': sha,
                    'spans': len(json.loads(old[1])['sections']),
                    'gap_mm': json.loads(old[1])['gap_mm']}
        conn.execute('INSERT INTO daily_release VALUES(?,?,?,?,?,?,?)',
                     (route, shift, sha, plan_link['version'] if plan_link else 0,
                      plan_link['digest'] if plan_link else '', canonical(doc),
                      canonical(facts)))
    return {'status': 'published', 'sha256': sha, 'sections': len(sections),
            'gap_sections': facts['gap_sections'], 'gap_mm': facts['gap_mm'],
            'excluded_receipts': len(excluded)}


def read_daily(path, route, shift):
    with closing(connect(path)) as conn:
        found = conn.execute('SELECT digest,facts FROM daily_release WHERE route=? AND shift=?',
                             (name(route), name(shift))).fetchone()
    if not found:
        raise ValueError('unknown daily release')
    return {'sha256': found[0], 'facts': json.loads(found[1])}


def _alignment(doc, path, route):
    if doc.get('alignment') is not None:
        ref = doc['alignment']
        if not isinstance(ref, dict):
            raise ValueError('alignment must be an object')
        edition = name(ref.get('edition'))
        edition_route = name(ref.get('route', route))
        if edition_route != route:
            raise ValueError('alignment route differs from the daily route')
        with closing(connect(path)) as conn:
            found = conn.execute('SELECT digest FROM alignment_release WHERE route=? AND edition=?',
                                 (edition_route, edition)).fetchone()
            rows = load_alignment_rows(conn, edition_route, edition)
        return {'route': edition_route, 'edition': edition, 'digest': found[0]}, reference(rows)
    if 'sections' not in doc:
        raise ValueError('a published alignment reference or sections are required')
    rows = doc['sections']
    return {'route': route, 'edition': None, 'digest': digest(rows)}, reference(rows)


def _requirements(raw, alignment):
    if raw is None:
        return {section: 1 for section in alignment}
    if not isinstance(raw, dict):
        raise ValueError('requirements must be an object')
    requirements = {}
    for section in alignment:
        value = raw.get(section, 1)
        if type(value) is not int or value < 0:
            raise ValueError(f'bad required group count for {section!r}')
        requirements[section] = value
    for section in raw:
        if section not in alignment:
            raise ValueError(f'requirement for unknown section {section!r}')
    return requirements


def _prior(doc, alignment, path=None):
    raw = doc.get('prior')
    if raw is None:
        observations = history_observations(doc.get('history'), alignment)
        return observations, {'kind': 'history', 'observations': len(observations)}
    facts = raw.get('facts') if isinstance(raw, dict) and 'facts' in raw else raw
    if not isinstance(facts, dict) or not isinstance(facts.get('observations'), list):
        raise ValueError('prior daily facts with observations are required')
    link = _prior_link(raw, facts, path, doc.get('route'))
    observations = []
    for row, item in enumerate(facts['observations'], 1):
        if not isinstance(item, dict) or item.get('section') not in alignment:
            raise ValueError(f'bad prior observation at row {row}')
        section = alignment[item['section']]
        start, end = item.get('start_mm'), item.get('end_mm')
        if any(type(value) is not int for value in (start, end)) \
                or start < 0 or start >= end or end > section.length:
            raise ValueError(f'bad prior observation span at row {row}')
        observations.append(_thaw(item))
    meta = {'kind': 'daily', 'shift': facts.get('shift'),
            'sha256': digest(facts), 'observations': len(observations)}
    if link:
        meta['release'] = link
    return observations, meta


def _prior_link(raw, facts, path, route):
    """Verify prior facts against the frozen daily release when it exists.

    The next shift is meant to take its gaps from the published daily result.
    A copy that has been hand-edited to omit a gap is rejected; an unpublished
    prior (for example, imported history) is still allowed when its digest is
    internally consistent.
    """
    shift = facts.get('shift') if isinstance(raw, dict) else None
    claimed = raw.get('sha256') if isinstance(raw, dict) else None
    actual = digest(facts)
    if claimed is not None and claimed != actual:
        raise ValueError('prior daily digest does not match its facts')
    if not isinstance(shift, str) or not shift or path is None or route is None:
        return None
    with closing(connect(path)) as conn:
        found = conn.execute('SELECT digest FROM daily_release WHERE route=? AND shift=?',
                             (name(route), name(shift))).fetchone()
    if not found:
        return None
    if found[0] != actual:
        raise ValueError('prior daily facts differ from the published release')
    return {'shift': shift, 'sha256': found[0]}


def _plan_link(raw, path, route, shift):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError('plan linkage must be an object')
    version = raw.get('version')
    if type(version) is not int or version < 1:
        raise ValueError('plan version is required')
    with closing(connect(path)) as conn:
        found = conn.execute('SELECT digest FROM shift_plan WHERE route=? AND shift=? AND version=?',
                             (route, shift, version)).fetchone()
    if not found:
        raise ValueError('unknown shift plan version')
    if raw.get('digest') is not None and raw['digest'] != found[0]:
        raise ValueError('shift plan digest mismatch')
    return {'version': version, 'digest': found[0]}


def _freeze(obs):
    return {'source': obs.source, 'ref': obs.ref, 'row': obs.row,
            'section': obs.section, 'start_mm': obs.start, 'end_mm': obs.end,
            'result': obs.result, 'crew': obs.crew, 'job': obs.job}


def _thaw(item):
    from .evidence import Observation
    crew = item.get('crew')
    if crew is not None and (not isinstance(crew, str) or not crew.strip()):
        raise ValueError('bad crew on prior observation')
    result = item.get('result')
    if result not in ('usable', 'occluded'):
        raise ValueError('bad result on prior observation')
    row = item.get('row', 0)
    if type(row) is not int or row < 0:
        raise ValueError('bad row on prior observation')
    source = item.get('source', 'prior')
    if not isinstance(source, str) or not source:
        raise ValueError('bad source on prior observation')
    return Observation(source, name(item.get('ref')), row, item['section'],
                       item['start_mm'], item['end_mm'], result, crew, item.get('job'))
