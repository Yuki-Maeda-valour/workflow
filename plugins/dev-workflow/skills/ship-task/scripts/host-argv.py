#!/usr/bin/env python3
"""ホストの起動引数を副作用なく閉じた集合で検査する。"""
import re
import sys


def validate_argv(argv):
    if not argv or not argv[0] or argv[0].startswith('-'):
        raise ValueError('実行ファイルが必要')
    seen = set()
    patterns = {'--model': r'[A-Za-z0-9][A-Za-z0-9._:/-]*',
                '--name': r'[A-Za-z0-9][A-Za-z0-9._:/-]*',
                '--effort': r'(?:low|medium|high|xhigh|max)',
                '--max-budget-usd': r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?',
                '--max-turns': r'[1-9][0-9]*'}
    for token in argv[1:]:
        name, separator, value = token.partition('=')
        if name in seen:
            raise ValueError('host argv の同じ引数を重複できない')
        seen.add(name)
        if token == '--verbose':
            continue
        if name not in patterns or separator != '=' or not re.fullmatch(patterns[name], value):
            raise ValueError('host argv の許可した引数ではない')



def main(argv=None):
    values = sys.argv[1:] if argv is None else argv
    if values[:1] == ['--']:
        values = values[1:]
    try:
        validate_argv(values)
    except ValueError as exc:
        print('ERROR [host-argv] ' + str(exc), file=sys.stderr)
        return 20
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
