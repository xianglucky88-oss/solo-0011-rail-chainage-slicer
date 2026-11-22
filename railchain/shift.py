"""Normalise shift documents into one context for planning and replay."""
from .coverage import covers_full
from .evidence import analyze, gap_sections, history_observations, total_gap_mm
from .reference import name, reference

RESULT_STATES = ('usable', 'occluded')


def normalize(doc, alignment_rows=None):
    if not isinstance(doc, dict):
        raise ValueError('shift document must be an object')
    route, shift = name(doc.get('route')), name(doc.get('shift'))
    alignment = reference(alignment_rows if alignment_rows is not None
                          else _section_rows(doc))
    requirements = _requirements(doc.get('requirements'), alignment)
    budget = doc.get('budget_minutes')
    if type(budget) is not int or budget < 0:
        raise ValueError('budget_minutes must be a nonnegative integer')
    shift_minutes = doc.get('shift_minutes')
    if shift_minutes is not None and (type(shift_minutes) is not int or shift_minutes < 0):
        raise ValueError('shift_minutes must be a nonnegative integer')
    release = doc.get('release_at_min', 0)
    if type(release) is not int or release < 0 or release > budget:
        raise ValueError('release_at_min must fall inside the shift budget')
    notifications = _notifications(doc.get('notifications', []), release)
    jobs = _jobs(doc.get('jobs', []), alignment, notifications)
    baseline = _baseline(doc, alignment, requirements)
    policies = _policies(doc.get('policies'), jobs, budget)
    scenarios = _scenarios(doc.get('scenarios'), notifications)
    return {
        'route': route, 'shift': shift,
        'sections': alignment,
        'requirements': requirements,
        'jobs': jobs,
        'notifications': notifications,
        'policies': policies,
        'scenarios': scenarios,
        'budget_minutes': budget,
        'shift_minutes': shift_minutes,
        'release_at_min': release,
        'baseline': baseline,
        'baseline_facts': baseline['facts'],
        'source_notes': doc.get('notes'),
    }


def context_from_facts(facts):
    """Rebuild a context from a stored plan's frozen context block."""
    stored = facts.get('context')
    if not isinstance(stored, dict):
        raise ValueError('stored plan lacks a context block')
    alignment = reference(stored['sections'])
    baseline = stored['baseline']
    return {
        'route': stored['route'], 'shift': stored['shift'],
        'sections': alignment,
        'requirements': dict(stored['requirements']),
        'jobs': {job['job']: dict(job) for job in stored['jobs']},
        'notifications': {item['id']: dict(item) for item in stored['notifications']},
        'policies': [dict(policy) for policy in stored['policies']],
        'scenarios': None,
        'budget_minutes': stored['budget_minutes'],
        'shift_minutes': stored['shift_minutes'],
        'release_at_min': stored['release_at_min'],
        'baseline': {
            'crew': {section: {crew: [tuple(span) for span in spans]
                               for crew, spans in groups.items()}
                     for section, groups in baseline['crew'].items()},
            'usable': {section: [tuple(span) for span in spans]
                       for section, spans in baseline['usable'].items()},
            'ok': dict(baseline['ok']),
            'facts': baseline['facts'],
        },
        'baseline_facts': baseline['facts'],
        'source_notes': stored.get('notes'),
    }


def freeze_context(ctx):
    baseline = ctx['baseline']
    return {
        'route': ctx['route'], 'shift': ctx['shift'],
        'sections': [{'section': section_name, 'offset_mm': section.offset,
                      'length_mm': section.length,
                      'direction': 'reverse' if section.reverse else 'forward'}
                     for section_name, section in ctx['sections'].items()],
        'requirements': dict(ctx['requirements']),
        'jobs': [{key: value for key, value in sorted(job.items())}
                 for job in ctx['jobs'].values()],
        'notifications': [{key: value for key, value in sorted(item.items())}
                          for item in ctx['notifications'].values()],
        'policies': [{'name': policy['name'], 'early': list(policy['early']),
                      'reserve_minutes': policy['reserve_minutes'],
                      'order': policy['order'], 'priority': list(policy['priority']),
                      'drop': list(policy['drop'])} for policy in ctx['policies']],
        'budget_minutes': ctx['budget_minutes'],
        'shift_minutes': ctx['shift_minutes'],
        'release_at_min': ctx['release_at_min'],
        'baseline': {
            'crew': {section: {crew: [list(span) for span in spans]
                               for crew, spans in groups.items()}
                     for section, groups in baseline['crew'].items()},
            'usable': {section: [list(span) for span in spans]
                       for section, spans in baseline['usable'].items()},
            'ok': dict(baseline['ok']),
            'facts': baseline['facts'],
        },
        'notes': ctx.get('source_notes'),
    }


def _section_rows(doc):
    if 'sections' in doc:
        rows = doc['sections']
    elif isinstance(doc.get('alignment'), dict) and 'sections' in doc['alignment']:
        rows = doc['alignment']['sections']
    else:
        raise ValueError('sections or an inline alignment are required')
    if not isinstance(rows, list):
        raise ValueError('sections must be a list')
    return rows


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


