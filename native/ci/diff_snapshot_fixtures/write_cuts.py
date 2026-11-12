"""書込みが途中で切れた地点を、straceで観測する(戻り値が短い・EFBIGなどの失敗)。

書けるバイト数はRLIMIT_FSIZEで制限し、SIGXFSZを無視する。制限はstraceではなく、
観測する命令だけに当てる(straceの記録が切れないように)。
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from . import closed_env
from .compare import Result, note_versions

LIMIT = ('import os,resource,signal,sys\n'
         'signal.signal(signal.SIGXFSZ,signal.SIG_IGN)\n'
         'n=int(sys.argv[1]);resource.setrlimit(resource.RLIMIT_FSIZE,(n,n))\n'
         'os.execvp(sys.argv[2],sys.argv[2:])\n')
CALL = re.compile(r'^(\d+\.\d+) (write|pwrite64|writev)\((\d+)<([^>]*)>, (.*)\) += (-1 ([A-Z0-9]+)|\d+)')
# coreutils 9.4のcat・cpはcopy_file_rangeで書く。書く先は3つ目の引数。
COPY = re.compile(r'^(\d+\.\d+) copy_file_range\(\d+<[^>]*>, [^,]+, \d+<([^>]*)>, [^,]+, (\d+), \d+\) += (-1 ([A-Z0-9]+)|\d+)')
IOV = re.compile(r'iov_len=(\d+)')


def available():
    return sys.platform.startswith('linux') and shutil.which('strace') is not None


def _requested(name, rest):
    if name == 'copy_file_range':
        return int(rest)
    if name == 'writev':
        return sum(int(value) for value in IOV.findall(rest))
    parts = rest.rsplit(', ', 2 if name == 'pwrite64' else 1)
    return int(parts[1])


def parse(folder):
    """-ffの記録を時刻の順に並べ、通常ファイルへの書込みのうち切れたものを返す。"""
    cuts = []
    for path in folder.iterdir():
        pid = path.name.rsplit('.', 1)[1]
        for line in path.read_text('utf-8', 'surrogateescape').splitlines():
            found = CALL.match(line)
            copied = COPY.match(line) if not found else None
            if copied:
                stamp, target, length, value, errno = copied.groups()
                name, rest = 'copy_file_range', length
            elif not found:
                continue
            else:
                stamp, name, _, target, rest, value, errno = found.groups()
            if not target.startswith('/') or target.startswith('/dev/'):
                continue
            requested = _requested(name, rest)
            returned = None if errno else int(value)
            # copy_file_rangeは入力の終わりで短く返すのが普通なので、失敗だけを切れたとみなす。
            if errno or (name != 'copy_file_range' and returned < requested):
                cuts.append(dict(time=float(stamp), pid=int(pid), path=target, requested=requested,
                                 returned=returned, errno=errno))
    cuts.sort(key=lambda cut: cut['time'])
    return cuts


def run(argv, *, cwd, env, size, stdout_file=None, stderr_file=None, timeout=120):
    """argvを、書けるバイト数をsizeに制限してstraceの下で動かす。

    stdout_file・stderr_fileを渡すと、その通常ファイルへ向ける(書込みの制限が当たる)。
    """
    if not available():
        raise RuntimeError('straceが無い')
    with tempfile.TemporaryDirectory(prefix='s3d-strace-', dir=cwd) as temporary:
        folder = Path(temporary)
        trace = ['strace', '-ff', '-ttt', '-y', '-s', '0', '-qq', '-e', 'trace=write,pwrite64,writev,copy_file_range',
                 '-e', 'signal=none', '-o', str(folder / 'trace')]
        command = [*trace, '--', sys.executable, '-I', '-c', LIMIT, str(size), *[str(a) for a in argv]]
        streams = {}
        try:
            streams['stdout'] = open(stdout_file, 'wb') if stdout_file else subprocess.PIPE
            streams['stderr'] = open(stderr_file, 'wb') if stderr_file else subprocess.PIPE
            started = time.time()
            closed_env.guard(env, cwd)
            done = subprocess.run(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, start_new_session=True,
                                  timeout=timeout, check=False, **streams)
            ended = time.time()
        finally:
            for value in streams.values():
                if value is not subprocess.PIPE:
                    value.close()
        stdout = Path(stdout_file).read_bytes() if stdout_file else done.stdout
        stderr = Path(stderr_file).read_bytes() if stderr_file else done.stderr
        cuts = parse(folder)
    note_versions(list(argv), cwd, stderr)
    return Result(done.returncode, stdout, stderr, None, None, started, ended), cuts
