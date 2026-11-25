"""固定SHAの旧自己テストを一時領域へ展開する。製品のテストは編集しない。"""
import hashlib
import os
from pathlib import Path
import re
import shlex

SOURCE_SHA256 = '2b1da69392e4e1146bf18381add8148d736189a18c8c1bf5b1bd37814ff5b0bb'

# 開始/終了は完全な1行。範囲内の各assertionはmanifestのtyped_assertionsへ残す。
# 終了行は残す。行数を変えないため、実行記録の行番号は旧版の行番号と一致する。
MOVED_BLOCKS = (
    ('textconv-mutant', 'VAR_TEXTCONV="$WORK/var-textconv.sh"', '# H1: index の gitlink に対応する通常ディレクトリは、git が未追跡を列挙しないため', 0),
    ('gitlink-list-fault', '# 直下の列挙に失敗したときも空として通さない。', '# 検索権限のない祖先を「不存在」と誤認しない。実権限の検査なので root は除く。', 0),
    ('gitlink-mutant', '# H1 検査を外した構文正常な写しでは、上の偽gitlinkが通ってしまう。', '# 実際の submodule を deinit した空ディレクトリ、未初期化 clone、不存在は許可する。deinit 後に', 0),
    ('publication-operations', '# ── ⑬⁗ 予測可能な一時名を使わない ──', 'sha_str() { printf \'%s\' "$1" | sha256sum | cut -d\' \' -f1; }', 0),
    ('rename-failures', '# ── ㉑″ 後段 mv 失敗(--out 宛だけ)→ exit 20 ──', '# ── ㉑⁗ 既存の --patch-out → exit 2 ──', 0),
    ('publication-signals', '# ── ㉒ 既存 out なしで後段 mv 失敗 → exit 20 ──', '# ── ㉓ フック・fsmonitor の無効化 ──', 0),
    ('display-pure', 'awk \'/^sanitize\\(\\)/{p=1} /^add_note\\(\\)/{p=0} p\' "$TARGET" >"$WORK/h2-display.sh"', 'base_repo h2c1', 0),
)


def verified_source(path):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
        raise AssertionError('旧自己テストのSHAが正式設計と一致しません')
    return raw.decode('utf-8')


def block_ranges(source):
    lines = source.splitlines()
    ranges = []
    for name, start, end, _ in MOVED_BLOCKS:
        starts = [i for i, line in enumerate(lines) if line == start]
        if len(starts) != 1:
            raise AssertionError(f'移設開始境界が一意でありません: {name}')
        first = starts[0]
        ends = [i for i, line in enumerate(lines) if line == end]
        if len(ends) != 1 or ends[0] <= first:
            raise AssertionError(f'移設終了境界が一意でありません: {name}')
        last = ends[0]
        if any(first < b and a < last for _, a, b in ranges):
            raise AssertionError('移設範囲が重なっています')
        ranges.append((name, first, last))
    return ranges


def moved_assertions(source):
    lines = source.splitlines()
    result = []
    for group, first, last in block_ranges(source):
        for number in range(first, last):
            if re.match(r'\s*(?:ckt|ckf|ckeq|ckne|ok|ng)\s', lines[number]):
                result.append({'id': f'legacy-typed-{number + 1:04d}', 'line': number + 1,
                               'group': group, 'source': lines[number].strip()})
    return result


