"""SQLite storage shared by alignment, shift-plan and daily releases."""
from hashlib import sha256
import json
import sqlite3

from .reference import name

SCHEMA = '''
CREATE TABLE IF NOT EXISTS alignment_release(
 route TEXT, edition TEXT, digest TEXT NOT NULL, source TEXT NOT NULL, facts TEXT NOT NULL,
 PRIMARY KEY(route,edition));

CREATE TABLE IF NOT EXISTS shift_plan(
 route TEXT, shift TEXT, version INTEGER NOT NULL, digest TEXT NOT NULL,
 basis_digest TEXT NOT NULL, parent_version INTEGER, source TEXT NOT NULL, facts TEXT NOT NULL,
 PRIMARY KEY(route,shift,version));

CREATE UNIQUE INDEX IF NOT EXISTS shift_plan_basis
 ON shift_plan(route,shift,basis_digest);

CREATE TABLE IF NOT EXISTS daily_release(
 route TEXT, shift TEXT, digest TEXT NOT NULL, plan_version INTEGER NOT NULL,
 plan_digest TEXT NOT NULL, source TEXT NOT NULL, facts TEXT NOT NULL,
 PRIMARY KEY(route,shift));
'''


def canonical(value):
    return json.dumps(value, allow_nan=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def connect(path):
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA foreign_keys = ON')
    conn.executescript(SCHEMA)
    return conn


def alignment_rows(source):
    """Return section rows from an alignment document or a bare section list."""
    if isinstance(source, dict):
        rows = source.get('sections')
    else:
        rows = source
    if not isinstance(rows, list):
        raise ValueError('alignment sections are required')
    return rows


def load_alignment_rows(conn, route, edition):
    found = conn.execute(
        'SELECT source FROM alignment_release WHERE route=? AND edition=?',
        (name(route), name(edition))).fetchone()
    if not found:
        raise ValueError('unknown alignment edition')
    return alignment_rows(json.loads(found[0]))
