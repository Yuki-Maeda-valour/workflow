#!/usr/bin/env python3
"""R4X-1専用比較。旧版 implement-agent.sh(sha256 9486ca07…)を変えず、同じ配置・同じ argv・同じ環境・同じ PATH・
同じ stdin・同じ umask で、検査専用入口 implement_agent_contract と比べる。

- 起動は C の起動の包み(launcher.c)。環境は並びのまま(重複・名前の無い項目も)渡す。新しいセッションを作る。
- 旧版と Rust は、同じ絶対パスの作業フォルダ(ケースごとに作り直す)で順に流す(パスの置き換えが要らない)。
- 比べるもの: 終了値(信号で終わればその番号)・stdout・stderr・作業フォルダと保護領域の候補の下の全エントリ
  (種類・mode・中身・リンク先)・PATH の観測用の包み(observer.c)が記録した道具の起動(argv・環境・fd 0〜2 の
  種類・信号の扱いと止め・cwd)。
- 旧版の道具の起動: どのケースでも 352 行の grep は 1 回まで、390 行の dirname は 1 回まで、391 行の basename は
  dirname が通ったときだけ 1 回まで。Rust が起動するのは決定の表の (1)(2) の場合だけで、そのときは旧版と Rust の
  argv・環境・fd・信号を比べる。ほかは旧版の argv が決まった形であることと、Rust の起動が 0 回であることを確かめる。
- 境界に届くケース: 旧版は PATH の先頭の観測用の mkdir で止まる(SIGKILL)。mkdir の argv の <cand> と、Rust の
  境界の行の <cand> を比べ、旧版の stdout・stderr と、Rust の境界の行の前までを比べる。
- 置き換えるのは、bash の文の先頭の旧版のパス(固定名 implement-agent.sh)と、旧版の bash 自身の行の訳
  (bash.mo・libc.mo から作った表で英語へ戻す。戻せない行は失敗)と、ジョブの知らせの行の pid(伏せて、記録した
  子の pid と同じかを確かめる)だけ。
- 最後の行は `合格: <N> 件(未実施 <M> 件)` か `不合格: …`。
"""
import argparse
import concurrent.futures
import ctypes
import dataclasses
import errno as errno_module
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'plugins/dev-workflow/skills/do-task/scripts'
OLD = SCRIPTS / 'implement-agent.sh'
FIXTURES = Path(__file__).with_name('implement_agent_fixtures')
USAGE_TXT = ROOT / 'native/crates/cli/src/commands/implement_agent/usage.txt'
OLD_SHA256 = '9486ca0790125567e44f047fbfe2b7db062fe63ef2145fb753f817a814891f58'
SYSTEM = 'linux' if sys.platform.startswith('linux') else 'darwin' if sys.platform == 'darwin' else 'other'
LINUX = SYSTEM == 'linux'
BOUNDARY = 99
FIXED_NAME = b'implement-agent.sh'
VERSIONS = {'bash': '5.2.21', 'coreutils': '9.4', 'sed': '4.9', 'grep': '3.11', 'glibc': '2.39'}
TOOL_NAMES = ('grep', 'dirname', 'basename', 'mkdir')
POINTS = ('grep', 'dirname', 'basename', 'boundary')
SIGNALS = {'TERM': signal.SIGTERM, 'HUP': signal.SIGHUP, 'INT': signal.SIGINT}
EXCLUDED_ENV = FIXTURES / 'excluded_env.json'
MAC_UNPERFORMED = FIXTURES / 'mac_unperformed.json'
MAC_FIXED = FIXTURES / 'mac_fixed.json'
# Mac の経路の模擬(Linux で、bash 4.0 以上が無い Mac と同じ流れを確かめる。合格に数えない。終了値 3)。
SIMULATE_MAC = os.environ.get('R4X1_SIMULATE_MAC') == '1'
SIMULATION_EXIT = 3
# C ライブラリの errno の文(基準の容器の glibc)。旧版の出力にあれば、Mac の固定期待に入れない。
ERRNO_TEXT = re.compile(b'|'.join(re.escape(os.strerror(n).encode()) for n in range(1, 134)
                                  if not os.strerror(n).startswith('Unknown error')))
CLOSED_FDS = FIXTURES / 'closed_fds.json'
PREFIX = 'R4X-1'

sys.path.insert(0, str(ROOT / 'scripts'))
import support_native  # noqa: E402  (比較器の共通の補助: プロセスの群の止め方)


class Mismatch(Exception):
    """旧版と Rust の結果の不一致。"""


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def fixture_path(value):
    """検査入口の絶対パス。省略・空・相対・不在・通常ファイル以外・実行不能は失敗。"""
    if not value or not os.path.isabs(value):
        raise ValueError('--fixture は今回ビルドした implement_agent_contract の絶対パスで指定してください')
    path = Path(value)
    if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError('--fixture は実行可能な通常ファイルである必要があります')
    return path


def check_sources():
    """旧版の sha256 と、使い方の文が旧版の 2 行目から番兵の前までと同じこと。"""
    if sha256_file(OLD) != OLD_SHA256:
        raise AssertionError('旧版の sha256 が違う(Rust の使い方の文と比較器を直す合図)')
    lines = OLD.read_bytes().split(b'\n')
    end = lines.index(b'# --- end usage ---')
    if b''.join(line + b'\n' for line in lines[1:end]) != USAGE_TXT.read_bytes():
        raise AssertionError('Rust の使い方の文が旧版の 2 行目から番兵の前までと違う')


# ---- 道具の版と環境 ----

