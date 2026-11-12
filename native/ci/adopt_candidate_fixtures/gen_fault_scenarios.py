#!/usr/bin/env python3
"""注入のシナリオ(旧 A11・A15〜A26 と追加群 E05〜E12)を定義し、旧版の実出力から固定期待を作る(R4C 用)。

    python3 gen_fault_scenarios.py --python <本家 CPython 3.10.11 の python3.10> --out fault_scenarios.json

各シナリオは、準備(prep)・注入の規則(rules)・追加の引数(args)・種類(kind)を持つ。`expect` は旧版の実出力で、
root の絶対パスだけを `<ROOT>` へ対応づけた標準出力・標準エラー・終了値・作業ツリーの記録・index の記録。
同じ JSON を、比較器(旧版と Rust の実比較)と Rust の単体テスト(注入つきの実部品+代役)が読む。
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BODY_TEXT = '> **発見元**: refactor\n> **指摘キー**: H41\n\n## 指摘\n内容\n'
NAME = '候補_例.md'
DEST = '進行中_例.md'
MV = {'point': 'run', 'match': 'mv'}


def rule(point, err=None, **fields):
    if err is not None:
        fields['raise'] = err
    return {'point': point, **fields}


def stage_record(mode, oid, stage, path):
    return ((f'{mode} {oid} {stage}\t{path}').encode().hex()) + '00'


def result(code=0, stdout_hex='', stderr=b''):
    return {'code': code, 'stdout_hex': stdout_hex, 'stderr_hex': stderr.hex()}


def scenarios():
    A40, A64 = 'a' * 40, 'a' * 64
    forced = rule('rename', missing=True)
    tracked = {'git': True}
    items = []

    def add(identifier, group, rules, prep=None, **extra):
        item = {'id': identifier, 'group': group, 'prep': prep or {}, 'rules': rules, **extra}
        item = {key: value for key, value in item.items()}
        if group == 'E11' or identifier == 'E05-created-after-check':
            item['os'] = ['linux']  # renameat2 を使うのは Linux だけ(Mac は link/unlink へ進み、結果が違う)
        items.append(item)

    add('A11-marker-before-rename', 'A11', [rule('marker', after_calls=1)])
    add('A11-marker-late-does-not-preempt-the-digest-check', 'A11', [rule('marker', after_calls=1)], digest='0' * 64)
    add('A15-replace-before-git-mv', 'A15', [{**MV, 'point': 'run', 'phase': 'before',
                                              'do': [{'op': 'attacker_replace', 'path': NAME, 'text': 'third party replacement'}]}], tracked)
    add('A16-index-replaced-after-git-mv', 'A16', [{**MV, 'phase': 'after', 'do': [
        {'op': 'write', 'path': DEST, 'text': 'third party staged replacement'},
        {'op': 'git', 'args': ['add', '--', DEST]}, {'op': 'write', 'path': DEST, 'text': '@BODY'}]}], tracked)
    add('A19-change-while-reading', 'A19', [rule('read', name='source', phase='during',
                                                  do=[{'op': 'write', 'path': NAME, 'text': 'changed while reading'}])])
    add('A20-forced-fallback', 'A20', [forced])
    add('A21-fallback-collision', 'A21', [forced], {'files': {DEST: 'third party'}})
    add('A22-replaced-after-link', 'A22', [forced, rule('link', name='dest', phase='after', do=[
        {'op': 'unlink', 'path': DEST}, {'op': 'write', 'path': DEST, 'text': 'third party'}])])
    add('A23-unlink-failure-rolls-back', 'A23', [forced, rule('unlink', name='source', err=1)])
    add('A24-replaced-and-unlink-fails', 'A24', [forced, rule('unlink', name='source', err=1, do=[
        {'op': 'unlink', 'path': DEST}, {'op': 'write', 'path': DEST, 'text': 'third party'}])])
    add('A25-kill-between-link-and-unlink', 'A25', [forced, rule('unlink', name='source', do=[{'op': 'pause'}])], kind='kill')
    add('A25b-sigint-between-link-and-unlink', 'A25', [forced, rule('unlink', name='source', do=[{'op': 'pause'}])], kind='interrupt')
    add('A25c-sigint-before-link', 'A25', [forced, rule('link', name='dest', phase='before', do=[{'op': 'pause'}])], kind='interrupt')
    # unlink が返った直後(旧版の try の中の最後の点)の SIGINT: 旧版は宛先を巻き戻す(元は削除済みなので、どちらの名前も残らない)。
    add('A25e-sigint-right-after-unlink-rolls-back', 'A25', [forced, rule('unlink', name='source', phase='after', do=[{'op': 'pause'}])], kind='interrupt')
    # link の直後の os.stat(dest) の間(try の外)の SIGINT: 旧版は KeyboardInterrupt で終わり、両方の名前が残る。
    add('A25f-sigint-during-stat-after-link-leaves-both-names', 'A25', [forced, rule('stat', name='dest', nth=2, do=[{'op': 'pause'}])], kind='interrupt')
    # 元の削除が済んだ後(宛先の読み直しの前。try の外)の SIGINT: 巻き戻さず、宛先を残して KeyboardInterrupt で終わる。
    add('A25d-sigint-after-unlink-keeps-the-destination', 'A25', [forced, rule('read', name='dest', do=[{'op': 'pause'}])], kind='interrupt')
    add('A26-same-uid-removes-marker', 'A26', [], kind='bypass')
    # 実行権限(mode)と実体の関係は、各実行内で記録して比べる。
    add('A01x-untracked-executable', 'A01', [], {'modes': {NAME: 0o755}})
    add('A12x-tracked-executable', 'A12', [], {'git': True, 'modes': {NAME: 0o755}})
    add('A20x-fallback-executable', 'A20', [forced], {'modes': {NAME: 0o755}})
    # 権限は S_IMODE の全体を記録して比べる(実行権限の有無だけでなく、読み書きの権限も改名で変わらない)。
    add('A01y-untracked-mode-600', 'A01', [], {'modes': {NAME: 0o600}})
    add('A12y-tracked-mode-600', 'A12', [], {'git': True, 'modes': {NAME: 0o600}})
    add('A20y-fallback-mode-664', 'A20', [forced], {'modes': {NAME: 0o664}})
    # E03 本文の読み取り
    for kind in ('directory', 'absent', 'unreadable', 'empty', 'size:8388607', 'size:8388608', 'size:8388609'):
        # 空と 8MiB 前後の本文は、実際の候補解析(#471)が警告を返す。代役の単体テストでは流さない(比較器が流す)。
        extra = {} if kind in ('directory', 'absent', 'unreadable', 'size:8388609') else {'warnings_from_dependency': True}
        add('E03-source-' + kind.replace(':', '-'), 'E03', [], {'source_as': kind}, **extra)
    same = 'y' * len(BODY_TEXT.encode())
    add('E03-same-size-change-during-read', 'E03', [rule('read', name='source', phase='during', do=[{'op': 'write', 'path': NAME, 'text': same}])])
    add('E03-growth-during-read', 'E03', [rule('read', name='source', phase='during', do=[{'op': 'write', 'path': NAME, 'text': 'z' * (len(BODY_TEXT.encode()) + 500)}])])
    # E05 衝突
    add('E05-dangling-symlink', 'E05', [], {'symlinks': {'完了_例.md': 'nowhere'}})
    add('E05-directory', 'E05', [], {'dirs': ['保留_例.md']})
    add('E05-stat-fails', 'E05', [rule('stat', name='中断_例.md', err=13)])
    add('E05-created-after-check', 'E05', [rule('rename', name='source', do=[{'op': 'write', 'path': DEST, 'text': 'late third party'}])])
    # E07 事前検査の結果の扱い(実部品の代わりに結果を差し替える。時間切れの表示は実部品の API で決まるので含めない)
    add('E07-precheck-nonzero-non-utf8', 'E07', [rule('precheck', result=result(1, '', b'ERROR bad \xff\n'))], tracked)
    add('E07-precheck-exit-22', 'E07', [rule('precheck', result=result(22, '', b'ERROR [filter] refused\n'))], tracked)
    add('E07-precheck-success-output-discarded', 'E07', [rule('precheck', result=result(0, b'filter-token\n'.hex(), b'NOTE something\n'))], tracked)
    add('E07-precheck-empty-accept-not-passed', 'E07', [], tracked, args=['--accept', ''])
    # E08 事前検査の間の変更
    add('E08-body-changed-during-precheck', 'E08', [rule('precheck', do=[{'op': 'write', 'path': NAME, 'text': 'changed during precheck'}])], tracked)
    add('E08-inode-replaced-during-precheck', 'E08', [rule('precheck', do=[{'op': 'replace_inode', 'path': NAME}])], tracked)
    add('E08-marker-after-precheck', 'E08', [rule('marker', after_calls=1)], tracked)
    # E09 index(改名前=1 回目、改名後=2 回目)
    before = {**{'point': 'run', 'match': '--stage'}, 'nth': 1}
    after = {**{'point': 'run', 'match': '--stage'}, 'nth': 2}
    good = lambda oid, path=NAME, mode='100644', stage='0': stage_record(mode, oid, stage, path)  # noqa: E731
    cases = {
        'nonzero': result(1), 'empty': result(0, ''), 'two-records': result(0, good(A40) + good(A40)),
        'no-final-nul': result(0, good(A40)[:-2]), 'no-tab': result(0, f'100644 {A40} 0 {NAME}'.encode().hex() + '00'),
        'mode-symlink': result(0, good(A40, mode='120000')), 'stage-1': result(0, good(A40, stage='1')),
        'other-path': result(0, good(A40, path='other.md')), 'oid-39': result(0, good('a' * 39)),
        'oid-65': result(0, good('a' * 65)), 'oid-upper': result(0, good('A' * 40)),
        'oid-non-hex': result(0, good('g' * 40)), 'oid-40-accepted': result(0, good(A40)),
        'oid-64-accepted': result(0, good(A64)), 'mode-executable-accepted': result(0, good(A40, mode='100755')),
    }
    for label, fake in cases.items():
        add(f'E09-before-{label}', 'E09', [{**before, 'result': fake}], tracked)
    for label in ('nonzero', 'empty', 'oid-39', 'oid-65', 'no-tab', 'mode-executable-accepted', 'stage-1'):
        fake = dict(cases[label])
        if label == 'stage-1':
            fake = result(0, good(A40, path=DEST, stage='1'))
        elif label in ('oid-39', 'oid-65'):
            fake = result(0, good('a' * int(label[4:]), path=DEST))
        elif label == 'mode-executable-accepted':
            fake = result(0, good(A40, path=DEST, mode='100755'))
        add(f'E09-after-{label}', 'E09', [{**after, 'result': fake}], tracked)
    # E10 git mv
    add('E10-mv-nonzero-non-utf8', 'E10', [{**MV, 'result': result(1, '', b'fatal: bad \xff\n')}], tracked)
    add('E10-mv-timeout', 'E10', [{**MV, 'timeout': True}], tracked)
    add('E10-mv-late-collision', 'E10', [{**MV, 'phase': 'before', 'do': [{'op': 'write', 'path': DEST, 'text': 'third party'}]}], tracked)
    add('E10-dest-vanishes-after-mv', 'E10', [{**MV, 'phase': 'after', 'do': [{'op': 'unlink', 'path': DEST}]}], tracked)
    add('E10-dest-inode-replaced-after-mv', 'E10', [{**MV, 'phase': 'after', 'do': [{'op': 'replace_inode', 'path': DEST}]}], tracked)
    add('E10-rev-parse-nonzero', 'E10', [{'point': 'run', 'match': 'rev-parse', 'result': result(128, '', b'fatal: not a git repository\n')}], tracked)
    add('E10-ls-files-nonzero-means-untracked', 'E10', [{'point': 'run', 'match': '--error-unmatch', 'result': result(1, '', b'error: pathspec\n')}], tracked)
    add('E10-after-mv-ls-files-timeout', 'E10', [{'point': 'run', 'match': '--stage', 'nth': 2, 'timeout': True}], tracked)
    add('E10-after-mv-ls-files-spawn-failure', 'E10', [{'point': 'run', 'match': '--stage', 'nth': 2, 'spawn_errno': 2}], tracked)
    add('E10-before-mv-ls-files-spawn-failure', 'E10', [{'point': 'run', 'match': '--stage', 'nth': 1, 'spawn_errno': 2}], tracked)
    add('E10-tracked-check-spawn-failure', 'E10', [{'point': 'run', 'match': '--error-unmatch', 'spawn_errno': 2}], tracked)
    add('E10-mv-under-fsize-limit', 'E10', [rule('precheck', result=result(0))], tracked, rlimit_fsize=100)
    add('E06-parent-git-environment-removed', 'E06', [], tracked, env={'GIT_DIR': '/nonexistent-r4c', 'GIT_WORK_TREE': '/nonexistent-r4c', 'GIT_INDEX_FILE': '/nonexistent-r4c/index', 'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.bare', 'GIT_CONFIG_VALUE_0': 'true'})
    add('E06-no-path-uses-the-default-search', 'E06', [], tracked, env={'PATH': None})
    add('E10-git-timeout', 'E10', [{'point': 'run', 'match': 'rev-parse', 'timeout': True}], tracked)
    # E11 Linux の改名
    for errno_number in (38, 22):
        add(f'E11-rename-errno-{errno_number}-falls-back', 'E11', [rule('rename', name='source', err=errno_number)])
    for errno_number in (1, 17, 13, 18):
        add(f'E11-rename-errno-{errno_number}-stops', 'E11', [rule('rename', name='source', err=errno_number)])
    add('E11-dest-vanishes-after-rename', 'E11', [rule('rename', name='source', phase='after', do=[{'op': 'unlink', 'path': DEST}])])
    add('E11-dest-inode-replaced-after-rename', 'E11', [rule('rename', name='source', phase='after', do=[{'op': 'replace_inode', 'path': DEST}])])
    # E12 link / unlink
    add('E12-link-eexist', 'E12', [forced, rule('link', name='dest', err=17)])
    add('E12-link-eacces', 'E12', [forced, rule('link', name='dest', err=13)])
    add('E12-dest-vanishes-after-link', 'E12', [forced, rule('link', name='dest', phase='after', do=[{'op': 'unlink', 'path': DEST}])])
    add('E12-dest-becomes-directory-after-link', 'E12', [forced, rule('link', name='dest', phase='after', do=[
        {'op': 'unlink', 'path': DEST}, {'op': 'mkdir', 'path': DEST}])])
    add('E12-source-reread-fails', 'E12', [forced, rule('read', name='source', nth=3, err=5)])
    add('E12-source-changes-before-unlink', 'E12', [forced, rule('read', name='source', nth=3, phase='before', do=[
        {'op': 'write', 'path': NAME, 'text': 'changed before unlink'}])])
    add('E12-rollback-sees-non-regular', 'E12', [forced, rule('unlink', name='source', err=1, do=[
        {'op': 'unlink', 'path': DEST}, {'op': 'mkdir', 'path': DEST}])])
    add('E12-rollback-stat-fails', 'E12', [forced, rule('unlink', name='source', err=1), rule('stat', name='dest', nth=3, err=13)])
    add('E12-rollback-unlink-fails', 'E12', [forced, rule('unlink', name='source', err=1), rule('unlink', name='dest', err=1)])
    add('E12-dest-read-fails-after-unlink', 'E12', [forced, rule('read', name='dest', err=5)])
    add('E12-dest-inode-replaced-after-unlink', 'E12', [forced, rule('unlink', name='source', phase='after', do=[
        {'op': 'replace_inode', 'path': DEST}])])
    add('E12-stat-after-link-fails', 'E12', [forced, rule('stat', name='dest', nth=2, err=13)])
    return items


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--python', required=True)
    parser.add_argument('--out', required=True)
    options = parser.parse_args()
    spec = importlib.util.spec_from_file_location('r4c_checker', HERE.parent / 'check_adopt_candidate.py')
    checker = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = checker
    spec.loader.exec_module(checker)
    checker.OLD_PYTHON = Path(options.python)
    checker.verify_old_python(checker.OLD_PYTHON)
    done = []
    for scenario in scenarios():
        scenario['expect'] = checker.scenario_record(scenario, 'old')
        done.append(scenario)
    Path(options.out).write_text(json.dumps({'scenarios': done}, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
