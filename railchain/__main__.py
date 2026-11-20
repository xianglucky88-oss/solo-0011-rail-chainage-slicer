import argparse
import json
from pathlib import Path
import sqlite3

from .publication import publish, read_edition


def main():
    parser = argparse.ArgumentParser(description='Publish route coverage slices from survey passes')
    parser.add_argument('--db', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('publish').add_argument('source', type=Path)
    read = commands.add_parser('read')
    read.add_argument('route')
    read.add_argument('edition')
    args = parser.parse_args()
    try:
        if args.command == 'publish':
            result = publish(args.db, json.loads(args.source.read_text(encoding='utf-8')))
        else:
            result = read_edition(args.db, args.route, args.edition)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
        parser.exit(2, f'error: {error}\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