def _notifications(rows, release):
    if not isinstance(rows, list):
        raise ValueError('notifications must be a list')
    found = {}
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f'bad notification at row {index}')
        identifier = name(row.get('id'))
        if identifier in found:
            raise ValueError(f'duplicate notification {identifier!r}')
        at_min = row.get('at_min', release)
        if type(at_min) is not int or at_min < 0:
            raise ValueError(f'bad at_min for notification {identifier!r}')
        requires = row.get('requires', [])
        if not isinstance(requires, list) or not all(isinstance(x, str) for x in requires):
            raise ValueError(f'bad requires for notification {identifier!r}')
        found[identifier] = {'id': identifier, 'at_min': at_min,
                             'requires': list(requires)}
    for identifier, item in found.items():
        for dependency in item['requires']:
            if dependency not in found:
                raise ValueError(f'notification {identifier!r} requires unknown {dependency!r}')
    _reject_cycles(found)
    return found


def _reject_cycles(found):
    state = {}

    def visit(node, trail):
        if state.get(node) == 'done':
            return
        if state.get(node) == 'open':
            raise ValueError(f'notification cycle: {" -> ".join(trail + [node])}')
        state[node] = 'open'
        for dependency in found[node]['requires']:
            visit(dependency, trail + [node])
        state[node] = 'done'

    for node in found:
        visit(node, [])


def _jobs(rows, alignment, notifications):
    if not isinstance(rows, list):
        raise ValueError('jobs must be a list')
    found = {}
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f'bad job at row {index}')
        job_id = name(row.get('job'))
        section = name(row.get('section'))
        if job_id in found:
            raise ValueError(f'duplicate job {job_id!r}')
        if section not in alignment:
            raise ValueError(f'unknown section on job {job_id!r}')
        section_obj = alignment[section]
        start, end = row.get('from_mm'), row.get('to_mm')
        if any(type(value) is not int for value in (start, end)) \
                or start < 0 or start >= end or end > section_obj.length:
            raise ValueError(f'invalid scope on job {job_id!r}')
        minutes = row.get('minutes')
        if type(minutes) is not int or minutes <= 0:
            raise ValueError(f'bad minutes on job {job_id!r}')
        crew = name(row.get('crew'))
        groups = _requirement_groups(row, notifications)
        found[job_id] = {
            'job': job_id, 'section': section, 'from_mm': start, 'to_mm': end,
            'crew': crew, 'minutes': minutes, 'requires': groups,
            'value_mm': section_obj.length,
            'priority': index,
        }
    return found


def _requirement_groups(row, notifications):
    raw = row.get('notifications')
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError('job notifications must be a list of alternatives')
    groups = []
    for group in raw:
        if isinstance(group, str):
            group = [group]
        if not isinstance(group, list) or not all(isinstance(x, str) for x in group):
            raise ValueError('bad notification alternative on job')
        for identifier in group:
            if identifier not in notifications:
                raise ValueError(f'job requires unknown notification {identifier!r}')
        groups.append(sorted(set(group)))
    return groups


def _baseline(doc, alignment, requirements):
    if doc.get('baseline') is not None:
        return _baseline_from_daily(doc['baseline'], alignment, requirements)
    observations = history_observations(doc.get('history'), alignment)
    sections, _ = analyze(alignment, requirements, observations)
    crew = {section['section']: {row['crew']: [tuple((span['start_mm'], span['end_mm']))
                                               for span in row['usable_spans']]
                                 for row in section['crew_groups']}
            for section in sections}
    usable = {section['section']: [(span['start_mm'], span['end_mm'])
                                   for span in section['usable_spans']]
              for section in sections}
    ok = {section['section']: section['independent_ok'] for section in sections}
    facts = {'sections': sections,
             'gap_sections': [section['section'] for section in gap_sections(sections)],
             'gap_mm': total_gap_mm(sections)}
    return {'crew': crew, 'usable': usable, 'ok': ok, 'facts': facts}


def _baseline_from_daily(raw, alignment, requirements):
    facts = raw.get('facts') if isinstance(raw, dict) else None
    if not isinstance(facts, dict) or not isinstance(facts.get('sections'), list):
        raise ValueError('baseline needs a published daily release or history')
    by_name = {item['section']: item for item in facts['sections']}
    crew, usable, ok = {}, {}, {}
    for section_name in alignment:
        item = by_name.get(section_name)
        if item is None:
            raise ValueError(f'baseline lacks section {section_name!r}')
        if item['length_mm'] != alignment[section_name].length:
            raise ValueError(f'baseline length differs for {section_name!r}')
        groups = {row['crew']: [(span['start_mm'], span['end_mm'])
                               for span in row['usable_spans']]
                  for row in item.get('crew_groups', [])}
        crew[section_name] = groups
        usable[section_name] = [(span['start_mm'], span['end_mm'])
                                for span in item.get('usable_spans', [])]
        # Re-derive independence from the crews that actually qualified rather
        # than copying a stale flag: the next shift may state a different
        # requirement, and usable-but-single-crew coverage is not a retest.
        complete = sum(1 for spans in groups.values()
                       if covers_full(spans, alignment[section_name].length))
        ok[section_name] = complete >= requirements[section_name]
    return {'crew': crew, 'usable': usable, 'ok': ok, 'facts': facts}