def adapt(source, *, result_log):
    """通常の判定式は不変。型検査へ移す範囲と結果記録だけを置き換える。"""
    if hashlib.sha256(source.encode()).hexdigest() != SOURCE_SHA256:
        raise AssertionError('展開元の自己テストが変わりました')
    lines = source.splitlines(keepends=True)
    for group, first, last in block_ranges(source):
        for number in range(first, last):
            lines[number] = f'# S3D typed test: {group}; legacy line {number + 1}\n'
    text = ''.join(lines)
    replacements = {
        'ok() { PASS=$((PASS + 1)); RESULTS+=("PASS  $1"); }':
            'ok() { local line=0 i; for i in "${BASH_LINENO[@]}"; do [ "$i" -eq 0 ] || line="$i"; done; printf \'%s\\0%s\\0%s\\0\' "$line" pass "$1" >>' + shlex.quote(str(result_log)) + '; PASS=$((PASS + 1)); RESULTS+=("PASS  $1"); }',
        'ng() { FAIL=$((FAIL + 1)); RESULTS+=("FAIL  $1"); }':
            'ng() { local line=0 i; for i in "${BASH_LINENO[@]}"; do [ "$i" -eq 0 ] || line="$i"; done; printf \'%s\\0%s\\0%s\\0\' "$line" fail "$1" >>' + shlex.quote(str(result_log)) + '; FAIL=$((FAIL + 1)); RESULTS+=("FAIL  $1"); }',
        '[ "$FAIL" -eq 0 ]':
            'printf \'end\\0%s\\0%s\\0%s\\0\' "$SECTION_OTHERS_1" "$SECTION_C32" "$SECTION_OTHERS_2" >>' + shlex.quote(str(result_log)) + '; [ "$FAIL" -eq 0 ]',
    }
    for old, new in replacements.items():
        if text.count(old) != 1:
            raise AssertionError('自己テストの記録境界が一意でありません')
        text = text.replace(old, new)
    # 旧fixtureは「git-lfsが無い環境」の2入力でも元PATHを使う。
    # 実git-lfsの必須入力と同時に検査するため、このブロックだけ既存の
    # sandbox生成関数でgit-lfsを置かないPATHにする。判定式は変えない。
    missing_start = '# ── ㉔(vi) git-lfs もスタブも無い環境 ──\n'
    missing_end = 'unset SELFTEST_LFS_TRACE\n\naccept_digest()'
    if text.count(missing_start) != 1 or text.count(missing_end) != 1:
        raise AssertionError('git-lfs不在fixtureの境界が一意でありません')
    first = text.index(missing_start)
    last = text.index(missing_end, first)
    block = text[first:last]
    old_run = '  run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob \'.env\''
    if block.count(old_run) != 1:
        raise AssertionError('git-lfs不在fixtureの起動先が一意でありません')
    block = block.replace(missing_start, 'make_sandbox "$WORK/s3d-no-lfs" git-lfs # S3D: 旧fixtureが要求する不在環境\n')
    block = block.replace(old_run, '  PATH="$WORK/s3d-no-lfs" run --cwd "$R" --base "$B" --out "$OUT" --exclude-glob \'.env\'')
    text = text[:first] + block + text[last:]
    if len(text.splitlines()) != len(source.splitlines()):
        raise AssertionError('自己テストの行番号が変わりました')
    return text


def wrapper(path, command, *, pid_log):
    """PIDを記録して同じPIDでexecする。引数には加工を加えない。"""
    if not command or any('\x00' in os.fspath(item) for item in command):
        raise ValueError('実行物の指定が不正です')
    body = '#!/usr/bin/env bash\nprintf \'%s\\0\' "$$" >>' + shlex.quote(str(pid_log))
    body += '\nexec ' + ' '.join(shlex.quote(os.fspath(item)) for item in command) + ' "$@"\n'
    Path(path).write_text(body)
    Path(path).chmod(0o700)


def observations(raw):
    fields = raw.split(b'\0')
    if not fields or fields[-1] != b'':
        raise AssertionError('自己テストの記録がNULで終わっていません')
    fields.pop()
    if len(fields) < 4 or fields[-4:] != [b'end', b'1', b'1', b'1']:
        raise AssertionError('自己テストが全節の末尾へ到達していません')
    fields = fields[:-4]
    if len(fields) % 3:
        raise AssertionError('自己テストの記録の列数が違います')
    counts, result = {}, {}
    for index in range(0, len(fields), 3):
        line, status, description = fields[index:index + 3]
        if not line.isdigit() or status not in (b'pass', b'fail'):
            raise AssertionError('自己テストの記録に未知の値があります')
        number = int(line)
        counts[number] = counts.get(number, 0) + 1
        case_id = f'legacy-{number:04d}-{counts[number]:02d}'
        # 旧自己テストの「飛ばす」を成功へ変換しない。
        skipped = any(word in description for word in ('飛ばす'.encode(), '省略('.encode()))
        result[case_id] = ('skipped' if skipped else status.decode(), description)
    if not result:
        raise AssertionError('自己テストの観測が空です')
    return result
