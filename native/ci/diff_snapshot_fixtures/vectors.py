"""正式設計の342入力。生成結果をcases.jsonと完全一致で検査する。"""


VALUE_OPTIONS = ('cwd', 'base', 'out', 'exclude-glob', 'exclude', 'secret-profile-ref',
                 'secret-profile-union-sha256', 'pre-untracked', 'pre-untracked-sha256',
                 'include-untracked', 'max-untracked-bytes', 'patch-out', 'patch-base', 'accept')
GLOBS = ('.env', '.env.*', '**/.env', '/secrets', 'secrets/', 'a?c', 'a*c', '**/a')
ERES = (r'\.env$', r'^(a)\1$', r'(^|/)[[:alpha:]]+\.txt$', r'\<secret\>',
        r'^root|tail$', r'secrets/**', r'^a{1,2}$', r'^line.*break$')
PATHS = (b'.env', b'.Env', b'deep/.env', b'secrets/a.key', b'x/secrets/a.key',
         b'aa', b'line\nbreak', 'é.txt'.encode(), b'bad\xff', b'wordsecret')


def vectors():
    result = []
    def add(case_id, category, executor='external', **data):
        result.append(dict(id=case_id, category=category, executor=executor, platforms=['linux', 'darwin'], **data))
    add('args-normal', 'args', kind='normal', expected=0)
    add('args-precheck', 'args', kind='precheck', expected=0)
    add('args-print', 'args', kind='print', expected=0)
    for option in VALUE_OPTIONS:
        add('args-value-missing-' + option, 'args', kind='value-missing', option=option, expected=2)
    for option in ('cwd', 'base', 'out'):
        add('args-required-missing-' + option, 'args', kind='required-missing', option=option, expected=2)
    add('args-unknown', 'args', kind='unknown', expected=2)
    for option in ('accept', 'secret-profile-union-sha256'):
        add('args-duplicate-' + option, 'args', kind='duplicate', option=option, expected=2)
    for combination in ('print-precheck', 'print-ref', 'precheck-base'):
        add('args-combination-' + combination, 'args', kind='combination', combination=combination, expected=2)
    add('args-patch-base-only', 'args', kind='patch-base-only', expected=2)
    for option in ('pre-untracked', 'pre-untracked-sha256'):
        add('args-pair-only-' + option, 'args', kind='pair-only', option=option, expected=2)
    add('args-union-only', 'args', kind='union-only', expected=2)
    for name, value in (('empty', ''), ('upper', 'A' * 64), ('short', 'a' * 63), ('long', 'a' * 65)):
        add('args-union-' + name, 'args', kind='union-invalid', value=value, expected=2)
    for name, value in (('negative', '-1'), ('letters', 'abc'), ('float', '1.5')):
        add('args-limit-' + name, 'args', kind='limit-invalid', value=value, expected=2)
    for name, option in (('short', '-h'), ('long', '--help')):
        add('args-help-' + name, 'args', kind='help', option=option, expected=0)
    for kind, patterns in (('glob', GLOBS), ('ere', ERES)):
        for i, pattern in enumerate(patterns):
            for j, path in enumerate(PATHS):
                add(f'ere-{kind}-{i + 1:02d}-{j + 1:02d}', 'ere', kind=kind,
                    pattern=pattern, path_hex=path.hex(), expected=0)
    rejected = (('glob', ''), ('glob', 'x//y'), ('glob', "a'b"), ('glob', 'a\x01b'),
                ('glob', 'a$b'), ('glob', 'a[b]'), ('glob', 'a{b}'), ('glob', '**'),
                ('ere', '['), ('ere', ''))
    for i, (kind, pattern) in enumerate(rejected):
        add(f'ere-reject-{i + 1:02d}', 'ere', kind=kind, pattern=pattern, expected=2)
    display = [bytes([value]) for value in range(1, 32)] + [b'\x7f']
    display += [chr(value).encode() for value in range(128, 160)]
    display += [value.encode() for value in ('日本語', 'é', '€', '🙂')]
    display += [('日' * count).encode() for count in (199, 200, 201)]
    display += [('日' * 198 + '\x80\x9fTAIL').encode()]
    for i, value in enumerate(display):
        add(f'display-{i + 1:02d}', 'display', 'typed', input_hex=value.hex())
    points = ('patch-before', 'patch-after', 'out-before', 'out-after', 'cleanup-before')
    for exists in (False, True):
        suffix = 'old' if exists else 'absent'
        for action in ('success', 'patch-failure', 'out-failure'):
            add(f'publish-{suffix}-{action}', 'publish', 'typed', old_out=exists, action=action)
        for point in points:
            for signum in ('TERM', 'INT', 'HUP'):
                add(f'publish-{suffix}-{point}-{signum}', 'publish', 'typed', old_out=exists, point=point, signal=signum)
    for signum in ('TERM', 'INT', 'HUP'):
        for state in ('default', 'ignored', 'child-wait'):
            add(f'signal-{signum}-{state}', 'signal', 'typed', signal=signum, state=state)
    for kind in ('absent', 'regular', 'empty', 'leaf-link', 'parent-link', 'fifo', 'limit', 'over-limit'):
        add('profile-current-' + kind, 'profile', kind=kind, origin='current',
            expected=20 if kind in ('leaf-link', 'parent-link', 'fifo', 'over-limit') else 0)
    for kind in ('absent', '100644', '100755', '120000', 'empty-tree', 'unresolved', 'blob', 'invalid'):
        add('profile-ref-' + kind, 'profile', kind=kind, origin='ref',
            expected=20 if kind in ('120000', 'unresolved', 'blob', 'invalid') else 0)
    assert len(result) == 342
    assert len({case['id'] for case in result}) == 342
    return result
