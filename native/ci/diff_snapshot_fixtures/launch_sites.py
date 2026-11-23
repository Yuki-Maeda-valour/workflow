"""旧版の起動場所の表(launch_sites.json)を確かめる。

- 起動の記録: Rustが起動した子は、表で「起動する」とした旧版の呼出し(gitとERE照合のgrep)に限る。
- 実測: 起動しない道具の失敗の文を、基準の版の道具を単体で同じ条件で動かして測り、表と比べる。
  基準と違う機械では、この欄を失敗として記録する(成功やskipにしない)。
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

TABLE = Path(__file__).with_name('launch_sites.json')
LAUNCHED_TOOLS = ('git', 'grep')
TYPED_IDS = ('tool-n-cp-1187', 'tool-n-cp-1191', 'tool-n-mv-1192', 'tool-n-mv-1194', 'tool-n-rm-247',
             'tool-n-redirect-1347', 'tool-n-redirect-1416', 'tool-n-redirect-1472')
# リダイレクトで読むファイルの失敗(開けない・読めない)を地点ごとに確かめる型検査(先行点検2の指摘4)。
READ_TYPED_IDS = ('tool-n-read-0694-open', 'tool-n-read-0807-open', 'tool-n-read-1418-open', 'tool-n-head-1346-open',
                  'tool-n-head-1474-open', 'tool-n-read-0694-read', 'tool-n-read-0807-read', 'tool-n-read-1418-read',
                  'tool-n-head-1346-read', 'tool-n-head-1474-read', 'tool-n-head-size-1347', 'tool-n-readlink-1411',
                  'tool-n-readlink-1462', 'tool-n-printf-0978')
# 版を読む道具と、基準の版(CIのUbuntu 24.04。2026-11-12 00:35の決定)。
VERSION_COMMANDS = {'bash': (['bash', '--version'], r'version (\d+\.\d+\.\d+)'),
                    'coreutils': (['mktemp', '--version'], r'coreutils\) (\d+(?:\.\d+)+)'),
                    'findutils': (['find', '--version'], r'findutils\) (\d+(?:\.\d+)+)'),
                    'sed': (['sed', '--version'], r'sed\) (\d+(?:\.\d+)+)'),
                    'grep': (['grep', '--version'], r'grep\) (\d+(?:\.\d+)+)'),
                    'glibc': (['ldd', '--version'], r'(\d+\.\d+)\s*$')}
# 実測の後で、表の文の欄へ入れる値(パスは測った場所を<D>へ対応づけた形)。
FILL = {'mktemp-missing': {'template': "'<D>/missing/tmp.XXXXXXXXXX'"},
        'mktemp-ro': {'template': "'<D>/ro/.diff-snapshot.XXXXXX'"},
        'mkdir-ro': {'dir': "'<D>/ro/sub'"}, 'mkdir-long': {'dir': "'<D>/<LONG>'"},
        'cp-create': {'dest': "'<D>/ro/out.tmp'"}, 'cp-fsize': {'source': "'src'", 'dest': "'<D>/big.tmp'"},
        'mv-ro': {'source': "'m1'", 'dest': "'<D>/ro/m1'"}, 'mv-overwrite-dir': {'dest': "'target/src'"},
        'rm-dir': {'path': "'<D>/target'"}, 'rm-rf': {'path': "'<D>/rr/inner/f'"},
        'find-unreadable': {'path': "'./unreadable/.'"},
        'redirect': {'script': 'bash', 'line': '1', 'path': '<D>/unreadable/x'},
        'cat-fsize': {'input': 'src'}, 'printf-fsize': {'script': 'bash', 'line': '1'},
        'sha256sum-read': {}, 'head-read': {}}
TRAP = 'trap \'ec=$?; echo "ERROR [internal] 予期しない失敗(終了コード $ec・行 $LINENO)" >&2; exit 20\' ERR'


def load():
    return json.loads(TABLE.read_text())


def check_static(table, legacy_source):
    """表の形と、旧版の一時ディレクトリのファイルの一覧が抜けなく対応することを確かめる。"""
    if table.get('schema') != 1:
        raise AssertionError('起動場所の表の形式が違います')
    names = set(re.findall(r'\$TMPD/([A-Za-z0-9_.-]+)', legacy_source))
    if names != set(table['temp_files']):
        raise AssertionError(f'一時ディレクトリのファイルの表が旧版と違います: {sorted(names ^ set(table["temp_files"]))}')
    if set(table['temp_files'].values()) - {'git-output-file', 'git-stdin-file', 'memory'}:
        raise AssertionError('一時ディレクトリのファイルの扱いが未知です')
    lines = len(legacy_source.splitlines())
    ids = [site['id'] for site in table['sites']]
    if len(set(ids)) != len(ids):
        raise AssertionError('起動場所の表のIDが重複しています')
    for site in table['sites']:
        if site['launched'] != (site['tool'] in LAUNCHED_TOOLS and 'match' in site):
            raise AssertionError(f'{site["id"]}: 起動するのはgitとERE照合のgrepだけです')
        for line in site['lines']:
            if line != 'helper' and not 1 <= line <= lines:
                raise AssertionError(f'{site["id"]}: 旧行が範囲外です')
            # Bashの組込みのprintfの書込みの失敗は、printfを動かす行(forの行)で知らされる。
            if site['tool'] == 'printf' and 'printf' not in legacy_source.splitlines()[line - 1]:
                raise AssertionError(f'{site["id"]}: {line}行はprintfを動かす行ではありません')
        for diagnostic in site.get('diagnostics', ()):
            filled = diagnostic['template']
            for key, value in FILL[diagnostic['recipe']].items():
                filled = filled.replace('{' + key + '}', value)
            if filled != diagnostic['measured']:
                raise AssertionError(f'{site["id"]}: 表の文の型と実測の文が一致しません')
    return {'sites': len(ids), 'temp_files': len(names)}


def _git_words(args):
    return ' '.join(a.decode('utf-8', 'surrogateescape') for a in args)


def classify(table, tool, args):
    """呼出しに当たる表の行のID。起動しない行は道具の名前だけで当てる。"""
    words = _git_words(args)
    hits = [site['id'] for site in table['sites'] if site['launched'] and site['tool'] == tool
            and re.search(site['match'], words)]
    if hits:
        return hits
    return [site['id'] for site in table['sites'] if not site['launched'] and site['tool'] == tool]


def check_launches(table, rust_calls, legacy_calls):
    """Rustの子はすべて起動する行に当たり、旧版の呼出しはすべて表のどこかに当たる。"""
    launched = {site['id'] for site in table['sites'] if site['launched']}
    for tool, args, _ in rust_calls:
        hits = classify(table, tool, args)
        if tool not in LAUNCHED_TOOLS or not hits or not set(hits) <= launched:
            raise AssertionError(f'表で起動しないとした子をRustが起動しました: {tool} {_git_words(args)[:120]}')
    for tool, args, _ in legacy_calls:
        if not classify(table, tool, args):
            raise AssertionError(f'旧版の呼出しが起動場所の表にありません: {tool} {_git_words(args)[:120]}')
    return len(rust_calls)


def versions():
    found = {}
    for name, (argv, pattern) in VERSION_COMMANDS.items():
        try:
            out = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=dict(os.environ, LC_ALL='C'),
                                 check=False).stdout.decode('utf-8', 'replace')
        except OSError:
            found[name] = None
            continue
        match = re.search(pattern, out.splitlines()[0] if out else '', re.M)
        found[name] = match.group(1) if match else None
    return found


def is_base(table, found=None):
    found = versions() if found is None else found
    return all(found.get(name) == value for name, value in table['base_versions'].items()), found


def _show(argv, cwd, env):
    done = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return done.stderr


def measure(work):
    """基準の版の道具を単体で動かし、表の失敗の文を測る(measure.shと同じ条件)。"""
    root = Path(tempfile.mkdtemp(prefix='launch-', dir=work))
    env = {'PATH': os.environ['PATH'], 'LC_ALL': 'C', 'HOME': str(root)}
    long_name = 'q' + 'x' * 300
    found = {}
    try:
        (root / 'ro').mkdir()
        (root / 'afile').write_bytes(b'')
        (root / 'unreadable').mkdir()
        (root / 'unreadable/x').write_bytes(b'')
        (root / 'src').write_bytes(b'data\n')
        (root / 'm1').write_bytes(b'')
        (root / 'target/src').mkdir(parents=True)
        (root / 'rr/inner').mkdir(parents=True)
        (root / 'rr/inner/f').write_bytes(b'')
        (root / 'ro').chmod(0o555)
        (root / 'unreadable').chmod(0)
        (root / 'rr/inner').chmod(0o555)
        found['mktemp-missing'] = _show(['mktemp', '-d'], root, dict(env, TMPDIR=str(root / 'missing')))
        found['mktemp-ro'] = _show(['mktemp', '-d', str(root / 'ro/.diff-snapshot.XXXXXX')], root, env)
        found['mkdir-ro'] = _show(['mkdir', '-p', '--', str(root / 'ro/sub')], root, env)
        found['mkdir-long'] = _show(['mkdir', '-p', '--', str(root / long_name / 'sub')], root, env)
        found['cp-create'] = _show(['cp', '--', 'src', str(root / 'ro/out.tmp')], root, env)
        found['cp-fsize'] = _show(['bash', '-c', "trap '' XFSZ; ulimit -f 0; cp -- src \"$1\"", 'bash',
                                   str(root / 'big.tmp')], root, env)
        # Rustが書くgitの入力ファイル(897のcat・977と1248のBashのprintf)の書込みの失敗。
        found['cat-fsize'] = _show(['bash', '-c', "trap '' XFSZ; ulimit -f 0; cat -- src >\"$1\"", 'bash',
                                    str(root / 'cat.tmp')], root, env)
        found['printf-fsize'] = _show(['bash', '-c', "trap '' XFSZ; ulimit -f 0; printf '%s\\0' src >>\"$1\"", 'bash',
                                       str(root / 'printf.tmp')], root, env)
        found['mv-overwrite-dir'] = _show(['mv', '--', 'src', 'target'], root, env)
        found['mv-ro'] = _show(['mv', '--', 'm1', str(root / 'ro/m1')], root, env)
        found['rm-dir'] = _show(['rm', '-f', '--', str(root / 'target')], root, env)
        found['rm-rf'] = _show(['rm', '-rf', '--', str(root / 'rr')], root, env)
        found['find-unreadable'] = _show(['find', './unreadable/.', '-mindepth', '1', '-maxdepth', '1', '-print',
                                          '-quit'], root, env)
        found['redirect'] = _show(['bash', '-c', 'wc -c <"$1"', 'bash', str(root / 'unreadable/x')], root, env)
        # 開けたファイルの読取りの失敗(279のsha256sum・1314と1315のhead)。ディレクトリへの向き先で起こす。
        found['sha256sum-read'] = _show(['bash', '-c', 'sha256sum <"$1"', 'bash', str(root / 'target')], root, env)
        found['head-read'] = _show(['bash', '-c', 'head -c 8000 <"$1"', 'bash', str(root / 'target')], root, env)
        # 旧版の組立て(ERR trapの行と、$(…)の中の失敗の2行)も同じbashで測る。
        script = root / 'trap.sh'
        script.write_text('set -eEuo pipefail\n' + TRAP + '\np="$1"\nx="$(wc -c <"$p" | tr -d \' \')"\n')
        found['redirect-trap'] = _show(['bash', str(script), str(root / 'unreadable/x')], root, env)
        script = root / 'cleanup.sh'
        script.write_text('set -eEuo pipefail\nD="$1"\ncleanup() { rm -rf -- "$D"; }\ntrap cleanup EXIT\n' + TRAP
                          + '\nexit 0\n')
        found['rm-trap-normal'] = _show(['bash', str(script), str(root / 'rr')], root, env)
    finally:
        for path in (root / 'ro', root / 'unreadable', root / 'rr/inner'):
            if path.exists():
                path.chmod(0o755)
        shutil.rmtree(root)
    return {name: value.replace(str(root).encode(), b'<D>').replace(long_name.encode(), b'<LONG>').decode(
        'utf-8', 'surrogateescape') for name, value in found.items()}


def check_measured(table, measured):
    """表の文と実測の文の一致。違ってよいのはパスの対応づけだけ。"""
    failures = []
    for site in table['sites']:
        for diagnostic in site.get('diagnostics', ()):
            if measured.get(diagnostic['recipe']) != diagnostic['measured']:
                failures.append(f'{site["id"]}:{diagnostic["name"]}')
    composed = {
        'redirect-trap': '<D>/trap.sh: line 4: <D>/unreadable/x: Permission denied\n'
                         'ERROR [internal] 予期しない失敗(終了コード 1・行 4)\n'
                         'ERROR [internal] 予期しない失敗(終了コード 20・行 4)\n',
        'rm-trap-normal': "rm: cannot remove '<D>/rr/inner/f': Permission denied\n"
                          'ERROR [internal] 予期しない失敗(終了コード 1・行 3)\n'}
    for name, expected in composed.items():
        if measured.get(name) != expected:
            failures.append(name)
    return failures
