from contextlib import closing
from hashlib import sha256
import json
import sqlite3

from .reference import name
from .slicing import partition

SCHEMA = '''
CREATE TABLE IF NOT EXISTS alignment_release(
 route TEXT, edition TEXT, digest TEXT NOT NULL, source TEXT NOT NULL, facts TEXT NOT NULL,
 PRIMARY KEY(route,edition));
'''


def publish(path, plan):
    route, edition = name(plan['route']), name(plan['edition'])
    raw = json.dumps(plan, allow_nan=False, sort_keys=True, separators=(',', ':'))
    digest = sha256(raw.encode()).hexdigest()
    facts = partition(plan)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(SCHEMA)
        conn.execute('BEGIN IMMEDIATE')
        old = conn.execute('SELECT digest FROM alignment_release WHERE route=? AND edition=?', (route, edition)).fetchone()
        if old:
            if old[0] != digest:
                raise ValueError('published alignment edition is immutable')
            return {'status': 'replay', 'sha256': digest, 'spans': len(facts)}
        conn.execute('INSERT INTO alignment_release VALUES(?,?,?,?,?)', (route, edition, digest, raw, json.dumps(facts)))
    return {'status': 'published', 'sha256': digest, 'spans': len(facts)}


def read_edition(path, route, edition):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(SCHEMA)
        found = conn.execute('SELECT digest,facts FROM alignment_release WHERE route=? AND edition=?',
                             (name(route), name(edition))).fetchone()
    if not found:
        raise ValueError('unknown alignment edition')
    return {'sha256': found[0], 'spans': json.loads(found[1])}