def first_line(argv):
    try:
        done = subprocess.run(argv, capture_output=True, timeout=30,
                              env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
    except (OSError, subprocess.TimeoutExpired):
        return ''
    return done.stdout.decode(errors='replace').splitlines()[0] if done.stdout else ''


def glibc_version():
    try:
        libc = ctypes.CDLL(None)
        function = libc.gnu_get_libc_version
        function.restype = ctypes.c_char_p
        return function().decode()
    except (OSError, AttributeError):
        return ''


def tool_versions():
    found = {
        'bash': re.search(r'version (\d+\.\d+\.\d+)', first_line([BASH or 'bash', '--version'])),
        'coreutils': re.search(r'coreutils\) (\S+)', first_line(['dirname', '--version'])),
        'sed': re.search(r'GNU sed\) (\S+)', first_line(['sed', '--version'])),
        'grep': re.search(r'GNU grep\) (\S+)', first_line(['grep', '--version'])),
    }
    versions = {name: (match.group(1) if match else '') for name, match in found.items()}
    versions['glibc'] = glibc_version()
    return versions


def baseline():
    """基準の版と違う道具の名前と版(空なら基準の環境)。"""
    versions = tool_versions()
    return {name: versions.get(name, '') for name, want in VERSIONS.items() if versions.get(name) != want}


def language_pack():
    """日本語の翻訳ファイルの在りかと sha256(版で結果は分かれないので、在ることを確かめて記録するだけ)。"""
    found = {}
    for domain in ('bash', 'coreutils', 'libc'):
        for base in ('/usr/share/locale', '/usr/share/locale-langpack'):
            path = Path(base) / 'ja/LC_MESSAGES' / f'{domain}.mo'
            if path.is_file():
                found[domain] = (str(path), sha256_file(path)[:16])
                break
    return found


def core_state():
    """機械の core_pattern と、受け継いだ RLIMIT_CORE(記録する)。"""
    pattern = ''
    try:
        pattern = Path('/proc/sys/kernel/core_pattern').read_text().strip()
    except OSError:
        pattern = '(読めない)'
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
    return pattern, soft, hard


def apply_core_limit(cases, pattern):
    """core_pattern がファイル名なら、core を残しうる全部のケースで RLIMIT_CORE を 0 にする(比べる木に core を混ぜない)。"""
    if core_file_pattern(pattern):
        return [dataclasses.replace(c, core0=True) for c in cases]
    return list(cases)


def core_file_pattern(pattern):
    """core_pattern がファイル名(パイプでない)なら真(比べる木に core が混ざりうる)。"""
    return bool(pattern) and not pattern.startswith('|') and pattern != '(読めない)'


BASH = None


def find_bash():
    for candidate in ('/usr/bin/bash', '/bin/bash', '/opt/homebrew/bin/bash', '/usr/local/bin/bash'):
        if os.access(candidate, os.X_OK):
            done = subprocess.run([candidate, '-c', 'echo "${BASH_VERSINFO[0]}"'], capture_output=True, timeout=30)
            if done.stdout.strip().isdigit() and int(done.stdout.strip()) >= 4:
                return candidate
    return None


def real_tool(name):
    found = shutil.which(name, path='/usr/bin:/bin')
    if not found:
        raise AssertionError(f'{name} が /usr/bin:/bin に無い')
    return found


# ---- 組む道具 ----

class Tools:
    """観測用の包みと起動の包みを cc で組む。組めなければ比較全体を失敗にする。"""

    def __init__(self, base):
        base.mkdir(parents=True, exist_ok=True)
        compiler = shutil.which('cc')
        if not compiler:
            raise AssertionError('cc が無い(観測用の包みを組めない。比較全体を失敗にする)')
        self.observer = base / 'observer'
        self.launcher = base / 'launcher'
        for source, target in ((FIXTURES / 'observer.c', self.observer), (FIXTURES / 'launcher.c', self.launcher)):
            done = subprocess.run([compiler, '-O2', '-w', '-o', str(target), str(source)], capture_output=True)
            if done.returncode != 0:
                raise AssertionError(f'{source.name} を組めない: {done.stderr.decode(errors="replace")[:400]}')
        self.real = {name: real_tool(name) for name in TOOL_NAMES}


TOOLS = None
FIXTURE = None
EUCJP = None   # 生成した ja_JP.eucJP の LOCPATH(作れなければ None)


def make_eucjp(base):
    """基準の容器と Linux の CI で、ja_JP.eucJP を localedef で作る(root 以外なので LOCPATH の下)。"""
    target = base / 'locales'
    target.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(['localedef', '-i', 'ja_JP', '-f', 'EUC-JP', str(target / 'ja_JP.eucJP')],
                          capture_output=True, timeout=300, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
    if not (target / 'ja_JP.eucJP' / 'LC_CTYPE').is_file():
        raise AssertionError(f'ja_JP.eucJP を作れない: {done.stderr.decode(errors="replace")[:300]}')
    return target


# ---- 比較の入力に使わない環境変数 ----

def excluded_env():
    data = json.loads(EXCLUDED_ENV.read_text(encoding='utf-8'))
    return set(data['names']), tuple(data['prefixes']) + (tuple(data.get('mac_prefixes', ())) if not LINUX else ())


def check_input_env(entries, allowed):
    names, prefixes = excluded_env()
    for entry in entries:
        name = entry.split(b'=', 1)[0].decode(errors='replace')
        if (name in names or name.startswith(prefixes)) and name not in allowed:
            raise AssertionError(f'比較の入力に使わない環境変数をケースに使った: {name}')


# ---- 旧版の bash 自身の行を英語へ戻す(2026-11-11 22:31 の決定の当て方) ----

def read_mo(path):
    """`.mo` の msgid → msgstr(単数の形)。"""
    data = Path(path).read_bytes()
    magic = data[:4]
    order = '<' if magic == b'\xde\x12\x04\x95' else '>' if magic == b'\x95\x04\x12\xde' else None
    if order is None:
        raise AssertionError(f'{path} が .mo でない')
    count, originals, translations = struct.unpack_from(order + '3I', data, 8)
    out = {}
    for index in range(count):
        length, offset = struct.unpack_from(order + '2I', data, originals + index * 8)
        length2, offset2 = struct.unpack_from(order + '2I', data, translations + index * 8)
        out[data[offset:offset + length].split(b'\0')[0]] = data[offset2:offset2 + length2].split(b'\0')[0]
    return out


def find_mo(domain, language='ja'):
    for base in ('/usr/share/locale', '/usr/share/locale-langpack'):
        path = Path(base) / language / 'LC_MESSAGES' / f'{domain}.mo'
        if path.is_file():
            return path
    return None


# 旧版の区間で bash 自身が出す文の原文(bash 5.2.21 の msgid)。訳があれば、訳から原文へ戻す。
BASH_BODIES = (
    b'%s: integer expression expected',
    b'setlocale: LC_ALL: cannot change locale (%s)',
    b'setlocale: LC_ALL: cannot change locale (%s): %s',
    b'shell level (%d) too high, resetting to 1',
    b'command substitution: ignored null byte in input',
    b'write error: %s',
    b'%s: command not found',
)
CWD_ERROR = b'%s: error retrieving current directory: %s: %s\n'
GETCWD_ERRSTR = b'getcwd: cannot access parent directories'
CWD_LEAD = b'error retrieving current directory'
LINE_FORMS = (b'%s: line %d: ', b'line %d: ')
WARNING = b'warning: '
CORE = b'(core dumped) '
LONGEST_SIGNAL_DESC = 24
SIGNAL_DESCS = (b'Hangup', b'Interrupt', b'Quit', b'Illegal instruction', b'Trace/breakpoint trap', b'Aborted',
                b'Bus error', b'Floating point exception', b'Killed', b'User defined signal 1',
                b'Segmentation fault', b'User defined signal 2', b'Broken pipe', b'Alarm clock', b'Terminated',
                b'Stack fault', b'Child exited', b'Continued', b'Stopped (signal)', b'Stopped',
                b'Stopped (tty input)', b'Stopped (tty output)', b'Urgent I/O condition',
                b'CPU time limit exceeded', b'File size limit exceeded', b'Virtual timer expired',
                b'Profiling timer expired', b'Window changed', b'I/O possible', b'Power failure', b'Bad system call')


def template_regex(template):
    """`printf` の書式を、値を拾う正規表現にする(`%s` は最短、`%d` は数)。"""
    out = b''
    index = 0
    while index < len(template):
        if template[index:index + 1] == b'%' and index + 1 < len(template):
            conversion = template[index + 1:index + 2]
            if conversion == b's':
                out += b'(.*?)'
            elif conversion == b'd':
                out += b'(-?[0-9]+)'
            elif conversion == b'%':
                out += b'%'
            else:
                out += re.escape(template[index:index + 2])
            index += 2
            continue
        out += re.escape(template[index:index + 1])
        index += 1
    return re.compile(b'^' + out + b'$', re.S)


def fill(template, values):
    out = template
    for value in values:
        position = re.search(rb'%[sd]', out)
        out = out[:position.start()] + value + out[position.end():]
    return out.replace(b'%%', b'%')


class Back:
    """旧版の bash 自身の行を英語の原文へ戻す表(基準の容器の bash.mo と libc.mo から作る)。"""

    def __init__(self, bash=None, libc=None):
        bash_path, libc_path = find_mo('bash'), find_mo('libc')
        self.bash = bash if bash is not None else (read_mo(bash_path) if bash_path else {})
        libc_table = libc if libc is not None else (read_mo(libc_path) if libc_path else {})
        self.libc_back = {}
        for msgid, msgstr in libc_table.items():
            if msgid and msgstr and b'%' not in msgid and msgstr != msgid:
                self.libc_back.setdefault(msgstr, msgid)
        # C の地域設定の旧版は英語の名前のまま出すので、英語の名前もそのまま戻せるようにする。
        self.signal_back = {desc: desc for desc in SIGNAL_DESCS}
        self.signal_back.update({libc_table.get(desc, desc): desc for desc in SIGNAL_DESCS})
        self.bodies = [(body, self.translated(body)) for body in BASH_BODIES]
        self.cwd_error = (CWD_ERROR.rstrip(b'\n'), self.translated(CWD_ERROR).rstrip(b'\n'))
        self.lines = [self.translated(form) for form in LINE_FORMS]
        self.warning = self.translated(WARNING)
        self.core = self.translated(CORE)

    def translated(self, msgid):
        return self.bash.get(msgid) or msgid

    def value(self, text):
        """値の中の libc の訳(strerror など)を原文へ戻す。"""
        return self.libc_back.get(text, text)

    def body(self, text):
        """`warning: ` などの後ろの本文を戻す。戻せなければ None。"""
        if text.isascii():
            for english, _ in self.bodies:
                if template_regex(english).match(text):
                    return text
        for english, local in self.bodies:
            found = template_regex(local).match(text)
            if found:
                return fill(english, [self.value(group) for group in found.groups()])
        return text if text.isascii() else None

    def notice(self, text):
        """ジョブの知らせの本文(`%5d <信号の名前><詰め>[(core dumped) ]<命令>`)。"""
        found = re.match(rb'^( *[0-9]+) (.*)$', text, re.S)
        if not found:
            return None
        pid, rest = found.groups()
        for local, english in sorted(self.signal_back.items(), key=lambda item: -len(item[0])):
            if not rest.startswith(local):
                continue
            tail = rest[len(local):]
            pad = max(LONGEST_SIGNAL_DESC - len(local), 0) if len(local) <= LONGEST_SIGNAL_DESC else len(local) - LONGEST_SIGNAL_DESC
            # bash は詰めを `%*s`(負なら左寄せの幅)で出す。訳の長さで詰めた空白を外す。
            width = LONGEST_SIGNAL_DESC - len(local)
            spaces = width if width >= 0 else -width
            if not tail.startswith(b' ' * spaces):
                continue
            tail = tail[spaces:]
            core = b''
            for mark in (self.core, CORE):
                if tail.startswith(mark):
                    core, tail = CORE, tail[len(mark):]
                    break
            del pad
            english_width = LONGEST_SIGNAL_DESC - len(english)
            padding = b' ' * (english_width if english_width >= 0 else -english_width)
            return pid + b' ' + english + padding + core + tail
        return None

    def english(self, line, codeset='UTF-8'):
        """1 行(改行を除く)を戻す。bash 自身の行でなければそのまま。戻せない行は Mismatch。"""
        def decode(part):
            if codeset == 'EUC-JP':
                try:
                    return part.decode('euc_jp').encode('utf-8')
                except UnicodeDecodeError:
                    return part
            return part

        head = FIXED_NAME + b': '
        if line.startswith(head):
            rest = decode(line[len(head):])
            number = None
            for form in [b'line %d: '] + [f for f in self.lines]:
                found = template_regex(form + b'%s').match(rest)
                if found:
                    number, rest = found.group(1), found.group(2)
                    break
            if number is None:
                found = re.match('^行 ([0-9]+): (.*)$'.encode(), rest, re.S)
                if found:
                    number, rest = found.groups()
            if number is None:
                raise Mismatch(f'bash の行の番号を読めない: {line!r}')
            prefix = head + b'line ' + number + b': '
            for mark in (WARNING, self.warning):
                if rest.startswith(mark):
                    body = self.body(rest[len(mark):])
                    if body is None:
                        raise Mismatch(f'bash の警告の行を英語へ戻せない: {line!r}')
                    return prefix + WARNING + body
            if re.match(rb'^ *[0-9]+ ', rest):
                notice = self.notice(rest)
                if notice is None:
                    raise Mismatch(f'ジョブの知らせの行を英語へ戻せない: {line!r}')
                return prefix + notice
            body = self.body(rest)
            if body is None:
                # 組み込みの命令の名前の後ろの本文(`[: …`・`echo: …`)。
                found = re.match(rb'^([^:]+): (.*)$', rest, re.S)
                if found:
                    inner = self.body(found.group(2))
                    if inner is not None:
                        return prefix + found.group(1) + b': ' + inner
                raise Mismatch(f'bash の行を英語へ戻せない: {line!r}')
            return prefix + body
        if line.startswith(b'bash: '):
            rest = decode(line[len(b'bash: '):])
            for mark in (WARNING, self.warning):
                if rest.startswith(mark):
                    body = self.body(rest[len(mark):])
                    if body is None:
                        raise Mismatch(f'bash の起動の警告を英語へ戻せない: {line!r}')
                    return b'bash: ' + WARNING + body
            if not rest.isascii():
                raise Mismatch(f'bash の起動の行を英語へ戻せない: {line!r}')
            return b'bash: ' + rest
        for who in (b'shell-init', b'job-working-directory', b'pwd'):
            if line.startswith(who + b': '):
                # bash 5.2.21 は `<名>: ` + 「error retrieving current directory」(訳) + `: ` +
                # bash_getcwd_errstr(訳) + `: ` + strerror(libc の訳)を出す(基準の容器で測った)。
                text = decode(line)
                errstrs = b'|'.join(re.escape(e) for e in (GETCWD_ERRSTR, self.translated(GETCWD_ERRSTR)))
                for template in self.cwd_error:
                    _, _, tail = template.partition(b'%s')
                    # 書式の `%s` を順に: 名前・bash_getcwd_errstr(訳の有無の両方)・strerror。
                    parts = tail.split(b'%s')
                    if len(parts) != 3:
                        continue
                    regex = (b'^(.*?)' + re.escape(parts[0]) + b'(' + errstrs + b')' + re.escape(parts[1]) +
                             b'(.*)' + re.escape(parts[2]) + b'$')
                    found = re.match(regex, text, re.S)
                    if found:
                        return fill(self.cwd_error[0], [found.group(1), GETCWD_ERRSTR, self.value(found.group(3))])
                raise Mismatch(f'cwd の誤りの行を英語へ戻せない: {line!r}')
        return line


BACK = None


def normalize_old(stderr, codeset='UTF-8'):
    """旧版の stderr: bash の文の先頭の旧版のパスを固定名にし、bash 自身の行を英語へ戻す。"""
    stderr = stderr.replace(str(OLD).encode() + b': ', FIXED_NAME + b': ')
    lines = stderr.split(b'\n')
    out = [BACK.english(line, codeset) for line in lines[:-1]]
    out.append(lines[-1])
    return b'\n'.join(out)


PID_NOTICE = re.compile(rb'^(implement-agent\.sh: line 354: )( *)([0-9]+)( )', re.M)


def mask_pids(stderr):
    """ジョブの知らせの行の pid を伏せ、伏せた pid の並びを返す(06:58 の決定)。pid の字面が bash の `%5ld` の
    形(右寄せの5桁)でなければ、伏せずに残す(差として見えるように)。"""
    pids = []

    def replace(found):
        pid = int(found.group(3))
        if (found.group(2) + found.group(3)) != b'%5d' % pid:
            return found.group(0)
        pids.append(pid)
        return found.group(1) + b'<pid>' + found.group(4)

    masked = PID_NOTICE.sub(replace, stderr)
    return masked, pids


# ---- ケース ----

def b(value):
    return value if isinstance(value, bytes) else str(value).encode()


@dataclasses.dataclass
class Case:
    """1 つの比較。`@R` はケースの根(同じ絶対パスで旧版と Rust を順に流す)、`@OBS` は観測用の包みのフォルダ。"""

    group: str
    label: str
    args: list
    env: dict = None          # 既定の環境(PATH・HOME)に足す・上書きする値。None の値は消す
    raw_env: list = None      # 環境の項目の並び(これを渡す。重複・名前の無い項目も)
    setup: object = None      # setup(root: Path)
    cwd: str = 'work'         # 根からの相対。'GONE' は起動の前に消したフォルダ
    stdin: bytes = None       # None は /dev/null
    stdout: str = 'file'      # 'file'・'devfull'・'closed'・'devnull'
    stderr: str = 'file'
    fsize: int = None
    core0: bool = False
    core1: bool = False       # RLIMIT_CORE を 1 にする(子が core を残さないことを決める。ジョブの知らせのケース)
    ignore: tuple = ()
    block: tuple = ()
    stop: tuple = None        # (地点, 信号の名前)
    eucjp: bool = False       # Rust が dirname・basename を起動する(決定の表の (2))
    allowed_env: tuple = ()   # 比較の入力に使わない環境変数のうち、このケースだけで使うもの
    mac: bool = True          # Mac の範囲で流すか
    repeat: int = 1
    needs: tuple = ()         # 旧版が必ず起動する道具(比べる前提が成り立ったことの確かめ)
    close: tuple = ()         # 起動時に閉じる標準の fd(固定期待の段だけ)
    wrapper: bool = True      # PATH の観測用の包みを置くか(包みの有無の段だけ外す)
    expect: object = None     # expect(side, root, legacy) -> 問題の一覧(完了条件の決まった値)

    @property
    def name(self):
        return f'{self.group}/{self.label}'


def base_env(root, case):
    entries = {
        'PATH': f'{root}/obs/bin:/usr/bin:/bin' if case.wrapper else '/usr/bin:/bin',
        'HOME': f'{root}/home',
    }
    if case.env:
        for key, value in case.env.items():
            if value is None:
                entries.pop(key, None)
            else:
                entries[key] = value
    return [b(key) + b'=' + b(value) for key, value in entries.items()]


def expand(value, root, wrapper=True):
    value = b(value)
    if EUCJP is not None:
        value = value.replace(b'@LOC', str(EUCJP).encode())
    if not wrapper:
        value = value.replace(b'@OBS:', b'')
    return value.replace(b'@OBS', str(root).encode() + b'/obs/bin').replace(b'@R', str(root).encode())


def default_layout(root):
    (root / 'work').mkdir()
    (root / 'home').mkdir()
    (root / 'p.md').write_bytes(b'hello\n')


@dataclasses.dataclass
class Side:
    returncode: int
    stdout: bytes
    stderr: bytes
    tree: dict
    records: dict
    marks: list
    pid: int
    timed_out: bool = False
    initial: dict = None


def read_record(path):
    data = path.read_bytes()
    record = {'argv': [], 'env': []}
    index = 0
    while index < len(data):
        end = data.index(b'\n', index)
        head = data[index:end].split(b' ', 1)
        key = head[0].decode()
        value = head[1] if len(head) > 1 else b''
        index = end + 1
        if key in ('arg', 'env', 'cwd'):
            length = int(value)
            field = data[index:index + length]
            index += length + 1
            if key == 'arg':
                record['argv'].append(field)
            elif key == 'env':
                record['env'].append(field)
            else:
                record['cwd'] = field
        else:
            record[key] = value
    return record


def records_of(root):
    out = {}
    log = root / 'obs' / 'log'
    for path in sorted(log.iterdir()) if log.is_dir() else []:
        tool, number = path.name.rsplit('.', 1)
        out.setdefault(tool, []).append((int(number), read_record(path)))
    return {tool: [record for _, record in sorted(items)] for tool, items in out.items()}


def snapshot(root):
    """根の下の全エントリ(包み・印・入出力のフォルダを除く)の種類・mode・中身(sha256)・リンク先。
    読めないファイルは中身の代わりに `unreadable` と記録する(権限を変えない)。"""
    out = {}

    def visit(path, key):
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            out[key] = ('symlink', mode, os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            try:
                digest = sha256_file(path)
            except PermissionError:
                digest = 'unreadable'
            out[key] = ('file', mode, digest)
        elif stat.S_ISDIR(info.st_mode):
            out[key] = ('directory', mode, None)
            try:
                children = sorted(path.iterdir())
            except PermissionError:
                out[key] = ('directory', mode, 'unreadable')
                return
            for child in children:
                visit(child, f'{key}/{child.name}')
        elif stat.S_ISFIFO(info.st_mode):
            out[key] = ('fifo', mode, None)
        else:
            out[key] = ('other', mode, None)

    for item in sorted(root.iterdir()):
        if item.name in ('obs', 'stop', 'io'):
            continue
        visit(item, item.name)
    return out


TIMEOUT = 120


def prepare(case, root):
    if root.exists():
        make_writable(root)
        shutil.rmtree(root)
    root.mkdir(parents=True)
    for name in ('obs/bin', 'obs/log', 'stop', 'io'):
        (root / name).mkdir(parents=True)
    for tool in TOOL_NAMES:
        shutil.copy(TOOLS.observer, root / 'obs/bin' / tool)
    point = case.stop[0] if case.stop else 'none'
    mode = 'wait' if point == 'boundary' else 'kill'
    lines = [f'real.{tool}={TOOLS.real[tool]}' for tool in TOOL_NAMES]
    lines += [f'log={root}/obs/log', f'stop={point}', f'stopdir={root}/stop', f'mkdir={mode}']
    (root / 'obs/conf').write_text('\n'.join(lines) + '\n')
    os.mkfifo(root / 'stop/fifo')
    (root / 'stop/point').write_text(point + '\n')
    (case.setup or default_layout)(root)
    if case.cwd == 'GONE':
        (root / 'gone').mkdir(exist_ok=True)


def make_writable(path):
    """消す前に、根の下のフォルダとファイルを読み書きできるようにする(symlink は辿らず、当てない)。"""
    for current, dirs, files in os.walk(path):
        for name in dirs:
            entry = os.path.join(current, name)
            if os.path.islink(entry):
                continue
            try:
                os.chmod(entry, 0o755)
            except OSError:
                pass
        for name in files:
            entry = os.path.join(current, name)
            if os.path.islink(entry):
                continue
            try:
                if not os.access(entry, os.R_OK | os.W_OK):
                    os.chmod(entry, 0o644)
            except OSError:
                pass
    try:
        if stat.S_ISDIR(os.lstat(path).st_mode):
            os.chmod(path, 0o755)
    except OSError:
        pass


def open_stream(kind, path, write=True):
    if kind == 'file':
        return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if kind == 'devfull':
        return os.open('/dev/full', os.O_WRONLY)
    if kind == 'devnull':
        return os.open('/dev/null', os.O_WRONLY)
    if kind == 'closed':
        read_end, write_end = os.pipe()
        os.close(read_end)
        return write_end
    raise ValueError(kind)


def args_prefix():
    return [str(OLD)]


def run_side(case, root, legacy):
    """1 つの側を流す。"""
    prepare(case, root)
    entries = [expand(e, root, case.wrapper) for e in case.raw_env] if case.raw_env is not None else base_env(root, case)
    entries = [expand(e, root, case.wrapper) for e in entries]
    check_input_env(entries, case.allowed_env)
    initial = snapshot(root)
    if case.cwd == 'GONE':
        # 起動の包みが exec の前に消すフォルダ(比較の前提)。
        initial.pop('gone', None)
    env_file = root / 'io' / 'env'
    env_file.write_bytes(b''.join(e + b'\0' for e in entries))
    options = []
    if case.fsize is not None:
        options += ['--fsize', str(case.fsize)]
    if case.core1:
        options += ['--core1']
    elif case.core0:
        options += ['--core0']
    for number in case.ignore:
        options += ['--ignore', str(int(number))]
    for number in case.block:
        options += ['--block', str(int(number))]
    for number in getattr(case, 'close', ()):
        options += ['--close', str(int(number))]
    if case.cwd == 'GONE':
        options += ['--rmdir', str(root / 'gone')]
    options += ['--env-file', str(env_file)]
    args = [expand(a, root) for a in case.args]
    if legacy and getattr(case, 'strace', None):
        program = [str(TOOLS.launcher), *options, '--', shutil.which('strace'), 'strace', '-f', '-qq', '-e',
                   'trace=execve', '-e', 'signal=none', '-v', '-s', '1048576', '-o', str(case.strace),
                   BASH, *args_prefix(), *args]
    elif legacy:
        program = [str(TOOLS.launcher), *options, '--', BASH, 'bash', str(OLD), *args]
    else:
        stop = [b'--r4x1-stop', str(root / 'stop').encode()] if case.stop else []
        program = [str(TOOLS.launcher), *options, '--', str(FIXTURE), 'implement_agent_contract', *stop, *args]
    if case.stdin is None:
        stdin = os.open('/dev/null', os.O_RDONLY)
    else:
        (root / 'io' / 'stdin').write_bytes(case.stdin)
        stdin = os.open(root / 'io' / 'stdin', os.O_RDONLY)
    stdout = open_stream(case.stdout, root / 'io' / 'stdout')
    stderr = open_stream(case.stderr, root / 'io' / 'stderr')
    cwd = root / case.cwd if case.cwd != 'GONE' else root / 'gone'
    process = None
    try:
        process = subprocess.Popen(program, cwd=cwd, stdin=stdin, stdout=stdout, stderr=stderr,
                                   start_new_session=True, env={})
    finally:
        for fd in (stdin, stdout, stderr):
            os.close(fd)
    timed_out = False
    try:
        if case.stop:
            point, name = case.stop
            mark = root / 'stop' / f'stop-{point}'
            deadline = time.monotonic() + TIMEOUT
            while not mark.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.005)
            if mark.exists() and process.poll() is None:
                os.kill(process.pid, SIGNALS[name])
            try:
                fd = os.open(root / 'stop' / 'fifo', os.O_WRONLY | os.O_NONBLOCK)
                try:
                    os.write(fd, b'g')
                finally:
                    os.close(fd)
            except OSError as error:
                # 読み手が先に終わった(ENXIO)か、開いた後に終わった(EPIPE)。
                if error.errno not in (errno_module.ENXIO, errno_module.EPIPE):
                    raise
        try:
            process.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            timed_out = True
            support_native.signal_group(process, signal.SIGKILL)
            process.wait()
        records = records_of(root)
        # 残った包みと道具が終わるのを待つ(比較の共通の補助)。
        pids = [int(r['pid']) for items in records.values() for r in items if 'pid' in r]
        if not support_native.wait_processes_gone(pids, TIMEOUT):
            timed_out = True
        records = records_of(root)
    finally:
        try:
            support_native.signal_group(process, signal.SIGKILL)
        except PermissionError:
            pass
    out = (root / 'io' / 'stdout').read_bytes() if case.stdout == 'file' else b''
    err = (root / 'io' / 'stderr').read_bytes() if case.stderr == 'file' else b''
    marks = sorted(p.name for p in (root / 'stop').iterdir() if p.name.startswith('stop-'))
    tree = snapshot(root)
    return Side(process.returncode, out, err, tree, records, marks, process.pid, timed_out, initial)


# ---- 比べ方 ----

def last_value(args, name):
    """旧版の引数の解析で、その名前の最後の値(値の無い名前は None)。"""
    value = None
    words = [b(a) for a in args]
    index = 0
    names = {b'--runner', b'--prompt-file', b'--cwd', b'--model', b'--log-file', b'--probe-timeout', b'--run-timeout'}
    while index < len(words):
        word = words[index]
        if word == b'--dry-run':
            index += 1
            continue
        if word in names:
            if index + 1 >= len(words):
                return value
            if word == name:
                value = words[index + 1]
            index += 2
            continue
        return value
    return value


def locale_codeset(entries):
    """旧 bash の地域設定の文字コード(訳した行を UTF-8 へ戻すため)。"""
    env = {}
    for entry in entries:
        if b'=' in entry:
            key, value = entry.split(b'=', 1)
            env.setdefault(key, value)
    name = env.get(b'LC_ALL') or env.get(b'LC_MESSAGES') or env.get(b'LANG') or b''
    return 'EUC-JP' if b'euc' in name.lower() else 'UTF-8'


NOT_REPRODUCED = (b'LOG_SWAP_PY',)


def env_parts(entries):
    kept = sorted(e for e in entries if e.split(b'=', 1)[0] not in NOT_REPRODUCED)
    removed = {e.split(b'=', 1)[0]: e.split(b'=', 1)[1] for e in entries if e.split(b'=', 1)[0] in NOT_REPRODUCED}
    return kept, removed


def compare_records(case, root, old, rust, inherited, problems):
    prompt = last_value(case.args, b'--prompt-file') or b''
    log_file = last_value(case.args, b'--log-file') or b''
    launches = set()
    if prompt.startswith(b'-'):
        launches.add('grep')
    if case.eucjp:
        launches |= {'dirname', 'basename'}
    expected_argv = {
        'grep': [b'grep', b'-q', b'[^[:space:]]', expand(prompt, root)],
        'dirname': [b'dirname', expand(log_file, root)],
        'basename': [b'basename', expand(log_file, root)],
    }
    for tool in ('grep', 'dirname', 'basename'):
        old_items = old.records.get(tool, [])
        rust_items = rust.records.get(tool, [])
        if len(old_items) > 1:
            problems.append(f'旧版の {tool} の起動が {len(old_items)} 回')
        for record in old_items:
            if record['argv'] != expected_argv[tool]:
                problems.append(f'旧版の {tool} の argv が決まった形でない: {record["argv"]!r}')
            # dirname・basename はコマンド置換のサブシェルの子(ERR の罠を受け継ぐので exec しない)。
            if tool == 'grep' and int(record.get('ppid', b'0')) != old.pid:
                problems.append(f'旧版の {tool} の親が旧版でない')
        if tool == 'basename' and old_items and not old.records.get('dirname'):
            problems.append('旧版が dirname より先に basename を起動した')
        if tool not in launches:
            if rust_items:
                problems.append(f'Rust が {tool} を起動した(決定の表の (1)(2) に当たらない)')
            continue
        if len(old_items) != len(rust_items):
            problems.append(f'{tool} の起動の回数: 旧版 {len(old_items)}・Rust {len(rust_items)}')
            continue
        for o, r in zip(old_items, rust_items):
            if int(r.get('ppid', b'0')) != rust.pid:
                problems.append(f'Rust の {tool} の親が Rust でない')
            for key in ('argv', 'cwd', 'fd0', 'fd1', 'fd2', 'ignored', 'blocked'):
                if o.get(key) != r.get(key):
                    problems.append(f'{tool} の {key}: 旧版 {o.get(key)!r}・Rust {r.get(key)!r}')
            old_env, old_removed = env_parts(o['env'])
            rust_env, rust_removed = env_parts(r['env'])
            if old_env != rust_env:
                only_old = sorted(set(old_env) - set(rust_env))
                only_rust = sorted(set(rust_env) - set(old_env))
                problems.append(f'{tool} の子の環境: 旧版だけ {only_old[:6]!r}・Rust だけ {only_rust[:6]!r}')
            for name in NOT_REPRODUCED:
                if rust_removed.get(name) != inherited.get(name):
                    problems.append(f'{tool} の {name.decode()} が受け継いだ値と違う(14:24 の決定): '
                                    f'{rust_removed.get(name)!r}')


def split_boundary(stderr, want):
    """Rust の stderr の末尾の境界の行(途中で切れたものも。行の頭から始まる want の頭の部分)と、その前。"""
    for length in range(min(len(want), len(stderr)), 0, -1):
        at = len(stderr) - length
        if stderr[at:] == want[:length] and (at == 0 or stderr[at - 1:at] == b'\n'):
            return stderr[:at], stderr[at:]
    return stderr, b''


def boundary_line(cand):
    return b'ERROR [r4x-1-boundary] ' + '保護領域の候補 '.encode() + cand + ' の mkdir から後は R4X-2 で移す'.encode() + b'\n'


def compare_sides(case, root, old, rust, reference=None):
    """問題の一覧を返す(空なら一致)。reference は、途中で切れる出力の決定(22:26・22:38)の、故障の無い旧版。"""
    problems = []
    if old.timed_out or rust.timed_out:
        problems.append(f'時間切れ(旧版 {old.timed_out}・Rust {rust.timed_out})')
        return problems
    entries = [expand(e, root) for e in case.raw_env] if case.raw_env is not None else base_env(root, case)
    inherited = {}
    for entry in entries:
        if b'=' in entry:
            key, value = entry.split(b'=', 1)
            inherited[key] = value
    codeset = locale_codeset(entries)
    old_err = normalize_old(old.stderr, codeset)
    expected_code = old.returncode
    if reference is not None:
        full = normalize_old(reference.stderr, codeset)
        if len(full) <= case.fsize:
            # (2) 置き換えた書き込みが切れない: 故障の無い旧版に合わせ、終了値の差を認める。
            old_err, expected_code = full, reference.returncode
        else:
            # (3) 両方が切れる: 置き換えた出力を同じバイト数で切った物。
            old_err = full[:case.fsize]
    old_err, old_pids = mask_pids(old_err)
    rust_err, rust_pids = mask_pids(rust.stderr)
    for pids, side, label in ((old_pids, old, '旧版'), (rust_pids, rust, 'Rust')):
        recorded = [int(r['pid']) for r in side.records.get('grep', [])]
        for pid in pids:
            if pid not in recorded:
                problems.append(f'{label} のジョブの知らせの pid {pid} が記録した子の pid {recorded} でない')
    signal_sent = case.stop is not None and case.stop[0] == 'boundary'
    trapped = signal_sent and SIGNALS[case.stop[1]] not in case.ignore
    old_mkdir = old.records.get('mkdir', [])
    if rust.records.get('mkdir'):
        problems.append('Rust が mkdir を起動した')
    if rust.returncode == BOUNDARY:
        if trapped:
            problems.append('罠のある信号を境界で送ったのに、Rust が境界の印を出した')
        if not old_mkdir:
            problems.append('Rust は境界の印を出したが、旧版は mkdir まで進まなかった')
            cand = None
        else:
            argv = old_mkdir[0]['argv']
            cand = argv[2] if len(argv) == 3 and argv[:2] == [b'mkdir', b'-p'] else None
            if cand is None:
                problems.append(f'旧版の最初の mkdir の argv が決まった形でない: {argv!r}')
            if signal_sent and len(old_mkdir) != 2:
                problems.append(f'起動時に無視した信号の境界のケースで、旧版の mkdir が {len(old_mkdir)} 回')
            if old.returncode != -signal.SIGKILL:
                problems.append(f'旧版が観測用の mkdir で止まっていない(終了値 {old.returncode})')
        if cand is not None:
            want = boundary_line(cand)
            before, line = split_boundary(rust_err, want)
            if line != want and (case.fsize is None and case.stderr == 'file'):
                problems.append(f'境界の行が違う: {line!r}(旧版の候補 {cand!r})')
        else:
            before, line = rust_err, b''
        if before != old_err:
            problems.append(diff_text('stderr(境界の行の前)', old_err, before))
    else:
        if old_mkdir and not trapped:
            problems.append(f'旧版は mkdir まで進んだが、Rust は境界の印を出さなかった(終了値 {rust.returncode})')
        if trapped and len(old_mkdir) != 1:
            problems.append(f'罠のある信号の境界のケースで、旧版の mkdir が {len(old_mkdir)} 回')
        if expected_code != rust.returncode:
            problems.append(f'終了値: 旧版 {expected_code}・Rust {rust.returncode}')
        if old_err != rust_err:
            problems.append(diff_text('stderr', old_err, rust_err))
    if old.stdout != rust.stdout:
        problems.append(diff_text('stdout', old.stdout, rust.stdout))
    if old.tree != rust.tree:
        keys = sorted(set(old.tree) | set(rust.tree))
        changed = [k for k in keys if old.tree.get(k) != rust.tree.get(k)]
        problems.append(f'ファイルの残りが違う: {changed[:8]}')
    # この区間で旧版はファイルを作らない(作業フォルダと保護領域の候補の下に何もできない)。
    for label, side in (('旧版', old), ('Rust', rust)):
        if side.tree != side.initial:
            keys = sorted(set(side.tree) | set(side.initial))
            changed = [k for k in keys if side.tree.get(k) != side.initial.get(k)]
            problems.append(f'{label} の後にファイルが変わった: {changed[:8]}')
    if case.stop:
        want = [f'stop-{case.stop[0]}']
        if old.marks != want or rust.marks != want:
            problems.append(f'止めた地点が違う: 期待 {want}・旧版 {old.marks}・Rust {rust.marks}')
    for tool in case.needs:
        if not old.records.get(tool):
            problems.append(f'旧版が {tool} まで進まなかった(ケースの前提が成り立たない)')
    if case.expect is not None:
        for label, side in (('旧版', old), ('Rust', rust)):
            for problem in case.expect(side, root, side is old):
                problems.append(f'{label}: {problem}')
    compare_records(case, root, old, rust, inherited, problems)
    return problems


def diff_text(label, old, new, limit=500):
    at = next((i for i, (x, y) in enumerate(zip(old, new)) if x != y), min(len(old), len(new)))
    start = max(at - 80, 0)
    return (f'{label} が違う({at} バイト目から): 旧版 {old[start:start + limit]!r} / '
            f'Rust {new[start:start + limit]!r}')


def needs_reference(case, old):
    """旧版の stderr が上限まで書かれた(途中で切れたかもしれない)ときだけ、故障の無い旧版と比べる。上限より短ければ
    切れていないので、置き換えた旧版の出力とそのまま比べる(故障が子の終わり方を変えるケースを含む)。"""
    return case.fsize is not None and case.stderr == 'file' and len(old.stderr) >= case.fsize


def run_case(case, root):
    """旧版と Rust を流して比べる。(問題の一覧, 境界の印を受けたか)。"""
    try:
        old = run_side(case, root, legacy=True)
        reference = None
        if needs_reference(case, old):
            free = dataclasses.replace(case, fsize=None)
            reference = run_side(free, root, legacy=True)
            if str(OLD).encode() not in reference.stderr:
                reference = None
        rust = run_side(case, root, legacy=False)
        problems = compare_sides(case, root, old, rust, reference)
        return problems, rust.returncode == BOUNDARY, (old, rust)
    except Mismatch as error:
        return [str(error)], False, None
    except Exception:  # noqa: BLE001  比較器の誤りもケースの不合格として出す
        return [f'比較器の例外: {traceback.format_exc()[-1500:]}'], False, None
    finally:
        if root.exists():
            make_writable(root)
            shutil.rmtree(root, ignore_errors=True)


# ---- 群 ----

P = '@R/p.md'
W = '@R/work'
VALID = ['--runner', 'codex', '--prompt-file', P, '--cwd', W]
OPTIONS = ('--runner', '--prompt-file', '--cwd', '--model', '--log-file', '--probe-timeout', '--run-timeout')


def layout(prompt=b'hello\n', files=None, dirs=(), links=None, fifos=(), modes=None, after=None):
    """既定の配置に足す。files は根からの相対パス → 中身、links は → リンク先。"""
    def setup(root):
        default_layout(root)
        if prompt is not None:
            (root / 'p.md').write_bytes(prompt)
        for name in dirs:
            (root / name).mkdir(parents=True, exist_ok=True)
        for name, data in (files or {}).items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for name, target in (links or {}).items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(expand(target, root), path)
        for name in fifos:
            os.mkfifo(root / name)
        for name, mode in (modes or {}).items():
            os.chmod(root / name, mode)
        if after:
            after(root)
    return setup


def expect_condition_3(side, root, legacy):
    cand = str(root).encode() + b'/work/home/.local/state/dev-workflow'
    problems = []
    if legacy:
        mkdirs = side.records.get('mkdir', [])
        if not mkdirs or mkdirs[0]['argv'] != [b'mkdir', b'-p', cand]:
            problems.append(f'観測用の mkdir が -p {cand!r} で止めていない')
        if side.stdout or side.stderr:
            problems.append('止まるまでの stdout か stderr が空でない')
    else:
        if side.returncode != BOUNDARY:
            problems.append(f'終了値が 99 でない: {side.returncode}')
        if side.stderr.split(b'\n')[-2:] != [boundary_line(cand)[:-1], b'']:
            problems.append(f'stderr の最後の行が境界の行でない: {side.stderr[-200:]!r}')
        if side.stdout or side.stderr != boundary_line(cand):
            problems.append('境界の行の前の stdout か stderr が空でない')
    return problems


def expect_condition_4(side, root, legacy):
    want = 'ERROR [usage] --cwd が必要です(実装経路では省略できない。書き込み範囲を呼び出し側が明示する)\n'.encode()
    problems = []
    if side.returncode != 2:
        problems.append(f'終了値が 2 でない: {side.returncode}')
    if side.stderr != want:
        problems.append(f'stderr がその 1 行だけでない: {side.stderr[:200]!r}')
    return problems


def expect_condition_5(side, root, legacy):
    want = 'ERROR [usage] --runner に値が必要です\n'.encode() + USAGE_TXT.read_bytes()
    problems = []
    if side.returncode != 2:
        problems.append(f'終了値が 2 でない: {side.returncode}')
    if side.stderr != want:
        problems.append('stderr が「--runner に値が必要です」の行と、旧版の 2〜98 行の使い方でない')
    return problems


def expect_condition_6(side, root, legacy):
    want = 'ERROR [aborted] シグナル TERM を受信したため中止した(外部ランナーのプロセスグループと直接の子プロセスに終了のシグナルを送った)\n'.encode()
    problems = []
    if side.returncode != 143:
        problems.append(f'終了値が 143 でない: {side.returncode}')
    if not side.stderr.endswith(want):
        problems.append(f'stderr の最後の行が中止の行でない: {side.stderr[-200:]!r}')
    if side.tree != side.initial:
        problems.append('作業フォルダか候補の下が変わった')
    return problems


def expect_grep_notice(side, root, legacy):
    """`--help` を RLIMIT_FSIZE 1000 で起動した grep が SIGXFSZ で死に、ジョブの知らせの行がちょうど 1 つ出ること。
    環境の違いで grep が死ななくなったら、知らせの行を比べる前提が崩れたので失敗にする。"""
    text = side.stderr
    if legacy:
        try:
            text = normalize_old(text)
        except Mismatch as error:
            return [f'知らせの行を英語へ戻せない: {error}']
    _, pids = mask_pids(text)
    notices = [line for line in text.split(b"\n") if PID_NOTICE.match(line)]
    problems = []
    if len(notices) != 1 or b' File size limit exceeded' not in notices[0]:
        problems.append(f'ジョブの知らせの行(File size limit exceeded)がちょうど 1 つでない: {notices!r}')
    if len(pids) != 1:
        problems.append(f'伏せた pid が 1 つでない: {pids}')
    return problems


def expect_condition_7(side, root, legacy):
    problems = []
    if side.returncode != -signal.SIGTERM:
        problems.append(f'信号 TERM で終わっていない: {side.returncode}')
    if side.stdout or side.stderr:
        problems.append('stdout か stderr が空でない')
    return problems


def group_arguments():
    cases = []
    add = lambda label, args, **kw: cases.append(Case('引数', label, args, **kw))  # noqa: E731
    for option in OPTIONS:
        add(f'値なし{option}', [option])
        add(f'値なし{option}-後ろ', VALID + [option])
    for word in ('-h', '--help', '--command', '--writeflag', '--readonly-flag', 'foo', '', '--r4x1-stop', '-', '--',
                 '--r4x1-tool'):
        # 先頭の `--r4x1-stop` は検査専用入口の隠しの引数なので、先頭には置かない。`--r4x1-tool` は隠しの引数で
        # ないので、先頭でも旧版と同じく不明な引数になる。
        if word != '--r4x1-stop':
            add(f'不明な引数[{word}]', [word, 'x'])
        add(f'不明な引数[{word}]-後ろ', VALID + [word])
        add(f'不明な引数[{word}]-2番目', ['--dry-run', word, 'x'])
    add('空の--runner', ['--runner', '', '--prompt-file', P, '--cwd', W])
    add('空の--prompt-file', ['--runner', 'codex', '--prompt-file', '', '--cwd', W])
    add('空の--cwd', ['--runner', 'codex', '--prompt-file', P, '--cwd', ''])
    add('空の--model', VALID + ['--model', ''])
    add('空の--log-file', VALID + ['--log-file', ''])
    add('繰り返し', ['--runner', 'gemini', '--runner', 'codex', '--prompt-file', '@R/none', '--prompt-file', P, '--cwd', W])
    add('順の入れ替え', ['--cwd', W, '--prompt-file', P, '--runner', 'codex'])
    add('--dry-run', VALID + ['--dry-run'])
    add('--dry-run-先頭', ['--dry-run'] + VALID)
    add('完了条件3', VALID, env={'HOME': '@R/work/home', 'PATH': '@R/fake:@OBS:/usr/bin:/bin'},
        setup=layout(dirs=('work/home',), files={'fake/codex': b'#!/bin/sh\nexit 0\n'}, modes={'fake/codex': 0o755}),
        expect=expect_condition_3)
    add('完了条件4-cwd省略', ['--runner', 'codex', '--prompt-file', P], expect=expect_condition_4)
    add('完了条件5-runnerだけ', ['--runner'], expect=expect_condition_5)
    add('引数なし', [])
    add('すべての引数', VALID + ['--model', 'gpt-5', '--log-file', 'log.md', '--probe-timeout', '9',
                                 '--run-timeout', '99', '--dry-run'])
    return cases


def group_runner_model():
    cases = []
    add = lambda label, args, **kw: cases.append(Case('runner と model', label, args, **kw))  # noqa: E731
    for runner in ('gemini', 'cursor-agent', 'claude', 'Codex', 'codex.', 'co_dex-1.2'):
        add(f'表に無い[{runner}]', ['--runner', runner, '--prompt-file', P, '--cwd', W])
    for runner in ('co dex', 'a/b', 'é', 'x;y', 'a*', '-'):
        add(f'使えない字[{runner}]', ['--runner', runner, '--prompt-file', P, '--cwd', W])
    for model in ('-m', '--x', 'a b', 'gpt 5', 'é', 'a;b', 'a*', 'a=b'):
        add(f'model[{model}]', VALID + ['--model', model])
    for model in ('gpt-5', 'a:b@c/d+e_f.g', 'x'):
        add(f'model通る[{model}]', VALID + ['--model', model])
    add('runnerとmodelの順', ['--model', '-x', '--runner', 'a b', '--prompt-file', P, '--cwd', W])
    return cases


SPACES = (b' ', b'\t', b'\n', b'\v', b'\f', b'\r')


def group_prompt():
    cases = []
    add = lambda label, args, **kw: cases.append(Case('プロンプトのファイル', label, args, **kw))  # noqa: E731
    with_prompt = lambda path: ['--runner', 'codex', '--prompt-file', path, '--cwd', W]  # noqa: E731
    add('不在', with_prompt('@R/none.md'))
    add('フォルダ', with_prompt('@R/work'))
    add('FIFO', with_prompt('@R/f.fifo'), setup=layout(fifos=('f.fifo',)))
    add('symlink', with_prompt('@R/l.md'), setup=layout(links={'l.md': '@R/p.md'}))
    add('先の無いsymlink', with_prompt('@R/l.md'), setup=layout(links={'l.md': '@R/none'}))
    for label, data in (('空', b''), ('改行だけ', b'\n'), ('空白6つ', b' \t\n\v\f\r'), ('NULだけ', b'\0'),
                        ('空白とNUL', b' \0\n\0\t'), ('NULの後にx', b'\0x'), ('0x80', b'\x80'), ('0xff', b'\xff\n'),
                        ('全角空白', '　'.encode()), ('x', b'x'), ('前後の空白', b'  x  \n'),
                        ('40000の後にx', b' ' * 40001 + b'x'), ('40000の後にNUL', b' ' * 40001 + b'\0'),
                        ('40000の後にNULとx', b' ' * 40001 + b'\0x'), ('NUL40000の後にx', b'\0' * 40001 + b'x'),
                        ('100000', b'a' * 100000), ('100001', b'a' * 100001)):
        add(f'中身[{label}]', with_prompt(P), setup=layout(prompt=data))
    for index, ch in enumerate(SPACES):
        for other in SPACES[index:]:
            add(f'空白の組[{ch!r}{other!r}]', with_prompt(P), setup=layout(prompt=ch + other))
    add('読めない', with_prompt(P), setup=layout(modes={'p.md': 0o000}))
    add('改行を含む名', with_prompt('@R/a\nb.md'), setup=layout(files={'a\nb.md': b'x\n'}))
    add('link/../p.md', with_prompt('@R/link/../p.md'),
        setup=layout(dirs=('d/sub',), links={'link': '@R/d/sub'}, files={'d/p.md': b'in-d\n'}))
    add('link/../p.md-空', with_prompt('@R/link/../p.md'),
        setup=layout(dirs=('d/sub',), links={'link': '@R/d/sub'}, files={'d/p.md': b' \n'}))
    for name in ('--help', '--version', '-r', '-v', '-', '--', '-e', '-f', '-q', '-c', '-s', '--colour=x', '-P'):
        for stdin_label, stdin in (('stdinなし', None), ('stdin空', b''), ('stdinに字', b'abc\n'),
                                   ('stdin空白', b'  \n')):
            files = {f'work/{name}': b'x\n'}
            add(f'-で始まる名[{name}]{stdin_label}', ['--runner', 'codex', '--prompt-file', name, '--cwd', '.'],
                setup=layout(files=files), stdin=stdin, needs=('grep',))
        add(f'-で始まる名[{name}]空白だけ', ['--runner', 'codex', '--prompt-file', name, '--cwd', '.'],
            setup=layout(files={f'work/{name}': b' \n'}), needs=('grep',))
    for name in ('-r', '-v'):
        add(f'POSIXLY_CORRECT[{name}]', ['--runner', 'codex', '--prompt-file', name, '--cwd', '.'],
            setup=layout(files={f'work/{name}': b'x\n', 'work/sub/t.txt': b'y\n'}), stdin=b'\n',
            env={'POSIXLY_CORRECT': '1'}, allowed_env=('POSIXLY_CORRECT',))
    add('-rで下にxが無い', ['--runner', 'codex', '--prompt-file', '-r', '--cwd', '.'],
        setup=layout(files={'work/-r': b' \n'}))
    add('grepがPATHに無い', ['--runner', 'codex', '--prompt-file', '-v', '--cwd', '.'],
        setup=layout(files={'work/-v': b'x\n'}), env={'PATH': '/nonexistent'}, stdin=b'x\n')
    return cases


def group_cwd():
    cases = []
    add = lambda label, args, **kw: cases.append(Case('--cwd', label, args, **kw))  # noqa: E731
    with_cwd = lambda value: ['--runner', 'codex', '--prompt-file', P, '--cwd', value]  # noqa: E731
    add('省略', ['--runner', 'codex', '--prompt-file', P])
    add('空', with_cwd(''))
    add('不在', with_cwd('@R/none'))
    add('ファイル', with_cwd('@R/p.md'))
    add('検索権限なし', with_cwd('@R/locked'), setup=layout(dirs=('locked',), modes={'locked': 0o600}))
    add('相対', with_cwd('sub'), setup=layout(dirs=('work/sub',)))
    add('相対の..', with_cwd('..'))
    add('link/..', with_cwd('link/..'), setup=layout(dirs=('d/sub',), links={'work/link': '@R/d/sub'}))
    add('末尾の/', with_cwd('@R/work/'))
    for name in ('-', '-P', '-L', '-e', '-@', '-x', '--', '-LP', '-Pe', '--help'):
        for env_label, env in (('HOME在り', {}), ('HOMEなし', {'HOME': None, 'XDG_STATE_HOME': '@R/state'}),
                               ('HOMEがファイル', {'HOME': '@R/p.md', 'XDG_STATE_HOME': '@R/state'}),
                               ('OLDPWD在り', {'OLDPWD': '@R/home'}), ('OLDPWDがファイル', {'OLDPWD': '@R/p.md'})):
            add(f'-で始まる値[{name}]{env_label}', ['--runner', 'codex', '--prompt-file', P, '--cwd', name],
                setup=layout(dirs=(f'work/{name}',)), env=env)
    add('CDPATH', with_cwd('sub'), setup=layout(dirs=('work/sub', 'cdp/sub')), env={'CDPATH': '@R/cdp'},
        allowed_env=('CDPATH',))
    add('CDPATHと-', ['--runner', 'codex', '--prompt-file', P, '--cwd', '-'], setup=layout(dirs=('work/-', 'cdp/-')),
        env={'CDPATH': '@R/cdp', 'OLDPWD': '@R/home'}, allowed_env=('CDPATH',))
    return cases


def group_timeouts():
    cases = []
    add = lambda label, args, **kw: cases.append(Case('時間', label, args, **kw))  # noqa: E731
    for name in ('--probe-timeout', '--run-timeout'):
        for value in ('abc', '0', '00', '007', '1', '99999999999999999999', '9223372036854775807',
                      '9223372036854775808', '-1', '', ' 1', '1 ', '+1', '1.5', '٣'):
            add(f'{name}[{value}]', VALID + [name, value])
    add('両方0', VALID + ['--probe-timeout', '0', '--run-timeout', '0'])
    add('両方大きい', VALID + ['--probe-timeout', '99999999999999999999', '--run-timeout', '99999999999999999999'])
    add('両方大きい-ja', VALID + ['--probe-timeout', '99999999999999999999'], env={'LANG': 'ja_JP.UTF-8'},
        mac=False)
    return cases


VERSION_TEXT = None


def group_log_file():
    cases = []
    add = lambda label, args, **kw: cases.append(Case('--log-file の正規化', label, args, **kw))  # noqa: E731
    with_log = lambda value: VALID + ['--log-file', value]  # noqa: E731
    for value in ('.', '..', '/', '//', 'a//b.md', 'dir/', 'plain.md', './x.md', '../x.md', 'a/link/../x.md',
                  '@R/abs/x.md', '@R/work/./y.md', 'x\n', 'a\nb/c.md', '-', 'x/-', '*', 'é.md'):
        add(f'値[{value}]', with_log(value), setup=layout(dirs=('work/a/real',), links={'work/a/link': '@R/work/a/real'}))
    for value in ('--help', '--he', '--h', '--version', '--vers', '--v', '--help=x', '--version=', '-x', '-z', '-s',
                  '--', '--zero', '--zz', '--=x', '-zf', '-é', '--help=', '-'):
        add(f'-で始まる値[{value}]', with_log(value))
        add(f'-で始まる値[{value}]-ja', with_log(value), env={'LANG': 'ja_JP.UTF-8'}, mac=False)
    for label, setup in (('通常ファイル', layout(files={'work/plain.md': b'x'})),
                         ('フォルダ', layout(dirs=('work/plain.md',))),
                         ('symlink', layout(links={'work/plain.md': '@R/p.md'})),
                         ('先の無いsymlink', layout(links={'work/plain.md': '@R/none'})),
                         ('FIFO', layout(fifos=('work/plain.md',)))):
        add(f'既存[{label}]', with_log('plain.md'), setup=setup)
        add(f'既存[{label}]-正規化', with_log('./sub/../plain.md'), setup=setup)
    add('既存-絶対', with_log('@R/work/plain.md'), setup=layout(files={'work/plain.md': b'x'}))
    add('既存-..', with_log('../p.md'))
    add('既存-改行', with_log('a\nb'), setup=layout(files={'work/a\nb': b'x'}))
    add('既存--', with_log('-'), setup=layout(files={'work/-': b'x'}))
    for value, english in (('--version', True), ('--vers', True)):
        add(f'既存[{value}]-版の文', with_log(value), setup=version_layout('C'))
        add(f'既存[{value}]-版の文-ja', with_log(value), setup=version_layout('ja'),
            env={'LANG': 'ja_JP.UTF-8'}, mac=False)
        add(f'既存[{value}]-版の文-ascii', with_log(value), setup=version_layout('ascii'),
            env={'LANG': 'C', 'LC_MESSAGES': 'ja_JP.UTF-8'}, mac=False)
    return cases


def coreutils_version(tool, mode):
    env = {'PATH': '/usr/bin:/bin'}
    if mode == 'ja':
        env['LANG'] = 'ja_JP.UTF-8'
    elif mode == 'ascii':
        env.update(LANG='C', LC_MESSAGES='ja_JP.UTF-8')
    else:
        env['LC_ALL'] = 'C'
    return subprocess.run([tool, '--version'], capture_output=True, env=env).stdout.rstrip(b'\n')


def version_layout(mode):
    """`--log-file --version` の正規化したパスに、エントリを先に置く(版の文が見えるようにする)。"""
    def setup(root):
        default_layout(root)
        directory = coreutils_version('dirname', mode)
        name = coreutils_version('basename', mode)
        logdir = lex_norm(str(root / 'work').encode() + b'/' + directory)
        norm = logdir.rstrip(b'/') + b'/' + name
        path = Path(os.fsdecode(norm))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x')
    return setup


def lex_norm(value):
    out = []
    for segment in value.lstrip(b'/').split(b'/') if value.startswith(b'/') else value.split(b'/'):
        if segment in (b'', b'.'):
            continue
        if segment == b'..':
            if out:
                out.pop()
            continue
        out.append(segment)
    return b'/' + b'/'.join(out) if out else b'/'


def group_candidates():
    cases = []
    add = lambda label, env, **kw: cases.append(Case('候補の解決の始め', label, VALID, env=env, **kw))  # noqa: E731
    add('XDGだけ', {'XDG_STATE_HOME': '@R/state', 'HOME': None})
    add('XDGとHOME', {'XDG_STATE_HOME': '@R/state'})
    add('XDG空', {'XDG_STATE_HOME': ''})
    add('XDG相対', {'XDG_STATE_HOME': 'rel'})
    add('XDGが/.local/state', {'XDG_STATE_HOME': '/.local/state'})
    add('XDGが/.cache', {'XDG_STATE_HOME': '/.cache', 'HOME': None})
    add('XDGがsymlink/..', {'XDG_STATE_HOME': '@R/link/..'}, setup=layout(dirs=('d/s',), links={'link': '@R/d/s'}))
    add('XDGが-x', {'XDG_STATE_HOME': '-x'})
    add('HOMEなし', {'HOME': None})
    add('HOME空', {'HOME': ''})
    add('HOMEが/', {'HOME': '/'})
    add('HOME相対', {'HOME': 'rel'})
    add('HOMEが-x', {'HOME': '-x'})
    add('HOMEとXDGなし', {'HOME': None, 'XDG_STATE_HOME': None})
    add('HOME空とXDG空', {'HOME': '', 'XDG_STATE_HOME': ''})
    add('HOMEが/とXDGが/.cache', {'HOME': '/', 'XDG_STATE_HOME': '/.cache'})
    for tmpdir in ('@R/tmp', '', 'rel', '-', '@R/none', '@R/p.md'):
        add(f'TMPDIR[{tmpdir}]', {'TMPDIR': tmpdir}, setup=layout(dirs=('tmp',)))
    add('候補なし-cwd解けない', {'HOME': None}, )
    cases.append(Case('候補の解決の始め', 'cwd解けない', ['--runner', 'codex', '--prompt-file', P, '--cwd', '-'],
                      setup=layout(dirs=('work/-',))))
    cases.append(Case('候補の解決の始め', 'cwd解けない-HOMEなし', ['--runner', 'codex', '--prompt-file', P, '--cwd', '-'],
                      setup=layout(dirs=('work/-',)), env={'HOME': None}))
    return cases


def group_signals():
    cases = []
    add = lambda label, args, **kw: cases.append(Case('中止の信号', label, args, **kw))  # noqa: E731
    add('完了条件6', VALID, env={'HOME': '@R/work/home', 'PATH': '@R/fake:@OBS:/usr/bin:/bin'}, stop=('boundary', 'TERM'),
        setup=layout(dirs=('work/home',), files={'fake/codex': b'#!/bin/sh\nexit 0\n'}, modes={'fake/codex': 0o755}),
        expect=expect_condition_6)
    add('完了条件7', VALID, env={'HOME': '@R/work/home', 'PATH': '@R/fake:@OBS:/usr/bin:/bin'}, stop=('grep', 'TERM'),
        setup=layout(dirs=('work/home',), files={'fake/codex': b'#!/bin/sh\nexit 0\n'}, modes={'fake/codex': 0o755}),
        expect=expect_condition_7)
    for name in SIGNALS:
        add(f'grep[{name}]', VALID, stop=('grep', name))
        add(f'dirname[{name}]', VALID + ['--log-file', 'x.md'], stop=('dirname', name))
        add(f'basename[{name}]', VALID + ['--log-file', 'x.md'], stop=('basename', name))
        add(f'boundary[{name}]', VALID, stop=('boundary', name))
        add(f'dirname失敗[{name}]', VALID + ['--log-file', '-x'], stop=('dirname', name))
        add(f'grep起動[{name}]', ['--runner', 'codex', '--prompt-file', '-v', '--cwd', '.'],
            setup=layout(files={'work/-v': b'x\n'}), stdin=b'x\n', stop=('grep', name))
        add(f'grepとLC_ALL[{name}]', VALID, env={'LC_ALL': 'xx_YY.UTF-8'}, stop=('grep', name))
        for ignored in SIGNALS:
            add(f'boundary[{name}]-無視{ignored}', VALID, stop=('boundary', name), ignore=(SIGNALS[ignored],))
        for stream in ('devfull', 'closed'):
            add(f'boundary[{name}]-stderr{stream}', VALID, stop=('boundary', name), stderr=stream,
                mac=stream != 'devfull')
            add(f'boundary[{name}]-stderr{stream}-PIPE無視', VALID, stop=('boundary', name), stderr=stream,
                ignore=(signal.SIGPIPE,))
        add(f'dirname-eucJP[{name}]', VALID + ['--log-file', '-x'], stop=('dirname', name), eucjp=True,
            env={'LOCPATH': '@LOC', 'LANG': 'ja_JP.eucJP'}, mac=False)
    for ignored in SIGNALS:
        add(f'無視{ignored}-境界まで', VALID, ignore=(SIGNALS[ignored],))
        add(f'無視{ignored}-usage', ['--runner'], ignore=(SIGNALS[ignored],))
    add('全部無視-境界まで', VALID, ignore=tuple(SIGNALS.values()))
    add('CHLD止め-grep起動', ['--runner', 'codex', '--prompt-file', '-v', '--cwd', '.'],
        setup=layout(files={'work/-v': b'x\n'}), stdin=b'x\n', block=(signal.SIGCHLD, signal.SIGUSR1))
    add('CHLD無視-grep起動', ['--runner', 'codex', '--prompt-file', '-v', '--cwd', '.'],
        setup=layout(files={'work/-v': b'x\n'}), stdin=b'x\n', ignore=(signal.SIGCHLD,), needs=('grep',))
    add('CHLD無視-境界まで', VALID, ignore=(signal.SIGCHLD,))
    add('CHLD無視-dirname-eucJP', VALID + ['--log-file', '-x'], ignore=(signal.SIGCHLD,), eucjp=True,
        env={'LOCPATH': '@LOC', 'LANG': 'ja_JP.eucJP'}, mac=False, needs=('dirname',))
    add('CHLD無視-dirname-eucJP-通る', VALID + ['--log-file', 'a/b.md'], ignore=(signal.SIGCHLD,), eucjp=True,
        env={'LOCPATH': '@LOC', 'LANG': 'ja_JP.eucJP'}, mac=False, needs=('dirname', 'basename'))
    add('PIPE無視-grep起動', ['--runner', 'codex', '--prompt-file', '-v', '--cwd', '.'],
        setup=layout(files={'work/-v': b'x\n'}), stdin=b'x\n', ignore=(signal.SIGPIPE, signal.SIGQUIT))
    return cases


def group_faults():
    cases = []
    add = lambda label, args, **kw: cases.append(Case('書き込みの故障', label, args, **kw))  # noqa: E731
    usage_args = ['--runner']
    fail_args = ['--runner', 'codex', '--prompt-file', P]
    int_args = VALID + ['--probe-timeout', '99999999999999999999']
    for label, args in (('usage', usage_args), ('fail_usage', fail_args), ('境界', VALID), ('[の文', int_args),
                        ('dirname', VALID + ['--log-file', '-x'])):
        for stream in ('stdout', 'stderr'):
            add(f'{label}-{stream}devfull', args, **{stream: 'devfull'}, mac=False)
            add(f'{label}-{stream}closed', args, **{stream: 'closed'})
            add(f'{label}-{stream}closed-PIPE無視', args, **{stream: 'closed'}, ignore=(signal.SIGPIPE,))
    usage_len = len(USAGE_TXT.read_bytes())
    for label, args, sizes in (('usage', usage_args, (0, 10, 37, 38, 39, 100, 4095, 4096, 4097, 4134, 8192,
                                                       usage_len, usage_len + 37, usage_len + 38, usage_len + 100)),
                               ('fail_usage', fail_args, (0, 5, 20, 1000)),
                               ('境界', VALID, (0, 10, 50, 1000)),
                               ('[の文', int_args, (0, 30, 60, 120, 150, 200, 1000)),
                               ('dirname', VALID + ['--log-file', '-x'], (0, 10, 31, 40, 80, 1000))):
        for size in sizes:
            add(f'{label}-FSIZE{size}', args, fsize=size, core0=True)
            add(f'{label}-FSIZE{size}-XFSZ無視', args, fsize=size, core0=True, ignore=(signal.SIGXFSZ,))
    for name in ('--help', '--version'):
        for size in (0, 100, 1000):
            add(f'grep{name}-stdoutFSIZE{size}', ['--runner', 'codex', '--prompt-file', name, '--cwd', '.'],
                setup=layout(files={f'work/{name}': b'x\n'}), fsize=size, core0=True, stderr='devnull')
            add(f'grep{name}-stdoutFSIZE{size}-XFSZ無視', ['--runner', 'codex', '--prompt-file', name, '--cwd', '.'],
                setup=layout(files={f'work/{name}': b'x\n'}), fsize=size, core0=True, ignore=(signal.SIGXFSZ,),
                stderr='devnull')
        for language, env in (('C', None), ('ja', {'LANG': 'ja_JP.UTF-8'})):
            for ignored in ((), (signal.SIGXFSZ,)):
                # 知らせの行が出るのは `--help`(1000 バイトを超える)で SIGXFSZ が既定の 2 件だけ。`--version` は
                # 1000 バイトに届かず、SIGXFSZ を無視すると grep は書き込みの誤りの終了値 2 で終わる(死なない形の確かめ)。
                # 死ぬ 2 件は RLIMIT_CORE を 1 にして、`(core dumped)` の有無を環境(core_pipe_limit など)に左右させない。
                dies = name == '--help' and not ignored
                add(f'grep{name}-知らせ-{language}{"-XFSZ無視" if ignored else ""}',
                    ['--runner', 'codex', '--prompt-file', name, '--cwd', '.'],
                    setup=layout(files={f'work/{name}': b'x\n'}), fsize=1000, core0=not dies, core1=dies,
                    ignore=ignored, env=env, needs=('grep',), expect=expect_grep_notice if dies else None)
        add(f'grep{name}-stdoutclosed', ['--runner', 'codex', '--prompt-file', name, '--cwd', '.'],
            setup=layout(files={f'work/{name}': b'x\n'}), stdout='closed')
        add(f'grep{name}-stdoutclosed-PIPE無視', ['--runner', 'codex', '--prompt-file', name, '--cwd', '.'],
            setup=layout(files={f'work/{name}': b'x\n'}), stdout='closed', ignore=(signal.SIGPIPE,))
        add(f'grep{name}-stdoutdevfull', ['--runner', 'codex', '--prompt-file', name, '--cwd', '.'],
            setup=layout(files={f'work/{name}': b'x\n'}), stdout='devfull', mac=False)
        add(f'grep{name}-stdoutFSIZE-ja', ['--runner', 'codex', '--prompt-file', name, '--cwd', '.'],
            setup=layout(files={f'work/{name}': b'x\n'}), fsize=100, core0=True, stderr='devnull',
            env={'LANG': 'ja_JP.UTF-8'}, mac=False)
    return cases


def group_startup():
    cases = []
    add = lambda label, args, **kw: cases.append(Case('bash の起動', label, args, **kw))  # noqa: E731
    for label, pwd in (('同じ実体のsymlink', '@R/wlink'), ('/./を含む', '@R/./work'), ('違うフォルダ', '@R/home'),
                       ('相対', 'work'), ('末尾の/', '@R/work/'), ('//', '/@R/work')):
        add(f'PWD[{label}]', VALID, env={'PWD': pwd}, setup=layout(links={'wlink': '@R/work'}))
        add(f'PWD[{label}]-grep起動', ['--runner', 'codex', '--prompt-file', '-v', '--cwd', '.'], env={'PWD': pwd},
            setup=layout(links={'wlink': '@R/work'}, files={'work/-v': b'x\n'}), stdin=b'x\n')
    add('PWDなし-grep起動', ['--runner', 'codex', '--prompt-file', '-v', '--cwd', '.'],
        setup=layout(files={'work/-v': b'x\n'}), stdin=b'x\n')
    for label, old in (('フォルダ', '@R/home'), ('フォルダでない', '@R/p.md'), ('相対', 'home'), ('空', '')):
        add(f'OLDPWD[{label}]-grep起動', ['--runner', 'codex', '--prompt-file', '-v', '--cwd', '.'],
            env={'OLDPWD': old}, setup=layout(files={'work/-v': b'x\n'}), stdin=b'x\n')
        add(f'OLDPWD[{label}]-cwdが-', ['--runner', 'codex', '--prompt-file', P, '--cwd', '-'],
            env={'OLDPWD': old}, setup=layout(dirs=('work/-',)))
    for label, extra in (('PWDなし', {}), ('PWD在り', {'PWD': '@R/gone'})):
        add(f'cwdが消えた[{label}]', ['--runner', 'codex', '--prompt-file', P, '--cwd', W], cwd='GONE', env=extra)
        add(f'cwdが消えた[{label}]-相対', ['--runner', 'codex', '--prompt-file', 'p.md', '--cwd', W], cwd='GONE',
            env=extra)
        add(f'cwdが消えた[{label}]-usage', ['--runner'], cwd='GONE', env=extra)
        add(f'cwdが消えた[{label}]-LC_ALL', ['--runner', 'codex', '--prompt-file', P, '--cwd', W], cwd='GONE',
            env=dict(extra, LC_ALL='xx_YY'))
        add(f'cwdが消えた[{label}]-ja', ['--runner', 'codex', '--prompt-file', P, '--cwd', W], cwd='GONE',
            env=dict(extra, LANG='ja_JP.UTF-8'), mac=False)
    for level in ('0', '1', '998', '999', '1000', '-5', 'x', '  7  ', '99999999999'):
        add(f'SHLVL[{level}]', ['--runner'], env={'SHLVL': level})
        add(f'SHLVL[{level}]-grep起動', ['--runner', 'codex', '--prompt-file', '-v', '--cwd', '.'],
            env={'SHLVL': level}, setup=layout(files={'work/-v': b'x\n'}), stdin=b'x\n')
    for label, level in (('7LF', '7\n'), ('999LF', '999\n'), ('7CR', '7\r'), ('7TAB', '7\t'), ('LF7', '\n7')):
        add(f'SHLVL[{label}]', ['--runner'], env={'SHLVL': level})
        add(f'SHLVL[{label}]-grep起動', ['--runner', 'codex', '--prompt-file', '-v', '--cwd', '.'],
            env={'SHLVL': level}, setup=layout(files={'work/-v': b'x\n'}), stdin=b'x\n', needs=('grep',))
    for value in ('x', '99', '-1', '0', '1', '2'):
        add(f'BASH_XTRACEFD[{value}]', ['--runner'], env={'BASH_XTRACEFD': value})
    for value in ('30', '53', '4.0', 'x', '5.2', '51'):
        add(f'BASH_COMPAT[{value}]', ['--runner'], env={'BASH_COMPAT': value}, allowed_env=('BASH_COMPAT',))
    for name, value in (('LC_ALL', 'xx_YY.UTF-8'), ('LC_ALL', 'C'), ('LANG', 'xx_YY.UTF-8'),
                        ('LC_CTYPE', 'xx_YY'), ('LC_MESSAGES', 'xx_YY'), ('LC_ALL', 'ja_JP'),
                        ('LC_ALL', 'a/b'), ('LC_ALL', '../x')):
        add(f'入っていない{name}[{value}]-usage', ['--runner'], env={name: value})
        add(f'入っていない{name}[{value}]-grep', VALID, env={name: value})
        add(f'入っていない{name}[{value}]-空白だけ', VALID, env={name: value}, setup=layout(prompt=b' \n'))
    add('POSIXLY_CORRECT', VALID, env={'POSIXLY_CORRECT': 'y'}, allowed_env=('POSIXLY_CORRECT',))
    add('POSIXLY_CORRECT-PWD', ['--runner', 'codex', '--prompt-file', '-v', '--cwd', '.'],
        env={'POSIXLY_CORRECT': 'y', 'PWD': '@R/./work'}, setup=layout(files={'work/-v': b'x\n'}), stdin=b'x\n',
        allowed_env=('POSIXLY_CORRECT',))
    add('同じ名前の二重', ['--runner', 'codex', '--prompt-file', '-v', '--cwd', '.'],
        raw_env=[b'PATH=@OBS:/usr/bin:/bin', b'HOME=@R/home', b'A=1', b'A=2', b'=x', b'noeq', b'a-b=3',
                 b'HOME=@R/work'], setup=layout(files={'work/-v': b'x\n'}), stdin=b'x\n')
    add('旧スクリプトの変数', ['--runner', 'codex', '--prompt-file', '-v', '--cwd', '.', '--model', 'm'],
        raw_env=[b'PATH=@OBS:/usr/bin:/bin', b'HOME=@R/home'] + [
            b(name) + b'=v-' + b(name) for name in SCRIPT_NAMES],
        setup=layout(files={'work/-v': b'x\n'}), stdin=b'x\n')
    add('旧スクリプトの変数-eucJP', VALID + ['--log-file', '-x'],
        raw_env=[b'PATH=@OBS:/usr/bin:/bin', b'HOME=@R/home', b'LOCPATH=@LOC', b'LANG=ja_JP.eucJP'] + [
            b(name) + b'=v-' + b(name) for name in SCRIPT_NAMES], eucjp=True, mac=False)
    add('bashの特別な変数', ['--runner', 'codex', '--prompt-file', '-v', '--cwd', '.'],
        raw_env=[b'PATH=@OBS:/usr/bin:/bin', b'HOME=@R/home'] + [
            b(name) + b'=v-' + b(name) for name in BASH_SPECIAL], setup=layout(files={'work/-v': b'x\n'}),
        stdin=b'x\n')
    return cases


# 旧版の 1〜391 行で代入する変数(352・390・391 行の時点で子へ渡す値を確かめる)。
SCRIPT_NAMES = ('RUNNER', 'MODEL', 'PROMPT_FILE', 'CWD', 'LOG_FILE', 'PROBE_TIMEOUT', 'RUN_TIMEOUT', 'HELP_TIMEOUT',
                'KILL_GRACE', 'MAX_PROMPT_BYTES', 'DRY_RUN', 'CMD', 'WRITE_TOKENS', 'PROBE_TOKENS', 'STATE_DIR',
                'CHILD_PID', 'RAW_OUT', 'PROBE_OUT', 'PROBE_ERR', 'MODEL_INFO_LOGGED', 'LOG_FD_OPEN',
                'LOG_SWAP_NOTED', 'LOG_EF_OK', 'LOG_SWAP_PY', 'DISP', 'PR_VAL', 'PR_SRC', 'PHYS_PWD', 'LOG_DIR',
                'LOG_NAME', 'LOG_NORM')
# bash が子へ渡す値を決め直す名前・渡さない名前(比較の入力に使ってよいもの)。
BASH_SPECIAL = ('IFS', 'OPTIND', 'OPTERR', 'LINENO', 'BASH', 'BASH_VERSION', 'PPID', 'BASHPID', 'RANDOM', 'SRANDOM',
                'BASH_SUBSHELL', 'BASH_COMMAND', 'BASH_REMATCH', 'BASH_VERSINFO', 'HISTCMD', 'COMP_WORDBREAKS',
                'PS1', 'PS2', 'PS4', 'UID', 'EUID', 'GROUPS', 'SECONDS', 'TERM', 'TZ', '_')


def group_locale():
    cases = []
    add = lambda label, args, **kw: cases.append(Case('地域設定', label, args, mac=False, **kw))  # noqa: E731
    settings = (
        ('C', {'LC_ALL': 'C'}), ('C.UTF-8', {'LC_ALL': 'C.UTF-8'}), ('ja_JP.UTF-8', {'LC_ALL': 'ja_JP.UTF-8'}),
        ('LANG=ja+MESSAGES=C', {'LANG': 'ja_JP.UTF-8', 'LC_MESSAGES': 'C'}),
        ('LANG=C+MESSAGES=ja', {'LANG': 'C', 'LC_MESSAGES': 'ja_JP.UTF-8'}),
        ('LANGUAGE=fr:ja', {'LANG': 'ja_JP.UTF-8', 'LANGUAGE': 'fr:ja'}),
        ('LANGUAGE=C:ja', {'LANG': 'ja_JP.UTF-8', 'LANGUAGE': 'C:ja'}),
        ('LANGUAGE=ja+C.UTF-8', {'LC_ALL': 'C.UTF-8', 'LANGUAGE': 'ja'}),
        ('入っていないLC_ALL+LANGUAGE=ja', {'LC_ALL': 'xx_YY.UTF-8', 'LANGUAGE': 'ja'}),
        ('LANG=ja_JP.utf8', {'LANG': 'ja_JP.utf8'}), ('LANG=ja', {'LANG': 'ja'}), ('LANG=japanese', {'LANG': 'japanese'}),
        ('LOCPATH-ja', {'LOCPATH': '@LOC', 'LANG': 'ja_JP.UTF-8'}),
        ('LOCPATH-C', {'LOCPATH': '@LOC', 'LANG': 'C.UTF-8'}),
        ('LC_CTYPE=C+ja', {'LANG': 'ja_JP.UTF-8', 'LC_CTYPE': 'C'}),
        ('LC_CTYPE=ja+C', {'LANG': 'C', 'LC_CTYPE': 'ja_JP.UTF-8', 'LC_MESSAGES': 'ja_JP.UTF-8'}),
        ('LANGUAGE=en:ja', {'LANG': 'ja_JP.UTF-8', 'LANGUAGE': 'en:ja'}),
        ('LANGUAGE=ja_JP', {'LANG': 'ja_JP.UTF-8', 'LANGUAGE': 'ja_JP'}),
    )
    for label, env in settings:
        for value in ('-x', '-z', '--help=x', '--zz', '-s', '--=x', '-é'):
            add(f'{label}[{value}]', VALID + ['--log-file', value], env=env)
        add(f'{label}[既存]', VALID + ['--log-file', 'plain.md'], env=env, setup=layout(files={'work/plain.md': b'x'}))
    for value in ('-x', '-z', '--help=x', '--zz', '--help', '--version', 'a/b.md', '-'):
        add(f'eucJP[{value}]', VALID + ['--log-file', value], env={'LOCPATH': '@LOC', 'LANG': 'ja_JP.eucJP'},
            eucjp=True)
        add(f'eucJP-LC_ALL[{value}]', VALID + ['--log-file', value],
            env={'LOCPATH': '@LOC', 'LC_ALL': 'ja_JP.eucJP'}, eucjp=True)
    add('eucJP[既存]', VALID + ['--log-file', 'plain.md'], env={'LOCPATH': '@LOC', 'LANG': 'ja_JP.eucJP'}, eucjp=True,
        setup=layout(files={'work/plain.md': b'x'}))
    add('eucJP-bashの行', VALID + ['--probe-timeout', '99999999999999999999'],
        env={'LOCPATH': '@LOC', 'LANG': 'ja_JP.eucJP'})
    add('eucJP-cwdが消えた', ['--runner', 'codex', '--prompt-file', P, '--cwd', W], cwd='GONE',
        env={'LOCPATH': '@LOC', 'LANG': 'ja_JP.eucJP'})
    return cases


def group_unexpected():
    cases = []
    add = lambda label, args, **kw: cases.append(Case('想定外の失敗', label, args, **kw))  # noqa: E731
    add('387のpwd', ['--runner', 'codex', '--prompt-file', P, '--cwd', W], cwd='GONE')
    for value in ('-x', '-z', '--', '--help=x', '-zz9'):
        add(f'390のdirname[{value}]', VALID + ['--log-file', value])
    add('390のdirname-stderr閉', VALID + ['--log-file', '-x'], stderr='closed')
    add('390のdirname-stderr閉-PIPE無視', VALID + ['--log-file', '-x'], stderr='closed', ignore=(signal.SIGPIPE,))
    return cases


GROUPS = (group_arguments, group_runner_model, group_prompt, group_cwd, group_timeouts, group_log_file,
          group_candidates, group_signals, group_faults, group_startup, group_locale, group_unexpected)


def static_mac_reason(case):
    """Mac で流さない理由(ケースの形から決まるもの)。無ければ None。"""
    env = case.env or {}
    values = ' '.join(str(v) for v in env.values() if v is not None) + ' '.join(map(str, case.raw_env or ()))
    if case.group == '地域設定' or 'ja_JP' in values or 'ja' == env.get('LANGUAGE'):
        return '日本語の包みと GNU の地域設定(Mac で用意できない)'
    if case.eucjp or '@LOC' in values:
        return 'localedef で作る地域設定(Mac で用意できない)'
    if 'devfull' in (case.stdout, case.stderr):
        return '/dev/full が無い'
    if case.fsize is not None:
        return '書き込みの途中で切れる位置は glibc の stdio の溜め方に依る(基準の版)'
    if case.cwd == 'GONE':
        return '起動時に cwd が消えた起動の getcwd の文は glibc に依る(基準の版)'
    if case.stop and case.stop[0] != 'boundary':
        return '罠の前の信号の時点は基準の bash で測った(基準の版)'
    if case.raw_env is not None:
        return '子の環境の表は基準の bash 5.2.21 で測った(基準の版)'
    if case.group == 'プロンプトのファイル' and any(word in case.label for word in ('NUL', '0x', '全角', '40000')):
        return 'GNU grep の NUL と 8 ビットの字の扱いに依る(Mac は BSD の grep)'
    if not case.mac:
        return 'GNU の道具の文に依る(Mac は BSD の道具)'
    return None


def dynamic_mac_reason(old):
    """Linux の旧版の結果から決まる、Mac で流さない理由(基準の版の文が出るケース)。無ければ None。"""
    for line in normalize_old(old.stderr).split(b'\n'):
        if line.startswith((FIXED_NAME + b': ', b'bash: ', b'shell-init: ', b'job-working-directory: ', b'pwd: ')):
            return 'bash 自身の行が出る(文は基準の bash 5.2.21 で決まる)'
        if line.startswith((b'dirname: ', b'basename: ')):
            return 'GNU の道具の文が出る(Mac は BSD の道具)'
    if old.records.get('dirname') and last_value_any(old):
        return 'GNU の道具の文が出る(Mac は BSD の道具)'
    return None


def last_value_any(old):
    """旧版の dirname の値が `-` で始まる(GNU の選択肢の読み方に依る)。"""
    for record in old.records.get('dirname', []):
        argv = record.get('argv', [])
        if len(argv) > 1 and argv[1].startswith(b'-'):
            return True
    return False


def load_mac_list():
    if not MAC_UNPERFORMED.is_file():
        return {}
    return json.loads(MAC_UNPERFORMED.read_text(encoding='utf-8'))['cases']


def all_cases(scope):
    cases = []
    skipped = load_mac_list() if scope == 'mac' else {}
    for make in GROUPS:
        for case in make():
            if scope == 'mac' and (static_mac_reason(case) or case.name in skipped):
                continue
            cases.append(case)
    names = [case.name for case in cases]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise AssertionError(f'ケースの名前が重なる: {duplicates[:5]}')
    return cases


# ---- 道具の固定期待(390・391 行の dirname・basename。基準の容器の本物の道具で測り、Rust の単体テストが照らす) ----

TOOL_VALUES = ('a/b', '/', '//', 'a/', '-', '--', '-x', '-z', '-zf', '--help', '--he', '--h', '--version', '--vers',
               '--v', '--help=x', '--version=', '--zero', '--zz', '--=x', '-é', 'x\n', '-s', '-a', '--suffix',
               '--multiple', '--mu')
# 道具の設定ごとの、Rust の再現の入力(単体テストが ToolLocale::fixed へ渡す値。基準の容器の glibc の
# setlocale と gettext の選び方から決まる値)。codeset が UTF-8 でも ASCII でもない設定では、Rust は道具を起動する。
TOOL_SETTINGS = {
    'C': ({'LC_ALL': 'C'}, 'C', [], 'ANSI_X3.4-1968'),
    'C.UTF-8': ({'LC_ALL': 'C.UTF-8'}, 'C.UTF-8', [], 'UTF-8'),
    'ja': ({'LANG': 'ja_JP.UTF-8'}, 'ja_JP.UTF-8', ['ja_JP.UTF-8'], 'UTF-8'),
    'ascii-ja': ({'LANG': 'C', 'LC_MESSAGES': 'ja_JP.UTF-8'}, 'ja_JP.UTF-8', ['ja_JP.UTF-8'], 'ANSI_X3.4-1968'),
    'fr:ja': ({'LANG': 'ja_JP.UTF-8', 'LANGUAGE': 'fr:ja'}, 'ja_JP.UTF-8', ['fr', 'ja'], 'UTF-8'),
    'ja+C.UTF-8': ({'LC_ALL': 'C.UTF-8', 'LANGUAGE': 'ja'}, 'C.UTF-8', [], 'UTF-8'),
    'bad+ja': ({'LC_ALL': 'xx_YY.UTF-8', 'LANGUAGE': 'ja'}, 'C', [], 'ANSI_X3.4-1968'),
    'ja_JP.utf8': ({'LANG': 'ja_JP.utf8'}, 'ja_JP.utf8', ['ja_JP.utf8'], 'UTF-8'),
    # LOCPATH があると locale-archive を読まない。基準の容器の ja_JP.utf8 は保管庫にだけあるので C になる。
    'LOCPATH-ja': ({'LOCPATH': '@LOC', 'LANG': 'ja_JP.UTF-8'}, 'C', [], 'ANSI_X3.4-1968'),
    'en:ja': ({'LANG': 'ja_JP.UTF-8', 'LANGUAGE': 'en:ja'}, 'ja_JP.UTF-8', ['en', 'ja'], 'UTF-8'),
    'eucJP': ({'LOCPATH': '@LOC', 'LANG': 'ja_JP.eucJP'}, 'ja_JP.eucJP', ['ja_JP.eucJP'], 'EUC-JP'),
}
TOOL_FIXTURE = FIXTURES / 'tool_outputs.json'
TOOLS_RS = ROOT / 'native/crates/cli/src/commands/implement_agent/tools.rs'


def rust_byte_literals(path):
    """Rust のソースの `b"…"` の字面(`\\n`・`\\"`・`\\\\`・`\\'` を戻したもの)。訳を引く msgid の候補。"""
    text = Path(path).read_text(encoding='utf-8')
    out = set()
    for found in re.finditer(r'b"((?:[^"\\]|\\.)*)"', text):
        raw = found.group(1)
        value = raw.replace('\\\\', '\x00').replace('\\n', '\n').replace('\\"', '"').replace("\\'", "'")
        out.add(value.replace('\x00', '\\').encode('utf-8'))
    return out


def measure_tools(root):
    """本物の dirname・basename を、設定と値ごとに流した結果(固定期待の中身)。"""
    cases = []
    for tool in ('dirname', 'basename'):
        for label, (env, _, _, _) in TOOL_SETTINGS.items():
            entries = {'PATH': '/usr/bin:/bin'}
            entries.update({k: expand(v, root).decode() for k, v in env.items()})
            for value in TOOL_VALUES:
                real = subprocess.run([tool, value], executable=TOOLS.real[tool], capture_output=True, env=entries,
                                      timeout=60)
                cases.append({'tool': tool, 'setting': label, 'value': value.encode().hex(),
                              'stdout': real.stdout.hex(), 'stderr': real.stderr.hex(), 'status': real.returncode})
    return cases


def write_tool_fixture(root, path):
    """基準の容器で測った道具の結果と、Rust が引く msgid の日本語の訳(coreutils.mo・libc.mo)を固定期待に書く。"""
    wanted = rust_byte_literals(TOOLS_RS)
    catalogs = {}
    for domain in ('coreutils', 'libc'):
        table = read_mo(find_mo(domain))
        catalogs[domain] = sorted([k.hex(), v.hex()] for k, v in table.items() if k in wanted)
    data = {
        'about': '390・391 行の dirname・basename の固定期待(基準の容器の GNU coreutils 9.4・glibc 2.39・language-pack-ja '
                 'で測った)。Rust の単体テストが、catalogs から作った翻訳ファイルと settings の値で再現を照らす。'
                 '範囲 full の比較器は、本物の道具をもう一度流して cases が今も出ることを確かめる',
        'catalogs': catalogs,
        'settings': {label: {'env': env, 'messages': messages, 'languages': languages, 'codeset': codeset}
                     for label, (env, messages, languages, codeset) in TOOL_SETTINGS.items()},
        'cases': measure_tools(root),
    }
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    return len(data['cases'])


def run_tool_stage(root):
    """(照らした件数, 問題の一覧)。本物の道具をもう一度流し、固定期待の結果が今も出ることを確かめる。"""
    fixed = json.loads(TOOL_FIXTURE.read_text(encoding='utf-8'))
    problems = []
    settings = {label: {'env': env, 'messages': messages, 'languages': languages, 'codeset': codeset}
                for label, (env, messages, languages, codeset) in TOOL_SETTINGS.items()}
    if fixed['settings'] != settings:
        problems.append('道具の固定期待: 設定の表が比較器の表と違う(固定期待を作り直す)')
    wanted = rust_byte_literals(TOOLS_RS)
    for domain in ('coreutils', 'libc'):
        table = read_mo(find_mo(domain))
        now = sorted([k.hex(), v.hex()] for k, v in table.items() if k in wanted)
        if fixed['catalogs'][domain] != now:
            problems.append(f'道具の固定期待: {domain}.mo の訳が今の翻訳ファイルと違う(固定期待を作り直す)')
    measured = measure_tools(root)
    if measured != fixed['cases']:
        changed = [f"{m['tool']}/{m['setting']}[{bytes.fromhex(m['value'])!r}]"
                   for m, f in zip(measured, fixed['cases']) if m != f]
        problems.append(f'道具の固定期待: 本物の道具の結果が固定期待と違う({len(measured)} 件・固定 '
                        f'{len(fixed["cases"])} 件・違う {changed[:5]})')
    return len(measured), problems


# ---- 流し方 ----

def work_root():
    base = os.environ.get('R4X1_WORK') or None
    return Path(tempfile.mkdtemp(prefix='r4x1-', dir=base)).resolve()


MAC_REASONS = {}
# 範囲 full の流しで集める、範囲 mac のケースの旧版の結果(Mac の固定期待の形の dict か、入れない理由の文字列)。
MAC_OLD = {}
MAC_SCOPE_NAMES = set()


# ---- Mac の固定期待(bash 4.0 以上が無い Mac。S3I-A の mac_fixed.json と同じ扱い) ----
#
# 基準の容器の範囲 full で旧版を流した結果から作る(Rust の出力は使わない)。範囲 mac のケースを、固定期待に入れる
# ケースと、入れない理由を付けたケースに分ける。旧版を流せない Mac では、Rust の結果をこの固定期待と、旧版との比較と
# 同じ置き換えの規則(compare_sides)で比べる。入れない理由は、Mac の未実施として名前と理由を出す。

ROOT_MARK = b'@ROOT@'
OLD_MARK = b'@OLD@'


def to_fixed_text(data, root):
    data = data.replace(str(root).encode(), ROOT_MARK).replace(str(OLD).encode(), OLD_MARK)
    return data.decode('utf-8', 'surrogateescape')


def from_fixed_text(text, root):
    data = text.encode('utf-8', 'surrogateescape')
    return data.replace(ROOT_MARK, str(root).encode()).replace(OLD_MARK, str(OLD).encode())


FIXED_TOOLS = ('grep', 'dirname', 'basename', 'mkdir')


def mac_fixed_entry(case, root, old):
    """旧版の結果を Mac の固定期待の形にする。入れないときは理由の文字列を返す。

    旧版が起動した道具は、Rust が起動しない形(Rust が同じ結果を中で作る形)だけを認める: 352 行の grep は
    `-` で始まらないパス(C の地域設定の `[^[:space:]]` の有無は GNU と BSD の grep で同じ。範囲 mac は NUL と
    8 ビットの字のケースを外してある)、390・391 行の dirname・basename は `-` と `//` で始まらない値(POSIX が
    結果を決めている。`//` で始まる値は処理系が決める)。"""
    if old.timed_out:
        return '旧版が時間切れになった'
    unknown = sorted(set(old.records) - set(FIXED_TOOLS))
    if unknown:
        return f'旧版が {"・".join(unknown)} を起動した(この区間では起きない形)'
    prompt = last_value(case.args, b'--prompt-file') or b''
    if old.records.get('grep') and prompt.startswith(b'-'):
        return ('Rust も grep を起動する(Mac の BSD の grep の結果と起動の記録を、Linux の GNU の grep の旧版と'
                '照らせない)')
    for tool in ('dirname', 'basename'):
        for record in old.records.get(tool, []):
            value = record['argv'][1] if len(record['argv']) > 1 else b''
            if value.startswith((b'-', b'//')):
                return f'{tool} の値が - か // で始まる(GNU と Mac の BSD の {tool} で結果が違いうる)'
    if old.tree != old.initial:
        return '旧版がファイルを変えた(この区間では起きない形)'
    if needs_reference(case, old):
        return '途中で切れる出力の期待に、故障の無い旧版の結果が要る'
    for data in (old.stdout, old.stderr):
        if ERRNO_TEXT.search(data):
            return 'C ライブラリの errno の文が出る(glibc と Mac の libSystem で文が違いうる)'
    code = old.returncode
    records = {}
    for tool in FIXED_TOOLS:
        for record in old.records.get(tool, []):
            records.setdefault(tool, []).append({
                'argv': [to_fixed_text(a, root) for a in record['argv']],
                'parent': 'old' if int(record.get('ppid', b'0')) == old.pid else 'other'})
    return {'returncode': code if code >= 0 else signal.Signals(-code).name,
            'stdout': to_fixed_text(old.stdout, root), 'stderr': to_fixed_text(old.stderr, root),
            'records': records, 'marks': list(old.marks)}


FIXED_PID = 1   # 固定期待から作る旧版の側の pid(記録の親が旧版かを、この値で表す)


def side_from_fixed(entry, root, rust):
    """固定期待から旧版の側を作る。旧版はファイルを変えない(固定期待に入れる条件)ので、木は Rust の起動前と同じ。"""
    code = entry['returncode']
    code = code if isinstance(code, int) else -int(getattr(signal, code))
    records = {tool: [{'argv': [from_fixed_text(a, root) for a in item['argv']],
                       'ppid': str(FIXED_PID if item['parent'] == 'old' else 0).encode(), 'pid': b'0'}
                      for item in items]
               for tool, items in entry['records'].items()}
    return Side(code, from_fixed_text(entry['stdout'], root), from_fixed_text(entry['stderr'], root),
                dict(rust.initial), records, list(entry['marks']), FIXED_PID, False, dict(rust.initial))


def mac_fixed_now(names):
    """範囲 full の流しで集めた結果から作った、今の Mac の固定期待(範囲 mac のケースだけ)。"""
    cases = {n: e for n, e in sorted(MAC_OLD.items()) if n in names and isinstance(e, dict)}
    unperformed = {n: e for n, e in sorted(MAC_OLD.items()) if n in names and isinstance(e, str)}
    return {'about': 'bash 4.0 以上が無い Mac で、Rust と比べる旧版の結果(基準の容器の範囲 full で旧版を流して作る。'
                     'Rust の出力は使わない)。unperformed は範囲 mac のうち固定期待に入れないケースと理由',
            'cases': cases, 'unperformed': unperformed}


def check_mac_fixed(names, groups):
    """範囲 full: 今の旧版の結果から作った Mac の固定期待が、固定したファイルと同じこと。群ごとに 1 件以上。"""
    problems = []
    missing = sorted(set(names) - set(MAC_OLD))
    if missing:
        problems.append(f'範囲 mac のケースの旧版の結果を集められない: {missing[:4]}')
    now = mac_fixed_now(names)
    fixed = json.loads(MAC_FIXED.read_text(encoding='utf-8')) if MAC_FIXED.is_file() else {}
    for key in ('cases', 'unperformed'):
        if fixed.get(key) != now[key]:
            changed = sorted(set(fixed.get(key, {})) ^ set(now[key]) |
                             {n for n in now[key] if fixed.get(key, {}).get(n) != now[key][n]})
            problems.append(f'Mac の固定期待({key})が今の旧版の結果と違う: {changed[:4]}')
    for group in sorted(set(groups.values())):
        if not any(groups[n] == group for n in now['cases']):
            problems.append(f'Mac の固定期待に、群 {group} のケースが 1 件も無い')
    return problems


def run_mac_fixed(cases, root, jobs):
    """bash 4.0 以上が無い Mac: Rust の結果を、Mac の固定期待と比べる。(比べたケース, 結果, 境界, 問題, 未実施)。"""
    fixed = json.loads(MAC_FIXED.read_text(encoding='utf-8'))
    entries, skipped = fixed['cases'], fixed['unperformed']
    names = {case.name for case in cases}
    problems = []
    if set(entries) & set(skipped) or set(entries) | set(skipped) != names:
        outside = sorted(names - set(entries) - set(skipped))
        extra = sorted((set(entries) | set(skipped)) - names)
        problems.append(f'Mac の固定期待の一覧が範囲 mac のケースと違う(固定期待に無い {outside[:4]}・範囲 mac に無い '
                        f'{extra[:4]})')
    chosen = [case for case in cases if case.name in entries]
    results = {}
    boundaries = {}
    lock = threading.Lock()

    def one(item):
        index, case = item
        base = root / 'mac' / f'c{index}'
        try:
            rust = run_side(case, base, legacy=False)
            old = side_from_fixed(entries[case.name], base, rust)
            found = compare_sides(case, base, old, rust)
            hit = rust.returncode == BOUNDARY
        except Mismatch as error:
            found, hit = [str(error)], False
        except Exception:  # noqa: BLE001  比較器の誤りもケースの不合格として出す
            found, hit = [f'比較器の例外: {traceback.format_exc()[-1500:]}'], False
        finally:
            if base.exists():
                make_writable(base)
                shutil.rmtree(base, ignore_errors=True)
        with lock:
            results[case.name] = found
            if hit:
                boundaries[case.group] = boundaries.get(case.group, 0) + 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        list(pool.map(one, enumerate(chosen)))
    unperformed = [f'{name}: {reason}' for name, reason in sorted(skipped.items()) if name in names]
    return chosen, results, boundaries, problems, unperformed


def run_cases(cases, root, jobs):
    results = {}
    boundaries = {}
    lock = threading.Lock()

    def one(item):
        index, case = item
        found = []
        hit = False
        reason = None
        entry = None
        for attempt in range(case.repeat):
            case_root = root / 'cases' / f'c{index}'
            problems, boundary, sides = run_case(case, case_root)
            found += problems
            hit = hit or boundary
            # 回ごとに集め、最初の空でない値を採る。
            reason = reason or static_mac_reason(case) or (dynamic_mac_reason(sides[0]) if sides else None)
            if entry is None and case.name in MAC_SCOPE_NAMES:
                entry = mac_fixed_entry(case, case_root, sides[0]) if sides else '旧版の結果を取れない'
        with lock:
            if entry is not None:
                MAC_OLD[case.name] = entry
            results[case.name] = found
            if reason:
                MAC_REASONS[case.name] = reason
            if hit:
                boundaries[case.group] = boundaries.get(case.group, 0) + 1
        return case

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        list(pool.map(one, enumerate(cases)))
    return results, boundaries


def main(argv=None):
    global FIXTURE, TOOLS, BASH, BACK, EUCJP, LINUX, SYSTEM, MAC_SCOPE_NAMES
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', required=True)
    parser.add_argument('--scope', choices=('full', 'mac'))
    parser.add_argument('--groups', help='開発用: 指定した群だけを流す(合格に数えない。終了値 3)')
    parser.add_argument('--case', help='開発用: 名前にこの字を含むケースだけを流す(合格に数えない。終了値 3)')
    parser.add_argument('-j', type=int, default=os.cpu_count() or 4)
    parser.add_argument('--write-closed-fds', help='開発用: 起動時に閉じた fd の固定期待を書く(合格に数えない)')
    parser.add_argument('--write-case-lists', help='開発用: ケースの一覧の固定を書く(合格に数えない)')
    parser.add_argument('--stability', type=int, default=3, help='止め所の揺れの測定で旧版を流す回数')
    parser.add_argument('--write-mac-list', help='開発用: Mac で流さないケースの一覧を書く(合格に数えない)')
    parser.add_argument('--write-tool-fixture', help='開発用: 道具の固定期待(tool_outputs.json)を書く(合格に数えない)')
    parser.add_argument('--write-mac-fixed', help='開発用: Mac の固定期待(mac_fixed.json)を書く(範囲 full の旧版から。'
                                                  '合格に数えない)')
    args = parser.parse_args(argv)
    if SIMULATE_MAC:
        # Linux で、bash 4.0 以上が無い Mac の流れ(範囲 mac・Mac の固定期待との比較)を確かめる。
        if args.scope == 'full' or not sys.platform.startswith('linux'):
            raise ValueError('R4X1_SIMULATE_MAC=1 は Linux で、範囲 mac だけに使う')
        LINUX, SYSTEM = False, 'darwin'
        print(f'{PREFIX}: Mac の経路の模擬(R4X1_SIMULATE_MAC=1)です。bash 4.0 以上が無い Mac として流します。'
              'Linux の合格でも、Mac の実機の確認でもありません', flush=True)
    if sys.version_info[:2] != (3, 10) or os.geteuid() == 0:
        raise ValueError('R4X-1 の比較は Python 3.10 系・root 以外で実行してください')
    FIXTURE = fixture_path(args.fixture)
    scope = args.scope or ('full' if LINUX else 'mac')
    # 比較の入力に使わない環境変数は、ケースの環境を一から作るので渡らない。受け継いだ環境にあった名前を記録する。
    names, prefixes = excluded_env()
    inherited = sorted(n for n in os.environ if n in names or n.startswith(prefixes))
    print(f'{PREFIX}: 比較の入力に使わない環境変数のうち、受け継いだ環境にあって外した名前: {inherited or "なし"}', flush=True)
    started = time.monotonic()
    BASH = None if SIMULATE_MAC else find_bash()
    check_sources()
    excluded_env()
    unperformed = []
    problems = []
    if scope == 'full':
        missing = baseline()
        if missing:
            raise AssertionError(f'基準の版と違う道具がある(版に依る欄を比べられない): {missing}')
        pack = language_pack()
        for domain in ('bash', 'coreutils', 'libc'):
            if domain not in pack:
                raise AssertionError(f'日本語の翻訳ファイル {domain}.mo が無い(language-pack-ja を入れる)')
        print(f'{PREFIX}: 日本語の翻訳ファイル: {pack}', flush=True)
        for item in full_requirements():
            problems.append(f'範囲 full の要件: {item}')
    pattern, soft, hard = core_state()
    print(f'{PREFIX}: core_pattern: {pattern!r}・RLIMIT_CORE: {soft}/{hard}(core を残しうるケースは 0 にする)',
          flush=True)
    root = work_root()
    stage_counts = {}
    try:
        TOOLS = Tools(root / 'tools')
        if LINUX:
            EUCJP = make_eucjp(root)
        BACK = Back()
        if args.write_case_lists:
            Path(args.write_case_lists).write_text(json.dumps(case_lists(), ensure_ascii=False, indent=1,
                                                              sort_keys=True) + '\n', encoding='utf-8')
            print(f'{PREFIX}: ケースの一覧を書いた')
            return 3
        if args.write_tool_fixture:
            print(f'{PREFIX}: 道具の固定期待: {write_tool_fixture(root, args.write_tool_fixture)} 件')
            return 3
        if args.write_closed_fds:
            count, _ = run_closed_stage(root, args.write_closed_fds)
            print(f'{PREFIX}: 起動時に閉じた fd の固定期待: {count} 件')
            return 3
        cases = all_cases(scope)
        if not args.groups and not args.case:
            problems += check_case_lists()
        cases = apply_core_limit(cases, pattern)
        if args.groups:
            names = set(args.groups.split(','))
            cases = [c for c in cases if c.group in names]
        if args.case:
            cases = [c for c in cases if args.case in c.name]
        complete = not args.groups and not args.case
        if scope == 'full' and complete:
            mac_cases = all_cases('mac')
            MAC_SCOPE_NAMES = {case.name for case in mac_cases}
        stage = time.monotonic()
        if BASH is None and scope == 'mac':
            # bash 4.0 以上が無い Mac: 旧版の代わりに、基準の容器で旧版を流して作った Mac の固定期待と比べる。
            cases, results, boundaries, found, skipped = run_mac_fixed(cases, root, max(args.j, 1))
            if not complete:
                found = [f for f in found if not f.startswith('Mac の固定期待の一覧が')]
            problems += found
            unperformed += skipped
            print(f'{PREFIX}: Mac の固定期待と比べた件数: {len(cases)} 件(bash 4.0 以上が無い。固定期待に入れない'
                  f'範囲 mac のケース {len(skipped)} 件は未実施)', flush=True)
            print(f'{PREFIX}: 時間: Mac の固定期待 {time.monotonic() - stage:.1f} 秒', flush=True)
        else:
            if BASH is None:
                unperformed.append(f'旧版との比較の全部({len(cases)} 件): bash 4.0 以上が無い')
                cases = []
            results, boundaries = run_cases(cases, root, max(args.j, 1))
            print(f'{PREFIX}: 時間: 群 {time.monotonic() - stage:.1f} 秒', flush=True)
        if scope == 'full' and complete:
            if args.write_mac_fixed:
                now = mac_fixed_now(MAC_SCOPE_NAMES)
                Path(args.write_mac_fixed).write_text(json.dumps(now, ensure_ascii=True, indent=1) + '\n',
                                                      encoding='utf-8')
                print(f'{PREFIX}: Mac の固定期待: {len(now["cases"])} 件(入れない範囲 mac のケース '
                      f'{len(now["unperformed"])} 件)')
                return 3
            problems += check_mac_fixed(MAC_SCOPE_NAMES, {case.name: case.group for case in mac_cases})
            listed = load_mac_list()
            if args.write_mac_list:
                Path(args.write_mac_list).write_text(json.dumps({
                    'about': 'Mac の範囲で流さないケースと理由(基準の Linux の範囲 full の結果から作る。'
                             '一覧の外の未実施は失敗。範囲 full のたびに、今の結果と同じことを確かめる)',
                    'cases': dict(sorted(MAC_REASONS.items()))}, ensure_ascii=False, indent=1) + '\n',
                    encoding='utf-8')
                print(f'{PREFIX}: Mac で流さないケース: {len(MAC_REASONS)} 件')
                return 3
            if listed != MAC_REASONS:
                changed = sorted(set(listed.items()) ^ set(MAC_REASONS.items()))
                problems.append(f'Mac で流さないケースの一覧が今の結果と違う: {changed[:4]}')
        tool_count = 0
        if scope == 'full' and not args.groups and not args.case:
            stage = time.monotonic()
            tool_count, found = run_tool_stage(root)
            problems += found
            print(f'{PREFIX}: 時間: 道具の固定期待 {time.monotonic() - stage:.1f} 秒', flush=True)
            for label, run in (('起動時に閉じた fd', lambda: run_closed_stage(root)),
                               ('止め所の揺れ', lambda: run_stability_stage(root, cases, args.stability))):
                stage = time.monotonic()
                count, found = run()
                stage_counts[label] = count
                problems += found
                print(f'{PREFIX}: 時間: {label} {time.monotonic() - stage:.1f} 秒', flush=True)
            stage = time.monotonic()
            count, found, traced = run_wrapper_stage(root)
            stage_counts['包みの有無'] = count
            problems += found
            if not traced:
                unperformed.append('包みの有無の子の環境の確かめ: strace が無い')
            print(f'{PREFIX}: 時間: 包みの有無 {time.monotonic() - stage:.1f} 秒', flush=True)
        if scope == 'mac':
            for name, reason in sorted(load_mac_list().items()):
                unperformed.append(f'{name}: {reason}')
            unperformed.append('道具の固定期待の測り直し・起動時に閉じた fd・止め所の揺れ・包みの有無: 基準の版の道具で測る段'
                               '(範囲 full だけ)')
    finally:
        make_writable(root)
        shutil.rmtree(root, ignore_errors=True)
    groups = {}
    for case in cases:
        groups.setdefault(case.group, [0, 0])
        groups[case.group][0] += 1
        if results.get(case.name):
            groups[case.group][1] += 1
    for group, (count, failed) in groups.items():
        print(f'{PREFIX}: 群 {group}: {count} 件(不一致 {failed} 件・境界の印 {boundaries.get(group, 0)} 件)')
    if tool_count:
        print(f'{PREFIX}: 道具の固定期待(本物の道具の測り直し): {tool_count} 件')
    for label, count in stage_counts.items():
        print(f'{PREFIX}: {label}: {count} 件')
    failures = [(name, found) for name, found in results.items() if found]
    for name, found in sorted(failures):
        for problem in found[:3]:
            print(f'{PREFIX}: 不一致: {name}: {problem}'.replace('\n', '⏎'))
    for problem in problems:
        print(f'{PREFIX}: 不一致: {problem}')
    for item in unperformed:
        print(f'{PREFIX}: 未実施: {item}')
    total = len(cases) + tool_count + stage_counts.get('起動時に閉じた fd', 0)
    print(f'{PREFIX}: 時間: 全体 {time.monotonic() - started:.1f} 秒')
    if args.groups or args.case:
        print(f'{PREFIX}: 部分実行(合格に数えない): {total} 件・不一致 {len(failures) + len(problems)} 件')
        return 3
    code, line = final_line(total, len(failures) + len(problems), len(unperformed), scope)
    print(line)
    if SIMULATE_MAC and code == 0:
        # 模擬は Linux の範囲 full も Mac の実機も確かめていないので、合格に数えない。
        print(f'{PREFIX}: 模擬の合格(Mac の経路の模擬。Linux の合格でも、Mac の実機の確認でもない): {total} 件'
              f'(未実施 {len(unperformed)} 件)')
        return SIMULATION_EXIT
    return code


def final_line(total, mismatches, unperformed, scope='full'):
    """(終了値, 最後の行)。不一致があるか 0 件なら不合格。範囲 full(Linux の基準)では、未実施も不合格。"""
    if mismatches:
        return 1, f'不合格: {mismatches} 件の不一致({total} 件中)'
    if total == 0:
        return 1, '不合格: 比べた件数が 0 件'
    if scope == 'full' and unperformed:
        return 1, f'不合格: 範囲 full で未実施が {unperformed} 件ある({total} 件中。基準の機械では未実施にしない)'
    return 0, f'合格: {total} 件(未実施 {unperformed} 件)'


def installed_locales():
    done = subprocess.run(['locale', '-a'], capture_output=True, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
    return {name.lower() for name in done.stdout.split()}


def full_requirements():
    """範囲 full に要るもののうち、足りないもの(strace で execve を記録できること・ja_JP.utf8 の地域設定)。"""
    missing = []
    if not strace_usable():
        missing.append('strace で execve を記録できない(包みの有無の段で grep の子の環境を照らせない)')
    if b'ja_jp.utf8' not in installed_locales():
        missing.append('locale -a に ja_JP.utf8 が無い(locales と language-pack-ja を入れる)')
    return missing



# ---- 起動時に閉じた fd(2026-11-12 08:55 の決定: 旧版と Rust を、それぞれの固定期待で確かめ、互いには比べない) ----

CLOSED_ARGS = (
    ('usage', ['--runner'], None),
    ('fail_usage', ['--runner', 'codex', '--prompt-file', P], None),
    ('境界', VALID, None),
    ('grep起動', ['--runner', 'codex', '--prompt-file', '-v', '--cwd', '.'], {'work/-v': b'x\n'}),
)
CLOSED_SETS = ((0,), (1,), (2,), (1, 2), (0, 1, 2))


def closed_cases():
    cases = []
    for label, args, files in CLOSED_ARGS:
        for closed in CLOSED_SETS:
            cases.append(Case('起動時に閉じた fd', f'{label}-閉{"".join(map(str, closed))}', args,
                              setup=layout(files=files) if files else None, close=closed, stdin=b'x\n'))
    return cases


def observation(side, root):
    def clean(value):
        return value.replace(str(root).encode(), b'@R').decode('utf-8', 'backslashreplace')
    records = {}
    for tool, items in sorted(side.records.items()):
        records[tool] = [{key: clean(record[key]) if isinstance(record.get(key), bytes) else
                          [clean(a) for a in record.get(key, [])]
                          for key in ('argv', 'fd0', 'fd1', 'fd2')} for record in items]
    return {'returncode': side.returncode, 'stdout': clean(side.stdout), 'stderr': clean(normalize_old(side.stderr)),
            'records': records, 'tree_changed': side.tree != side.initial}


def run_closed_stage(root, write=None):
    problems = []
    measured = {}
    for index, case in enumerate(closed_cases()):
        base = root / 'closed' / f'c{index}'
        try:
            measured[case.label] = {
                'old': observation(run_side(case, base, legacy=True), base),
                'rust': observation(run_side(case, base, legacy=False), base),
            }
        finally:
            if base.exists():
                make_writable(base)
                shutil.rmtree(base, ignore_errors=True)
    if write:
        Path(write).write_text(json.dumps({'about': '起動時に閉じた fd の固定期待(基準の容器で測った。'
                                                    '旧版と Rust は互いに比べない。2026-11-12 08:55 の決定)',
                                           'cases': measured}, ensure_ascii=False, indent=1, sort_keys=True) + '\n',
                               encoding='utf-8')
        return len(measured), problems
    fixed = json.loads(CLOSED_FDS.read_text(encoding='utf-8'))['cases']
    if sorted(fixed) != sorted(measured):
        problems.append(f'起動時に閉じた fd: ケースの一覧が固定期待と違う({len(measured)} 件・固定 {len(fixed)} 件)')
    for label, sides in measured.items():
        for side in ('old', 'rust'):
            if fixed.get(label, {}).get(side) != sides[side]:
                problems.append(f'起動時に閉じた fd/{label}: {side} が固定期待と違う: {sides[side]!r:.400}')
    return len(measured), problems


# ---- 包みの有無(比較器の自己検査): 包みが旧版の結果(stdout・stderr・終了値・子の環境)を変えないこと ----

def wrapper_cases():
    """境界まで進まない(包みの無い旧版が mkdir を本当に起こさない)ケース。"""
    picked = []
    for case in group_arguments() + group_runner_model() + group_timeouts() + group_prompt() + group_unexpected():
        if case.cwd == 'GONE' or case.stop or case.raw_env is not None:
            continue
        picked.append(case)
    return picked


def wrapper_differences(with_wrapper, plain):
    """包みの有無で変わった旧版の結果の項目。"""
    return [f'包みで旧版の {key} が変わった' for key in ('returncode', 'stdout', 'stderr')
            if getattr(with_wrapper, key) != getattr(plain, key)]


def execve_env(path, tool):
    """strace の記録から、その道具の execve の環境の項目を拾う(`_` を除く)。"""
    if not path.is_file():
        return None
    for line in path.read_text(encoding='utf-8', errors='surrogateescape').splitlines():
        if 'execve("' not in line or f'["{tool}"' not in line:
            continue
        found = re.search(r'\], (\[.*\])\) = 0', line)
        if not found:
            continue
        items = re.findall(r'"((?:[^"\\]|\\.)*)"', found.group(1))
        values = [codecs_unescape(item) for item in items]
        return sorted(v for v in values if not v.startswith(b'_='))
    return None


def codecs_unescape(text):
    out = bytearray()
    index = 0
    raw = text.encode('utf-8', 'surrogateescape')
    while index < len(raw):
        if raw[index:index + 1] == b'\\' and index + 1 < len(raw):
            nxt = raw[index + 1:index + 2]
            simple = {b'n': b'\n', b't': b'\t', b'"': b'"', b'\\': b'\\', b'r': b'\r', b'v': b'\v', b'f': b'\f'}
            if nxt in simple:
                out += simple[nxt]
                index += 2
                continue
            if nxt == b'x':
                out.append(int(raw[index + 2:index + 4], 16))
                index += 4
                continue
            if nxt.isdigit():
                end = index + 1
                while end < len(raw) and end < index + 4 and raw[end:end + 1].isdigit():
                    end += 1
                out.append(int(raw[index + 1:end], 8))
                index = end
                continue
        out += raw[index:index + 1]
        index += 1
    return bytes(out)


def strace_usable():
    """strace で execve を記録できるか(ptrace が使えない機械では未実施と記録する。S3I-A と同じ扱い)。"""
    if not LINUX or shutil.which('strace') is None:
        return False
    with tempfile.TemporaryDirectory() as directory:
        trace = Path(directory) / 'trace'
        try:
            subprocess.run(['strace', '-f', '-qq', '-e', 'trace=execve', '-o', str(trace), '/bin/true'],
                           capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return trace.is_file() and 'execve("/bin/true"' in trace.read_text(errors='replace')


def run_wrapper_stage(root):
    problems = []
    count = 0
    traced_count = 0
    have_strace = strace_usable()
    for index, case in enumerate(wrapper_cases()):
        base = root / 'wrapper' / f'c{index}'
        try:
            with_wrapper = run_side(case, base, legacy=True)
            if with_wrapper.records.get('mkdir'):
                continue
            count += 1
            bare = dataclasses.replace(case, wrapper=False)
            plain = run_side(bare, base, legacy=True)
            problems += [f'包みの有無/{case.name}: {p}' for p in wrapper_differences(with_wrapper, plain)]
            if have_strace and with_wrapper.records.get('grep'):
                traced = dataclasses.replace(case, wrapper=False)
                trace = root / 'wrapper' / f'strace{index}'
                object.__setattr__(traced, 'strace', trace)
                run_side(traced, base, legacy=True)
                env = execve_env(trace, 'grep')
                traced_count += 1
                recorded = sorted(e for e in with_wrapper.records['grep'][0]['env'] if not e.startswith(b'_='))
                if env is None:
                    problems.append(f'包みの有無/{case.name}: strace の記録に grep の execve が無い')
                elif [e.replace(str(base).encode(), b'@R') for e in env] != [
                        e.replace(str(base).encode() + b'/obs/bin:', b'').replace(str(base).encode(), b'@R')
                        for e in recorded]:
                    problems.append(f'包みの有無/{case.name}: 包みで旧版の grep の子の環境が変わった')
        finally:
            if base.exists():
                make_writable(base)
                shutil.rmtree(base, ignore_errors=True)
    if count == 0:
        problems.append('包みの有無: 比べた件数が 0 件')
    if have_strace and traced_count == 0:
        problems.append('包みの有無: strace で子の環境を照らした件数が 0 件')
    print(f'{PREFIX}: 包みの有無: strace で子の環境を照らした件数 {traced_count} 件', flush=True)
    return count, problems, have_strace


# ---- 止め所の揺れ(2026-11-12 06:58 の追加の決定 2): 旧版を何回も流し、罠の時点が揺れないかを測る ----

def run_stability_stage(root, cases, times):
    problems = []
    count = 0
    for index, case in enumerate(c for c in cases if c.stop):
        base = root / 'stability' / f'c{index}'
        outcomes = set()
        try:
            for _ in range(times):
                side = run_side(case, base, legacy=True)
                outcomes.add((side.returncode, normalize_old(side.stderr).replace(str(base).encode(), b'@R'),
                              tuple(side.marks)))
                count += 1
        finally:
            if base.exists():
                make_writable(base)
                shutil.rmtree(base, ignore_errors=True)
        if len(outcomes) != 1:
            problems.append(f'止め所の揺れ/{case.name}: 旧版の結果が {len(outcomes)} 通りに揺れた')
    return count, problems


# ---- ケースの一覧の固定 ----

CASE_LISTS = FIXTURES / 'case_lists.json'


def case_lists():
    data = {}
    for scope in ('full', 'mac'):
        groups = {}
        for case in all_cases(scope):
            groups.setdefault(case.group, []).append(case.name)
        data[scope] = {group: {'count': len(names), 'sha256': sha256_bytes('\n'.join(sorted(names)).encode())}
                       for group, names in groups.items()}
    return data


def check_case_lists():
    fixed = json.loads(CASE_LISTS.read_text(encoding='utf-8'))
    now = case_lists()
    problems = []
    for scope in ('full', 'mac'):
        if fixed.get(scope) != now[scope]:
            changed = sorted(set(fixed.get(scope, {})) ^ set(now[scope]) |
                             {g for g in now[scope] if fixed.get(scope, {}).get(g) != now[scope][g]})
            problems.append(f'ケースの一覧が固定と違う(範囲 {scope}・群 {changed})')
    return problems


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (AssertionError, ValueError, OSError) as error:
        print(f'不合格: {error}')
        sys.exit(1)
