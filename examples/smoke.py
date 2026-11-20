from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from railchain import publish, read_edition

plan = json.loads(Path(__file__).with_name('alignment.json').read_text(encoding='utf-8'))
with TemporaryDirectory() as temp:
    path = Path(temp) / 'alignment.sqlite'
    first = publish(path, plan)
    assert first['status'] == 'published'
    assert publish(path, plan)['status'] == 'replay'
    out = read_edition(path, plan['route'], plan['edition'])
    assert [(s['start_mm'], s['end_mm'], s['status']) for s in out['spans']] == [
        (0, 850, 'covered'), (850, 1000, 'unobserved'), (1000, 1100, 'unobserved'),
        (1100, 1300, 'covered'), (1300, 1500, 'covered'), (1500, 1600, 'occluded')]
    assert [obs['row'] for obs in out['spans'][4]['evidence']] == [2, 3]
    for kind in ('range', 'duplicate'):
        bad = deepcopy(plan)
        bad['edition'] = 'alignment-bad'
        if kind == 'range':
            bad['passes'][1]['to_mm'] = 601
        else:
            bad['passes'].append(bad['passes'][0])
        try:
            publish(path, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f'bad survey pass accepted: {kind}')
    try:
        publish(path, dict(plan, passes=[]))
    except ValueError as error:
        assert 'immutable' in str(error)
    else:
        raise AssertionError('changed edition accepted')
    assert read_edition(path, plan['route'], plan['edition']) == out
    print(json.dumps(out, sort_keys=True))
