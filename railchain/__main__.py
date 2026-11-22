import argparse
import json
from pathlib import Path
import sqlite3

from .daily import publish_daily, read_daily
from .plan import plan, read_plan, replan
from .publication import publish, read_edition


def main():
    parser = argparse.ArgumentParser(
        description='Publish route coverage, plan a shift and release daily survey evidence')
    parser.add_argument('--db', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)

    commands.add_parser('publish').add_argument('source', type=Path)
    read = commands.add_parser('read')
    read.add_argument('route')
    read.add_argument('edition')

    shift = commands.add_parser('plan-shift')
    shift.add_argument('source', type=Path)

    again = commands.add_parser('replan-shift')
    again.add_argument('source', type=Path)

    show = commands.add_parser('read-shift')
    show.add_argument('route')
    show.add_argument('shift')
    show.add_argument('--version', type=int)

    daily = commands.add_parser('publish-daily')
    daily.add_argument('source', type=Path)

    show_daily = commands.add_parser('read-daily')
    show_daily.add_argument('route')
    show_daily.add_argument('shift')

    args = parser.parse_args()
    try:
        if args.command == 'publish':
            result = publish(args.db, _load(args.source))
        elif args.command == 'read':
            result = read_edition(args.db, args.route, args.edition)
        elif args.command == 'plan-shift':
            result = plan(args.db, _load(args.source))
        elif args.command == 'replan-shift':
            result = replan(args.db, _load(args.source))
        elif args.command == 'read-shift':
            result = read_plan(args.db, args.route, args.shift, args.version)
        elif args.command == 'publish-daily':
            result = publish_daily(args.db, _load(args.source))
        else:
            result = read_daily(args.db, args.route, args.shift)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        parser.exit(2, f'error: {error}\n')
    print(json.dumps(result, sort_keys=True))


def _load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


if __name__ == '__main__':
    main()
