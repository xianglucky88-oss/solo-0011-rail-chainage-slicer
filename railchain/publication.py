from contextlib import closing
import json

from .reference import name
from .slicing import partition
from .store import canonical, connect, digest


def publish(path, plan):
    route, edition = name(plan['route']), name(plan['edition'])
    raw = canonical(plan)
    sha = digest(plan)
    facts = partition(plan)
    with closing(connect(path)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        old = conn.execute('SELECT digest FROM alignment_release WHERE route=? AND edition=?',
                           (route, edition)).fetchone()
        if old:
            if old[0] != sha:
                raise ValueError('published alignment edition is immutable')
            return {'status': 'replay', 'sha256': sha, 'spans': len(facts)}
        conn.execute('INSERT INTO alignment_release VALUES(?,?,?,?,?)',
                     (route, edition, sha, raw, json.dumps(facts)))
    return {'status': 'published', 'sha256': sha, 'spans': len(facts)}


def read_edition(path, route, edition):
    with closing(connect(path)) as conn:
        found = conn.execute('SELECT digest,facts FROM alignment_release WHERE route=? AND edition=?',
                             (name(route), name(edition))).fetchone()
    if not found:
        raise ValueError('unknown alignment edition')
    return {'sha256': found[0], 'spans': json.loads(found[1])}