def _policies(raw, jobs, budget):
    if raw is None:
        reserve = max(1, budget // 4) if budget else 0
        early = [job_id for job_id, job in jobs.items() if not job['requires']]
        return [
            {'name': 'early-certain', 'early': early, 'reserve_minutes': 0,
             'order': 'value', 'priority': [], 'drop': []},
            {'name': 'reserve-capacity', 'early': [], 'reserve_minutes': reserve,
             'order': 'rate', 'priority': [], 'drop': []},
        ]
    if not isinstance(raw, list) or not raw:
        raise ValueError('policies must be a nonempty list')
    found, names = [], set()
    for index, row in enumerate(raw, 1):
        if not isinstance(row, dict):
            raise ValueError(f'bad policy at row {index}')
        policy_name = name(row.get('name'))
        if policy_name in names:
            raise ValueError(f'duplicate policy {policy_name!r}')
        names.add(policy_name)
        reserve = row.get('reserve_minutes', 0)
        if type(reserve) is not int or reserve < 0 or reserve > budget:
            raise ValueError(f'bad reserve on policy {policy_name!r}')
        early = row.get('early', [])
        if not isinstance(early, list) or any(job_id not in jobs for job_id in early):
            raise ValueError(f'bad early list on policy {policy_name!r}')
        order = row.get('order', 'value')
        if order not in ('value', 'rate', 'listed'):
            raise ValueError(f'bad order on policy {policy_name!r}')
        priority = row.get('priority', [])
        if not isinstance(priority, list) or any(job_id not in jobs for job_id in priority):
            raise ValueError(f'bad priority on policy {policy_name!r}')
        drop = row.get('drop', [])
        if not isinstance(drop, list) or any(job_id not in jobs for job_id in drop):
            raise ValueError(f'bad drop list on policy {policy_name!r}')
        found.append({'name': policy_name, 'early': list(early),
                      'reserve_minutes': reserve, 'order': order,
                      'priority': list(priority), 'drop': list(drop)})
    return found


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


def depends_on(notifications, identifier):
    """All notifications transitively required by ``identifier``."""
    seen, stack = set(), [identifier]
    while stack:
        node = stack.pop()
        for dependency in notifications[node]['requires']:
            if dependency not in seen:
                seen.add(dependency)
                stack.append(dependency)
    return seen


def close_arrivals(notifications, arrivals):
    """Drop any notification whose requirements did not also arrive.

    A notification cannot be known if the earlier report it waits on never
    came, so dependent notifications are not silently treated as present.
    """
    arrivals = set(arrivals)
    while True:
        missing = {identifier for identifier in arrivals
                   if not depends_on(notifications, identifier) <= arrivals}
        if not missing:
            return arrivals
        arrivals -= missing


def missing_prerequisites(notifications, arrivals):
    """Declared arrivals whose earlier reports are absent, with what is missing."""
    arrivals = set(arrivals)
    problems = {}
    for identifier in sorted(arrivals):
        absent = depends_on(notifications, identifier) - arrivals
        if absent:
            problems[identifier] = sorted(absent)
    return problems


def _scenarios(raw, notifications):
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError('scenarios must be a list')
    found, seen = [], set()
    for index, row in enumerate(raw, 1):
        if isinstance(row, list):
            row = {'id': f'combination-{index}', 'notifications': row}
        if not isinstance(row, dict):
            raise ValueError(f'bad scenario at row {index}')
        identifier = name(row.get('id'))
        if identifier in seen:
            raise ValueError(f'duplicate scenario {identifier!r}')
        seen.add(identifier)
        declared = row.get('notifications', [])
        if not isinstance(declared, list) or any(x not in notifications for x in declared):
            raise ValueError(f'bad arrivals in scenario {identifier!r}')
        arrivals = sorted(close_arrivals(notifications, declared))
        outcomes = _outcomes(row.get('outcomes', {}), notifications)
        missing = sorted(set(arrivals) - set(outcomes))
        if missing:
            raise ValueError(f'scenario {identifier!r} lacks an outcome for '
                             f'{missing[0]!r}')
        found.append({'id': identifier, 'notifications': arrivals,
                      'outcomes': outcomes})
    if found:
        return found
    identifiers = sorted(notifications)
    if len(identifiers) > 6:
        raise ValueError('provide explicit scenarios for more than six notifications')
    scenarios, total = [], 3 ** len(identifiers)
    for mask in range(total):
        value, state = mask, {}
        for identifier in identifiers:
            state[identifier] = ('absent', 'usable', 'occluded')[value % 3]
            value //= 3
        declared = [identifier for identifier in identifiers if state[identifier] != 'absent']
        arrivals = sorted(close_arrivals(notifications, declared))
        outcomes = {identifier: state[identifier] for identifier in arrivals
                    if state[identifier] in RESULT_STATES}
        scenarios.append({'id': 'auto-' + '-'.join(
            f'{identifier}:{state[identifier]}' for identifier in identifiers),
            'notifications': arrivals, 'outcomes': outcomes})
    return scenarios
