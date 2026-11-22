"""Shift planning under arriving survey notifications.

The planner deliberately never looks ahead.  A *policy* is a fixed rule
(ordering, which certain jobs are committed up front, and how much capacity is
held back).  At every decision point a policy only sees notifications that have
already arrived, so the same opening information always maps to the same set of
opening jobs.  Scenarios are used to *evaluate* how a policy would fare under
each possible future; they never select a plan for a scenario that has already
resolved, which is what rules out picking a best-looking table after the fact.

Options that are worse than another option in every possible situation are not
offered as choices at all.
"""
from contextlib import closing
from copy import deepcopy
from fractions import Fraction
import json

from .coverage import covers_full, merge, union_length
from .reference import name
from .shift import (RESULT_STATES, close_arrivals, context_from_facts,
                    freeze_context, missing_prerequisites, normalize)
from .store import canonical, connect, digest


# --------------------------------------------------------------------------
# public entry points


def plan(path, doc):
    sections, doc = _resolve_alignment(path, doc)
    ctx = normalize(doc, sections)
    route, shift = ctx['route'], ctx['shift']
    basis = digest({'kind': 'plan', 'route': route, 'shift': shift, 'source': doc})

    opening = _ids(doc.get('received', []), 'received')
    _require_arrived(ctx, opening, 0)
    _require_prerequisites(ctx, opening)
    received = close_arrivals(ctx['notifications'], opening)
    outcomes = _outcomes(doc.get('outcomes', {}), ctx['notifications'])
    _require_outcomes_match(outcomes, received)

    with closing(connect(path)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        old = conn.execute('SELECT version,digest,facts FROM shift_plan '
                           'WHERE route=? AND shift=? AND basis_digest=?',
                           (route, shift, basis)).fetchone()
        if old:
            facts = json.loads(old[2])
            if facts['context'] != freeze_context(ctx):
                raise ValueError('shift plan basis collides with different context')
            return {'status': 'replay', 'version': old[0], 'sha256': old[1],
                    'policy': facts['policy']['name'],
                    'committed': facts['committed']}
        if conn.execute('SELECT 1 FROM shift_plan WHERE route=? AND shift=?',
                        (route, shift)).fetchone():
            raise ValueError('shift plan already exists; use replan to advance it')

        evaluation = evaluate(ctx)
        policy = evaluation['recommended']
        if doc.get('policy') is not None:
            policy = _select_policy(doc['policy'], ctx)
            if policy['name'] not in {item['policy']['name'] for item in evaluation['options']}:
                raise ValueError('policy is not one of the offered options')
        capacity = ctx['budget_minutes']
        if 0 < ctx['release_at_min']:
            capacity -= policy['reserve_minutes']
        chosen, decisions = _commit_at(ctx, policy, _visible(received, outcomes, 0, ctx),
                                       0, capacity, {}, prior=[], use_early=True)
        facts = {
            'route': route, 'shift': shift,
            'context': freeze_context(ctx),
            'evaluation': _evaluation_facts(evaluation),
            'policy': _policy_facts(policy),
            'received': sorted(received), 'outcomes': outcomes, 'now_min': 0,
            'committed': chosen, 'decisions': decisions,
            'spent': {}, 'locked': [], 'dropped': [],
            'remaining_budget': ctx['budget_minutes'] - sum(c['minutes'] for c in chosen),
            'reserved_minutes': policy['reserve_minutes'] if ctx['release_at_min'] else 0,
            'parent_version': None, 'version': 1, 'basis_digest': basis,
            'options': [item['policy']['name'] for item in evaluation['options']],
        }
        sha = digest(facts)
        conn.execute('INSERT INTO shift_plan VALUES(?,?,?,?,?,?,?,?)',
                     (route, shift, 1, sha, basis, None, canonical(doc), canonical(facts)))
    return {'status': 'planned', 'version': 1, 'sha256': sha,
            'policy': policy['name'], 'committed': chosen,
            'remaining_budget': facts['remaining_budget'],
            'reserved_minutes': facts['reserved_minutes'],
            'options': [item['policy']['name'] for item in evaluation['options']],
            'dominated': [item['policy'] for item in evaluation['dominated']],
            'scenarios': [item['id'] for item in ctx['scenarios']]}


def replan(path, doc):
    if not isinstance(doc, dict):
        raise ValueError('replan document must be an object')
    route, shift = name(doc.get('route')), name(doc.get('shift'))
    now = doc.get('now_min')
    if type(now) is not int or now < 0:
        raise ValueError('now_min must be a nonnegative integer')
    with closing(connect(path)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        found = conn.execute('SELECT version,digest,facts FROM shift_plan '
                             'WHERE route=? AND shift=? ORDER BY version DESC LIMIT 1',
                             (route, shift)).fetchone()
        if not found:
            raise ValueError('unknown shift plan')
        parent_version, _, raw = found
        previous = json.loads(raw)
        ctx = context_from_facts(previous)
        notifications = ctx['notifications']

        received_now = _ids(doc.get('notifications', previous['received']), 'notifications')
        _require_arrived(ctx, received_now, now)
        _require_prerequisites(ctx, received_now)
        received = close_arrivals(notifications, received_now)
        if not set(previous['received']) <= received:
            raise ValueError('received notifications cannot be withdrawn')
        outcomes = _outcomes(doc.get('outcomes', previous['outcomes']), notifications)
        _require_outcomes_match(outcomes, received)
        for key, value in previous['outcomes'].items():
            if key in received and outcomes.get(key, value) != value:
                raise ValueError('notification outcome cannot be revised')

        spent = _spent(doc.get('spent_minutes', previous['spent']), previous['spent'])
        locked = set(_ids(doc.get('locked', previous['locked']), 'locked'))
        dropped = set(_ids(doc.get('drop', []), 'drop'))
        unknown = (locked | dropped | set(spent)) - set(ctx['jobs'])
        if unknown:
            raise ValueError(f'unknown job {sorted(unknown)[0]!r}')

        snapshot = {item['job']: item for item in previous['committed']}
        for job_id in set(spent) | locked:
            if job_id not in snapshot:
                # Minutes can only sink into a job that was actually signed;
                # locking an unsigned job would smuggle capacity into the plan.
                raise ValueError(f'job {job_id!r} is not part of the signed plan')
        started = {job_id for job_id, item in snapshot.items()
                   if item['start_min'] <= now or spent.get(job_id, 0) > 0}
        untouchable = started | locked
        if dropped & untouchable:
            raise ValueError('cannot drop a started or locked job')
        for job_id, minutes in spent.items():
            if minutes > ctx['jobs'][job_id]['minutes']:
                raise ValueError(f'spent minutes exceed job {job_id!r}')

        policy = previous['policy']
        override = doc.get('policy')
        if override is not None:
            policy = _select_policy(override, ctx)

        pinned = [snapshot[job_id] for job_id in snapshot if job_id in untouchable]
        spent_total = sum(spent.values())
        pinned_need = sum(max(0, ctx['jobs'][item['job']]['minutes']
                              - spent.get(item['job'], 0)) for item in pinned)
        if spent_total + pinned_need > ctx['budget_minutes']:
            raise ValueError('started jobs already exceed the shift budget')
        capacity = ctx['budget_minutes'] - spent_total - pinned_need
        crew_free = _crew_free(pinned, now)

        visible = _visible(received, outcomes, now, ctx)
        kept = []
        for item in previous['committed']:
            job_id = item['job']
            if job_id in untouchable or job_id in dropped:
                continue
            job = ctx['jobs'][job_id]
            if _job_result(job, visible) != 'usable':
                continue
            kept.append(item)
        # Jobs signed but not yet started keep their sign-off; they are prior
        # state, not fresh commitments.  They still hold their crew and minutes.
        for item in kept:
            crew_free[item['crew']] = max(crew_free.get(item['crew'], 0), item['end_min'])
        capacity -= sum(item['minutes'] for item in kept)
        if capacity < 0:
            raise ValueError('previously signed jobs already exceed the shift budget')

        chosen, decisions = _commit_at(ctx, policy, visible, now, capacity, crew_free,
                                       prior=kept,
                                       exclude=locked | dropped
                                       | {item['job'] for item in pinned}
                                       | {item['job'] for item in kept})
        committed = pinned + kept + chosen
        facts = deepcopy(previous)
        facts.update({
            'policy': _policy_facts(policy),
            'received': sorted(received), 'outcomes': outcomes, 'now_min': now,
            'committed': committed, 'decisions': previous['decisions'] + decisions,
            'spent': spent, 'locked': sorted(locked), 'dropped': sorted(dropped),
            'remaining_budget': ctx['budget_minutes'] - spent_total - pinned_need
            - sum(item['minutes'] for item in kept)
            - sum(item['minutes'] for item in chosen),
            'parent_version': parent_version, 'version': parent_version + 1,
        })
        basis = digest({'kind': 'replan', 'route': route, 'shift': shift,
                        'now_min': now,
                        'received': sorted(received), 'outcomes': outcomes,
                        'spent': spent, 'locked': sorted(locked),
                        'dropped': sorted(dropped), 'policy': policy['name']})
        repeat = conn.execute('SELECT version,digest,facts FROM shift_plan '
                              'WHERE route=? AND shift=? AND basis_digest=?',
                              (route, shift, basis)).fetchone()
        if repeat:
            stored = json.loads(repeat[2])
            return {'status': 'replay', 'version': repeat[0], 'sha256': repeat[1],
                    'committed': stored['committed']}
        facts['basis_digest'] = basis
        sha = digest(facts)
        conn.execute('INSERT INTO shift_plan VALUES(?,?,?,?,?,?,?,?)',
                     (route, shift, facts['version'], sha, basis, parent_version,
                      canonical(doc), canonical(facts)))
    return {'status': 'replanned', 'version': facts['version'], 'sha256': sha,
            'committed': facts['committed'], 'remaining_budget': facts['remaining_budget']}


def read_plan(path, route, shift, version=None):
    with closing(connect(path)) as conn:
        if version is None:
            found = conn.execute('SELECT version,facts FROM shift_plan '
                                 'WHERE route=? AND shift=? ORDER BY version DESC LIMIT 1',
                                 (name(route), name(shift))).fetchone()
        else:
            found = conn.execute('SELECT version,facts FROM shift_plan '
                                 'WHERE route=? AND shift=? AND version=?',
                                 (name(route), name(shift), version)).fetchone()
    if not found:
        raise ValueError('unknown shift plan')
    return {'version': found[0], 'plan': json.loads(found[1])}


def _resolve_alignment(path, doc):
    """Accept inline sections, a bare section list, or a published edition."""
    if not isinstance(doc, dict):
        raise ValueError('shift document must be an object')
    if 'sections' in doc:
        return doc['sections'], doc
    ref = doc.get('alignment')
    if isinstance(ref, dict) and 'edition' in ref:
        route = name(ref.get('route', doc.get('route')))
        from .store import load_alignment_rows
        with closing(connect(path)) as conn:
            rows = load_alignment_rows(conn, route, name(ref['edition']))
        return rows, doc
    if isinstance(ref, list):
        return ref, doc
    raise ValueError('sections or a published alignment edition are required')


# --------------------------------------------------------------------------
# policy evaluation over scenarios


def evaluate(ctx):
    results = [_run_policy(ctx, policy) for policy in ctx['policies']]
    _mark_dominance(results)
    options = [item for item in results if not item['dominated_by']]
    dominated = [{'policy': item['policy']['name'], 'dominated_by': item['dominated_by']}
                 for item in results if item['dominated_by']]
    if not options:
        raise ValueError('no planning policy survives')
    recommended = min(options, key=lambda item: (item['worst_gap_mm'],
                                                 -item['worst_added_mm'],
                                                 -item['worst_usable_mm'],
                                                 item['policy']['name']))
    return {'scenarios': ctx['scenarios'], 'policies': results,
            'options': options, 'dominated': dominated,
            'recommended': recommended['policy']}


def _run_policy(ctx, policy):
    """Play a policy forward under a scenario, committing non-anticipatively."""
    outcomes = []
    for scenario in ctx['scenarios']:
        received = set(scenario['notifications'])
        arrivals = sorted({ctx['notifications'][identifier]['at_min']
                           for identifier in received} | {ctx['release_at_min'], 0})
        chosen, crew_free = [], {}
        for at in arrivals:
            visible = _visible(received, scenario['outcomes'], at, ctx)
            cap = ctx['budget_minutes'] - policy['reserve_minutes'] \
                if at < ctx['release_at_min'] else ctx['budget_minutes']
            room = cap - sum(item['minutes'] for item in chosen)
            if room <= 0:
                continue
            batch, _ = _commit_at(ctx, policy, visible, at, room, crew_free,
                                  prior=chosen, use_early=at == 0)
            if batch:
                chosen.extend(batch)
                for item in batch:
                    crew_free[item['crew']] = max(crew_free.get(item['crew'], 0),
                                                  item['end_min'])
        score = _score(ctx, chosen)
        score.update({'scenario': scenario['id'],
                      'committed': [item['job'] for item in chosen]})
        outcomes.append(score)
    return {
        'policy': policy,
        'scenarios': outcomes,
        'worst_added_mm': min(item['added_mm'] for item in outcomes),
        'worst_usable_mm': min(item['usable_added_mm'] for item in outcomes),
        'worst_gap_mm': max(item['gap_mm'] for item in outcomes),
        'dominated_by': [],
    }


def _mark_dominance(results):
    for item in results:
        for other in results:
            if other is not item and _dominates(other, item):
                item['dominated_by'].append(other['policy']['name'])


def _dominates(left, right):
    strict = False
    for a, b in zip(left['scenarios'], right['scenarios']):
        if a['added_mm'] < b['added_mm'] or a['usable_added_mm'] < b['usable_added_mm'] \
                or a['gap_mm'] > b['gap_mm']:
            return False
        if a['added_mm'] > b['added_mm'] or a['usable_added_mm'] > b['usable_added_mm'] \
                or a['gap_mm'] < b['gap_mm']:
            strict = True
    return strict


# --------------------------------------------------------------------------
# committing and scoring


def _commit_at(ctx, policy, visible, at, capacity, crew_free, prior=(), exclude=(),
               use_early=False):
    """Commit admissible, useful jobs in a fixed policy order at one instant.

    ``prior`` jobs were signed earlier and inform the running crew/coverage
    state, but their minutes are already accounted for by the caller.  Only the
    returned batch consumes ``capacity``.  ``crew_free`` is updated in place.
    ``use_early`` only applies at opening: the early list is what a policy
    commits before any further report can arrive, not a repeated priority.
    """
    jobs = ctx['jobs']
    exclude = set(exclude) | set(policy.get('drop', ())) | {item['job'] for item in prior}
    chosen = []
    spent = 0
    state = _running_state(ctx, prior)
    newly = []

    def admit(job):
        nonlocal spent
        if job['job'] in exclude or any(item['job'] == job['job'] for item in chosen):
            return False
        if _job_result(job, visible) != 'usable':
            return False
        if not _contributes(state, job, ctx):
            return False
        if spent + job['minutes'] > capacity:
            return False
        start = max(at, crew_free.get(job['crew'], 0))
        end = start + job['minutes']
        if ctx['shift_minutes'] is not None and end > ctx['shift_minutes']:
            return False
        item = {'job': job['job'], 'section': job['section'],
                'from_mm': job['from_mm'], 'to_mm': job['to_mm'],
                'crew': job['crew'], 'minutes': job['minutes'],
                'committed_at': at, 'start_min': start, 'end_min': end}
        chosen.append(item)
        spent += job['minutes']
        crew_free[job['crew']] = end
        _apply(state, job)
        newly.append(job['job'])
        return True

    if use_early:
        for job_id in policy['early']:
            if job_id in jobs:
                admit(jobs[job_id])
    for job_id in _order(ctx, policy):
        admit(jobs[job_id])
    decisions = [{'at_min': at, 'visible': sorted(visible), 'committed': newly}] if newly else []
    return chosen, decisions


def _running_state(ctx, chosen):
    crew = {section: {name: list(spans) for name, spans in groups.items()}
            for section, groups in ctx['baseline']['crew'].items()}
    usable = {section: list(ctx['baseline']['usable'][section])
              for section in ctx['sections']}
    for item in chosen:
        crew.setdefault(item['section'], {}).setdefault(item['crew'], []).append(
            (item['from_mm'], item['to_mm']))
        usable.setdefault(item['section'], []).append((item['from_mm'], item['to_mm']))
    return {'crew': crew, 'usable': usable}


def _apply(state, job):
    state['crew'].setdefault(job['section'], {}).setdefault(job['crew'], []).append(
        (job['from_mm'], job['to_mm']))
    state['usable'].setdefault(job['section'], []).append((job['from_mm'], job['to_mm']))


def _contributes(state, job, ctx):
    """Whether a job would widen usable coverage or qualify a new crew.

    A job wholly inside already usable ground that is run by a crew which
    already qualifies adds nothing: it cannot close a gap and cannot add
    independence, so it must not consume minutes.
    """
    section = ctx['sections'][job['section']]
    required = ctx['requirements'][job['section']]
    groups = state['crew'].get(job['section'], {})
    qualified_before = {crew for crew, spans in groups.items()
                        if covers_full(spans, section.length)}
    if len(qualified_before) >= required:
        # The section is already satisfied; the only remaining value is new
        # usable ground.
        before = union_length(state['usable'].get(job['section'], []))
        after = union_length(state['usable'].get(job['section'], [])
                             + [(job['from_mm'], job['to_mm'])])
        return after > before
    # Still short of the requirement.  A job helps if it makes its own crew
    # qualified, or at least widens what that crew has covered.
    crew_spans = groups.get(job['crew'], []) + [(job['from_mm'], job['to_mm'])]
    if job['crew'] not in qualified_before and covers_full(crew_spans, section.length):
        return True
    before = union_length(groups.get(job['crew'], []))
    after = union_length(crew_spans)
    if after > before:
        return True
    before_all = union_length(state['usable'].get(job['section'], []))
    after_all = union_length(state['usable'].get(job['section'], [])
                             + [(job['from_mm'], job['to_mm'])])
    return after_all > before_all


def _job_result(job, visible):
    if not job['requires']:
        return 'usable'
    for group in job['requires']:
        if all(visible.get(identifier) == 'usable' for identifier in group):
            return 'usable'
    return 'absent'


def _order(ctx, policy):
    jobs = ctx['jobs']
    if policy['order'] == 'listed':
        sequence = [job_id for job_id in policy['priority'] if job_id in jobs]
        sequence += [job_id for job_id in jobs if job_id not in sequence]
    else:
        def key(job_id):
            job = jobs[job_id]
            group_count = len(job['requires']) or 1
            if policy['order'] == 'mileage':
                return (-job['value_mm'], group_count, job['minutes'], job['priority'])
            return (-Fraction(job['value_mm'], job['minutes']), group_count,
                    job['minutes'], job['priority'])
        sequence = sorted(jobs, key=key)
    early = [job_id for job_id in policy['early'] if job_id in jobs]
    return early + [job_id for job_id in sequence if job_id not in early]


def _score(ctx, chosen):
    base = ctx['baseline']
    state = _running_state(ctx, chosen)
    added_mm = usable_added_mm = gap_mm = 0
    sections, gap_sections = [], []
    for section_name, section in ctx['sections'].items():
        required = ctx['requirements'][section_name]
        qualified = sorted(crew_name for crew_name, spans
                           in state['crew'].get(section_name, {}).items()
                           if covers_full(spans, section.length))
        ok = len(qualified) >= required
        was_gap = not base['ok'][section_name]
        if was_gap and ok:
            added_mm += section.length
        if not ok:
            gap_mm += section.length
            gap_sections.append(section_name)
        before = sum(b - a for a, b in merge(base['usable'][section_name]))
        after = sum(b - a for a, b in merge(state['usable'].get(section_name, [])))
        usable_added_mm += after - before
        sections.append({'section': section_name, 'required_groups': required,
                         'qualified_groups': qualified, 'independent_ok': ok,
                         'gap_before': was_gap, 'gap_mm': 0 if ok else section.length})
    return {'added_mm': added_mm, 'usable_added_mm': usable_added_mm,
            'gap_mm': gap_mm, 'gap_sections': gap_sections, 'sections': sections}


def _visible(received, outcomes, at, ctx):
    """Reports a decision at ``at`` may rely on.

    Both conditions are required: the notification must have arrived by this
    minute, and its outcome must be known.  An arrival with an unresolved
    result is not a usable report, so a dependent job stays unsigned.
    """
    return {identifier: outcomes[identifier]
            for identifier in received
            if ctx['notifications'][identifier]['at_min'] <= at
            and identifier in outcomes}


def _crew_free(pinned, now):
    free = {}
    for item in pinned:
        free[item['crew']] = max(free.get(item['crew'], 0),
                                 max(now, item['end_min']))
    return free


# --------------------------------------------------------------------------
# document helpers


def _require_arrived(ctx, identifiers, at):
    if not isinstance(identifiers, list):
        raise ValueError('received notifications must be a list')
    for identifier in identifiers:
        if identifier not in ctx['notifications']:
            raise ValueError(f'unknown notification {identifier!r}')
        if ctx['notifications'][identifier]['at_min'] > at:
            raise ValueError(f'notification {identifier!r} has not arrived yet')


def _require_prerequisites(ctx, arrivals):
    problems = missing_prerequisites(ctx['notifications'], arrivals)
    if problems:
        identifier, absent = next(iter(sorted(problems.items())))
        raise ValueError(
            f'notification {identifier!r} awaits {", ".join(repr(x) for x in absent)} '
            'which has not arrived')


def _require_outcomes_match(outcomes, received):
    """Every arrived notification needs a known outcome.

    An arrival whose report is still pending is not the same as a usable one;
    guessing either way would invent information the shift leader did not have.
    """
    missing = sorted(set(received) - set(outcomes))
    if missing:
        raise ValueError(f'outcome missing for arrived notification {missing[0]!r}')
    if not set(outcomes) <= set(received):
        raise ValueError('outcome given for a notification that has not arrived')


def _outcomes(raw, notifications):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError('outcomes must be an object')
    chosen = {}
    for identifier, value in raw.items():
        if identifier not in notifications:
            raise ValueError(f'outcome for unknown notification {identifier!r}')
        if value not in RESULT_STATES:
            raise ValueError(f'bad outcome for notification {identifier!r}')
        chosen[identifier] = value
    return chosen


def _ids(raw, label):
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        raise ValueError(f'{label} must be a list of identifiers')
    return list(raw)


def _spent(raw, previous):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError('spent_minutes must be an object')
    chosen = {}
    for job_id, value in raw.items():
        if type(value) is not int or value < 0:
            raise ValueError(f'bad spent minutes for job {job_id!r}')
        if value < previous.get(job_id, 0):
            raise ValueError('spent minutes cannot be returned')
        chosen[job_id] = value
    return chosen


def _select_policy(raw, ctx):
    if isinstance(raw, str):
        for policy in ctx['policies']:
            if policy['name'] == raw:
                return policy
        raise ValueError(f'unknown policy {raw!r}')
    if not isinstance(raw, dict):
        raise ValueError('policy must be a name or an object')
    from .shift import _policies
    return _policies([raw], ctx['jobs'], ctx['budget_minutes'])[0]


def _policy_facts(policy):
    return {'name': policy['name'], 'early': list(policy['early']),
            'reserve_minutes': policy['reserve_minutes'], 'order': policy['order'],
            'priority': list(policy['priority'])}


def _evaluation_facts(evaluation):
    return {
        'scenarios': [{'id': item['id'], 'notifications': item['notifications'],
                       'outcomes': item['outcomes']} for item in evaluation['scenarios']],
        'options': [_option_facts(item) for item in evaluation['options']],
        'dominated': evaluation['dominated'],
        'recommended': evaluation['recommended']['name'],
    }


def _option_facts(item):
    return {
        'policy': item['policy']['name'],
        'early': list(item['policy']['early']),
        'reserve_minutes': item['policy']['reserve_minutes'],
        'worst_added_mm': item['worst_added_mm'],
        'worst_usable_mm': item['worst_usable_mm'],
        'worst_gap_mm': item['worst_gap_mm'],
        'scenarios': [{'scenario': row['scenario'], 'added_mm': row['added_mm'],
                       'usable_added_mm': row['usable_added_mm'],
                       'gap_mm': row['gap_mm'], 'gap_sections': row['gap_sections'],
                       'committed': row['committed']}
                      for row in item['scenarios']],
    }
