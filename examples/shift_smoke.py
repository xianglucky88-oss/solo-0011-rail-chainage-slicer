"""End-to-end checks for the shift and daily-evidence contract."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from railchain import (plan, publish, publish_daily, read_daily, read_edition,
                       read_plan, replan)

HERE = Path(__file__).resolve().parent
SHA = 0


def expect_error(kind, call, fragment=''):
    try:
        call()
    except ValueError as error:
        assert fragment in str(error), (kind, str(error))
    else:
        raise AssertionError(f'{kind}: no error raised')


def alignment_doc():
    return json.loads((HERE / 'alignment.json').read_text(encoding='utf-8'))


def shift_doc():
    return json.loads((HERE / 'shift.json').read_text(encoding='utf-8'))


def daily_doc():
    return json.loads((HERE / 'daily.json').read_text(encoding='utf-8'))


def main():
    with TemporaryDirectory() as temp:
        path = Path(temp) / 'railchain.sqlite'
        plan_document = alignment_doc()
        publish(path, plan_document)
        edition = read_edition(path, 'quarry-branch', 'alignment-blue')
        assert edition['spans'][-1]['status'] == 'occluded'

        # ---- planning ------------------------------------------------
        document = shift_doc()
        first = plan(path, document)
        assert first['status'] == 'planned'
        assert first['policy'] == 'early-certain'
        assert [job['job'] for job in first['committed']] == ['wall-finish']
        assert first['reserved_minutes'] == 0
        assert plan(path, document)['status'] == 'replay'
        expect_error('second plan for same shift', lambda: plan(path, dict(document, sections=[])))

        stored = read_plan(path, 'quarry-branch', document['shift'])
        options = {item['policy']: item for item in stored['plan']['evaluation']['options']}
        assert set(options) == {'early-certain', 'reserve-capacity'}
        early = options['early-certain']['scenarios']
        reserved = options['reserve-capacity']['scenarios']
        assert {row['scenario']: row['committed'] for row in early}['quiet'] == ['wall-finish']
        # The reserved policy leaves the later, more valuable whole-section job
        # in play when its report turns out usable.
        assert 'east-second' in {row['scenario']: row['committed']
                                 for row in reserved}['crew-yes']

        # Opening under the reserved policy holds capacity until the report
        # deadline instead of signing the certain job immediately.
        reserved_open = deepcopy(document)
        reserved_open['shift'] = 'S-reserve'
        reserved_open['policy'] = 'reserve-capacity'
        held = plan(path, reserved_open)
        assert held['committed'] == [] and held['reserved_minutes'] == 30
        # A strictly worse policy is not offered as a choice.
        worse = deepcopy(document)
        worse['policies'].append({'name': 'inert', 'early': [], 'reserve_minutes': 30,
                                  'order': 'rate',
                                  'drop': ['wall-finish', 'wall-tail', 'east-second']})
        worse['shift'] = 'S-inert'
        assert 'inert' not in plan(path, worse)['options']

        # ---- non-anticipation ---------------------------------------
        base = {'route': document['route'], 'shift': document['shift']}
        late = deepcopy(document)
        late['shift'] = 'S-late'
        late['received'] = ['report-wall']
        late['outcomes'] = {'report-wall': 'usable'}
        expect_error('future notification used at opening',
                     lambda: plan(path, dict(late, received=['report-crew'],
                                             outcomes={'report-crew': 'usable'})),
                     'has not arrived')
        expect_error('dependent report without its predecessor',
                     lambda: replan(path, dict(base, now_min=45,
                                               notifications=['report-crew'],
                                               outcomes={'report-crew': 'usable'})),
                     'awaits')
        expect_error('outcome for unreported notification',
                     lambda: plan(path, dict(late, received=[],
                                             outcomes={'report-wall': 'usable'})),
                     'has not arrived')

        # ---- replanning ---------------------------------------------
        started = replan(path, dict(base, now_min=30,
                                    notifications=['report-wall'],
                                    outcomes={'report-wall': 'usable'},
                                    spent_minutes={'wall-finish': 10}))
        expect_error('drop a started job',
                     lambda: replan(path, dict(base, now_min=31,
                                               notifications=['report-wall'],
                                               outcomes={'report-wall': 'usable'},
                                               spent_minutes={'wall-finish': 10},
                                               drop=['wall-finish'])),
                     'started or locked')
        expect_error('recover spent minutes',
                     lambda: replan(path, dict(base, now_min=31,
                                               notifications=['report-wall'],
                                               outcomes={'report-wall': 'usable'},
                                               spent_minutes={'wall-finish': 5})),
                     'cannot be returned')
        # 60 budget, 10 already spent, 25 still needed to finish the pinned job.
        assert started['remaining_budget'] == 60 - 10 - 25 - 8
        expect_error('withdraw a report',
                     lambda: replan(path, dict(base, now_min=32,
                                               notifications=[],
                                               outcomes={})),
                     'cannot be withdrawn')
        expect_error('revise an outcome',
                     lambda: replan(path, dict(base, now_min=32,
                                               notifications=['report-wall'],
                                               outcomes={'report-wall': 'occluded'})),
                     'cannot be revised')

        advanced = replan(path, dict(base, now_min=45,
                                     notifications=['report-wall', 'report-crew'],
                                     outcomes={'report-wall': 'usable',
                                               'report-crew': 'usable'},
                                     spent_minutes={'wall-finish': 35},
                                     locked=['wall-finish']))
        assert advanced['status'] == 'replanned' and advanced['version'] == 3
        # The early-certain plan already sank 35 minutes into the wall, so the
        # later whole-section east run no longer fits: that is the capacity it
        # gave up, and the report records it rather than pretending otherwise.
        assert advanced['remaining_budget'] == 17
        assert [job['job'] for job in advanced['committed']][0] == 'wall-finish'
        assert 'east-second' not in [job['job'] for job in advanced['committed']]
        assert replan(path, dict(base, now_min=45,
                                 notifications=['report-wall', 'report-crew'],
                                 outcomes={'report-wall': 'usable',
                                           'report-crew': 'usable'},
                                 spent_minutes={'wall-finish': 35},
                                 locked=['wall-finish']))['status'] == 'replay'
        # Locked work survives a later revision even if it drifted out of view.
        later = replan(path, dict(base, now_min=50,
                                  notifications=['report-wall', 'report-crew'],
                                  outcomes={'report-wall': 'usable',
                                            'report-crew': 'usable'},
                                  spent_minutes={'wall-finish': 35},
                                  locked=['wall-finish']))
        assert 'wall-finish' in [job['job'] for job in later['committed']]

        # ---- daily evidence -----------------------------------------
        daily = daily_doc()
        daily['plan'] = {'version': first['version']}
        published = publish_daily(path, daily)
        assert published['status'] == 'published'
        facts = read_daily(path, 'quarry-branch', daily['shift'])['facts']
        assert facts['plan']['version'] == first['version']
        assert facts['plan']['digest'] == first['sha256']
        sections = {item['section']: item for item in facts['sections']}

        east = sections['east-cut']
        assert east['independent_ok'] and not east['gap_mm']
        assert east['qualified_groups'] == ['A', 'C']
        # The resubmission and the out-of-scope extra do not add a crew group.
        assert 'D' not in east['qualified_groups']
        assert {item['reason'] for item in facts['receipts']['excluded']} == {
            'resubmission', 'clipped to signed scope', 'unsigned job'}

        # A crew cannot borrow another crew's signed job to fake independence.
        borrowed = daily_doc()
        borrowed['shift'] = 'S-borrowed'
        borrowed['receipts'] = [
            {'receipt': 'mine', 'job': 'j-east-second', 'from_mm': 0, 'to_mm': 300,
             'result': 'usable', 'crew': 'C'}]
        borrowed['prior'] = {'facts': {'shift': 'S0', 'observations': [
            {'source': 'history', 'ref': 'h-east', 'row': 1, 'section': 'east-cut',
             'start_mm': 0, 'end_mm': 1000, 'result': 'usable', 'crew': 'A',
             'job': None}]}}
        publish_daily(path, borrowed)
        borrowed_facts = read_daily(path, 'quarry-branch', 'S-borrowed')['facts']
        borrowed_east = {item['section']: item for item in borrowed_facts['sections']}['east-cut']
        assert 'C' not in borrowed_east['qualified_groups']

        wall = sections['retaining-wall']
        assert not wall['independent_ok']
        assert wall['observed_mm'] == 600 and wall['usable_mm'] == 600
        # Observed everywhere but still a whole-section retest gap for crew B.
        assert wall['gap_mm'] == 600
        assert wall['retest_spans'] == [{'start_mm': 0, 'end_mm': 600}]

        # A half-section or same-crew reverse run never satisfies two crews.
        partial = daily_doc()
        partial['shift'] = 'S-partial'
        partial['receipts'] = [
            {'receipt': 'half', 'job': 'j-east-half', 'from_mm': 0, 'to_mm': 500,
             'result': 'usable', 'crew': 'C'},
            {'receipt': 'reverse', 'job': 'j-east-whole', 'from_mm': 0, 'to_mm': 1000,
             'result': 'usable', 'crew': 'A', 'direction': 'reverse'}]
        partial['signed_jobs'] = [
            {'job': 'j-east-half', 'section': 'east-cut', 'from_mm': 0, 'to_mm': 500,
             'crew': 'C'},
            {'job': 'j-east-whole', 'section': 'east-cut', 'from_mm': 0, 'to_mm': 1000,
             'crew': 'A'}]
        partial['prior'] = {'facts': {'shift': 'S0', 'observations': [
            {'source': 'history', 'ref': 'h-east', 'row': 1, 'section': 'east-cut',
             'start_mm': 0, 'end_mm': 1000, 'result': 'usable', 'crew': 'A',
             'job': None}]}}
        assert publish_daily(path, partial)['status'] == 'published'
        partial_facts = read_daily(path, 'quarry-branch', 'S-partial')['facts']
        partial_east = {item['section']: item for item in partial_facts['sections']}['east-cut']
        assert not partial_east['independent_ok'], partial_east
        assert partial_east['qualified_groups'] == ['A']

        # Same daily evidence is immutable.
        expect_error('daily rewrite', lambda: publish_daily(
            path, dict(daily, receipts=[])), 'immutable')

        # ---- hand-over ----------------------------------------------
        next_shift = shift_doc()
        next_shift['shift'] = 'next'
        next_shift['requirements'] = {'east-cut': 2, 'retaining-wall': 2}
        next_shift['baseline'] = {'facts': daily_doc_facts(path, daily['shift'])}
        planned = plan(path, next_shift)
        assert planned['status'] == 'planned'
        context = read_plan(path, 'quarry-branch', 'next')['plan']['context']
        # east-cut is genuinely complete for two crews; the wall is not, even
        # though it was fully observed -- observed is not retested.
        assert context['baseline']['ok'] == {'east-cut': True, 'retaining-wall': False}

        # Next shift with a higher requirement treats the published single-crew
        # coverage as a gap, not as satisfied retest.
        stricter = shift_doc()
        stricter['shift'] = 'next-strict'
        stricter['requirements'] = {'east-cut': 3, 'retaining-wall': 1}
        stricter['baseline'] = {'facts': daily_doc_facts(path, daily['shift'])}
        plan(path, stricter)
        strict_context = read_plan(path, 'quarry-branch', 'next-strict')['plan']['context']
        assert strict_context['baseline']['ok']['east-cut'] is False

        print(json.dumps({'status': 'ok', 'shift_options': first['options']},
                         sort_keys=True))


def daily_doc_facts(path, shift):
    return read_daily(path, 'quarry-branch', shift)['facts']


if __name__ == '__main__':
    main()
