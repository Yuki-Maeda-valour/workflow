"""旧script自身の確認済み所在地欄と、起動失敗のthread値だけを対応づける。"""
import dataclasses
import os
import re

from .compare import fixed, no_secrets, same_result

APPROVED_DIFFERENCES = [
    {'id': 'legacy-script-own-location', 'scripts': ['diff-snapshot.sh', 'secret-profiles.py'],
     'scope': 'verified-source-location-fields-only'},
    {'id': 'python-startup-current-thread', 'scope': 'fixed-fatal-startup-position-count-format'},
]
SURROGATE_FRAMES = [[287, '<module>'], [279, 'main'], [279, '<genexpr>']]
INTERNAL = 'ERROR [internal] secret profile の和集合を作れない\n'.encode()
SOURCE_LINES = [b'    sys.exit(main())\n',
                b'    sys.stdout.buffer.write(b"".join(item.encode("utf-8") + b"\\0" for item in unique))\n',
                b'    sys.stdout.buffer.write(b"".join(item.encode("utf-8") + b"\\0" for item in unique))\n']


def surrogate_traceback(raw, helper):
    """例外・3フレーム・旧行番号を検査して、所在地だけ対応づける。"""
    helper = os.fsencode(helper)
    if not os.path.isabs(helper) or b'\n' in helper or b'"' in helper:
        raise AssertionError('旧helperの所在地を一意に確認できません')
    expected = b'Traceback (most recent call last):\n'
    for (line, function), source in zip(SURROGATE_FRAMES, SOURCE_LINES):
        expected += b'  File "' + helper + f'", line {line}, in {function}\n'.encode() + source
    if raw.count(helper) != 3 or not raw.startswith(expected):
        raise AssertionError('承認対象の旧3フレームと一致しません')
    tail = raw[len(expected):]
    expression = (rb"UnicodeEncodeError: 'utf-8' codec can't encode "
                  rb"(?:character '\\u[dD][89a-fA-F][0-9a-fA-F]{2}' in position [0-9]+|"
                  rb"characters in position [0-9]+-[0-9]+): surrogates not allowed\n")
    if not re.fullmatch(expression + re.escape(INTERNAL), tail):
        raise AssertionError('承認対象のUnicodeEncodeErrorではありません')
    # 前後を丸ごと置換しない。検査済みの各File欄のみを対応づける。
    return expected.replace(b'  File "' + helper + b'",', b'  File "secret-profiles.py",') + tail


def compare_surrogate(old, new, *, helper, markers=()):
    for value in (old, new):
        no_secrets(value, markers)
        fixed(value, 20, stdout=b'')
        if value.report is not None or value.patch is not None:
            raise AssertionError('例外後に成果物が公開されました')
    expected = surrogate_traceback(old.stderr, helper)
    same_result(dataclasses.replace(old, stderr=expected), new, markers=markers)


def thread_field(raw):
    if not raw.startswith(b'Fatal Python error: init_stdio_encoding: failed to get the Python codec name of the stdio encoding\nPython runtime state: core initialized\nLookupError: unknown encoding: '):
        raise AssertionError('未知codecの起動失敗ではありません')
    if raw.count(b'Current thread ') != 1:
        raise AssertionError('Current thread欄が1回ではありません')
    matches = list(re.finditer(rb'^Current thread 0x([0-9a-f]{16}) \(most recent call first\):\n  <no Python frame>\n', raw, re.M))
    if len(matches) != 1 or matches[0].end() != len(raw) - len(INTERNAL) or not raw.endswith(INTERNAL):
        raise AssertionError('Current thread欄の形式または終了位置が違います')
    match = matches[0]
    if not re.fullmatch(rb"Fatal Python error: init_stdio_encoding: failed to get the Python codec name of the stdio encoding\nPython runtime state: core initialized\nLookupError: unknown encoding: [^\n]+\n\n", raw[:match.start()]):
        raise AssertionError('Current thread欄の行位置が違います')
    return raw[:match.start(1)] + b'<runtime-thread>' + raw[match.end(1):]


def compare_unknown_codec(old, new, *, markers=()):
    for value in (old, new):
        no_secrets(value, markers)
        fixed(value, 20, stdout=b'')
        if value.report is not None or value.patch is not None:
            raise AssertionError('起動失敗後に成果物が公開されました')
    same_result(dataclasses.replace(old, stderr=thread_field(old.stderr)),
                dataclasses.replace(new, stderr=thread_field(new.stderr)), markers=markers)


def typed_report(path, *, source_sha256, manifest_sha256, expected_ids):
    import json
    from pathlib import Path
    from .compare import exact_ids
    if not path or not Path(path).is_absolute():
        raise AssertionError('型検査reportの絶対パスが必要です')
    value = json.loads(Path(path).read_bytes())
    if value.get('schema') != 1 or value.get('source_sha256') != source_sha256 or value.get('manifest_sha256') != manifest_sha256:
        raise AssertionError('型検査reportの形式・ソース・manifestが違います')
    exact_ids(expected_ids, value.get('passed', []))
    return value


def bash_locations(raw, *, invocation, rows):
    """旧ソースから固定した発生行の先頭欄だけを置換する。"""
    invocation = os.fsencode(invocation)
    if not invocation or b'\n' in invocation or b'\0' in invocation:
        raise AssertionError('旧Bash呼出名が一意でありません')
    lines = raw.splitlines(keepends=True)
    indices = [index for index,line in enumerate(lines) if line.startswith(invocation + b': line ')]
    if indices != [row['position'] for row in rows]:
        raise AssertionError('Bash所在地欄の位置または出現数が違います')
    for row in rows:
        position = row['position']
        expected = invocation + b': line ' + str(row['line']).encode() + b': ' + row['detail'] + b'\n'
        if lines[position] != expected:
            raise AssertionError('Bash発生行・操作・失敗理由が固定値と違います')
        lines[position] = b'diff-snapshot.sh' + expected[len(invocation):]
    return b''.join(lines)
