"""合成機密に対するOS読取り監査。straceに本文バッファを記録させない。"""
import ast
from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import re
import shutil
import subprocess
from .compare import AuditEvent, audit_reads, fixed, run, same_result, unchanged

STRING = r'"(?:[^"\\]|\\.)*"'
TRACE_CALLS = '%process,open,openat,read,pread64,mmap,close,dup,dup2,dup3,chdir,fchdir,link,linkat,rename,renameat,renameat2'


def cbytes(value):
    # straceは非ASCII byteを八進escapeで出す。Unicodeへの置換を行わない。
    result = ast.literal_eval(value)
    if not isinstance(result, str):
        raise AssertionError('straceの文字列形式が不正です')
    return result.encode('latin1')


def number(value):
    value = value.strip()
    return int(value, 16 if value.startswith('0x') else 10)


@dataclass
class Process:
    executable: bytes = b''
    argv: tuple = ()
    fds: dict = field(default_factory=dict)
    cwd: bytes = b''


def ordered_calls(raw):
    """子が先に出力しても、clone開始時点のfdを引き継げる順序へ復元する。"""
    pending, records = {}, []
    for line in raw.decode('ascii').splitlines():
        match = re.match(r'^(\d+)\s+(.*)$', line)
        if not match:
            raise AssertionError('straceのPID列がありません')
        pid, operation = int(match[1]), match[2]
        if operation.endswith('<unfinished ...>'):
            if pid in pending:
                raise AssertionError('straceの未完了呼出しが重複しています')
            pending[pid] = (len(records), operation[:-len('<unfinished ...>')])
            records.append(None)
        elif operation.startswith('<... '):
            resume = re.match(r'<\.\.\. \w+ resumed>(.*)$', operation)
            if resume is None or pid not in pending:
                raise AssertionError('straceの再開呼出しが不正です')
            index, beginning = pending.pop(pid)
            records[index] = (pid, beginning + resume[1])
        else:
            records.append((pid, operation))
    if pending or any(record is None for record in records):
        raise AssertionError('straceに回収できていない呼出しがあります')
    return records


def parse_trace(raw, *, cwd, secret_identities, git_executables):
    """成功execとfdの生存期間を追う。本文readの文字列表示があれば拒む。"""
    processes, events, git_calls = {}, [], []
    secret_identities=dict(secret_identities)
    identity_paths = {}
    for path, identity in secret_identities.items():
        identity_paths.setdefault(identity, path)
    def secret(path):
        if path in secret_identities:
            identity = secret_identities[path]
            return identity_paths[identity], identity
        return None
    def base_for(descriptor,process):
        annotated=re.search(r'<([^>]+)>',descriptor)
        if annotated:return cbytes(chr(34)+annotated[1]+chr(34))
        if descriptor=='AT_FDCWD':return process.cwd
        try:return process.fds[int(descriptor)]
        except (ValueError,KeyError) as error:raise AssertionError('相対pathの親fdが追跡されていません') from error
    for pid, operation in ordered_calls(raw):
        process = processes.setdefault(pid, Process(cwd=os.fsencode(cwd)))
        if operation.startswith(('+++', '---')):
            continue
        if operation.startswith('execve(') and re.search(r'\)\s+= 0$', operation):
            match = re.match(r'execve\((' + STRING + r'), \[(.*?)\], ', operation)
            if match is None or '...' in match[2]:
                raise AssertionError('straceの実行引数が欠落しています')
            executable = cbytes(match[1])
            argv = tuple(cbytes(value) for value in re.findall(STRING, match[2]))
            process.executable = os.path.realpath(executable)
            process.argv = argv
            if process.executable in git_executables:
                event = AuditEvent('', 'Spawn', child_pid=pid, argv=argv, executable=process.executable)
                git_calls.append(event)
                events.append(event)
            continue
        if operation.startswith(('clone(', 'clone3(', 'fork(', 'vfork(')):
            match = re.search(r'\)\s+= (\d+)$', operation)
            if match:
                child = int(match[1])
                if child in processes:
                    raise AssertionError('clone開始前に子の操作が観測されました')
                processes[child] = Process(process.executable, process.argv, dict(process.fds), process.cwd)
            continue
        if operation.startswith('chdir(') and operation.endswith('= 0'):
            match = re.match(r'chdir\((' + STRING + r')\)', operation)
            if match:
                path = cbytes(match[1])
                process.cwd = os.path.normpath(os.path.join(process.cwd, path))
            continue
        if operation.startswith('fchdir(') and operation.endswith('= 0'):
            match = re.match(r'fchdir\((\d+)(?:<([^>]+)>)?\)', operation)
            if match is None:
                raise AssertionError('straceのfchdirを解釈できません')
            base = cbytes('"' + match[2] + '"') if match[2] else process.fds.get(int(match[1]))
            if base is None:
                raise AssertionError('fchdirのfdが追跡されていません')
            process.cwd = base
            continue
        if operation.startswith(('link(','linkat(','rename(','renameat(','renameat2(')) and operation.endswith('= 0'):
            ordinary=re.match(r'(link|rename)\(('+STRING+r'), ('+STRING+r')\)',operation)
            if ordinary:
                old=os.path.normpath(os.path.join(process.cwd,cbytes(ordinary[2])))
                new=os.path.normpath(os.path.join(process.cwd,cbytes(ordinary[3])))
            else:
                relative=re.match(r'(?:linkat|renameat2?)\(([^,]+), ('+STRING+r'), ([^,]+), ('+STRING+r')',operation)
                if relative is None:raise AssertionError('hardlink/renameを解釈できません')
                old=os.path.normpath(os.path.join(base_for(relative[1],process),cbytes(relative[2])))
                new=os.path.normpath(os.path.join(base_for(relative[3],process),cbytes(relative[4])))
            if old in secret_identities:secret_identities[new]=secret_identities[old]
            else:secret_identities.pop(new,None)
            continue
        if operation.startswith(('open(', 'openat(')):
            if operation.startswith('openat('):
                match = re.match(r'openat\(([^,]+), (' + STRING + r'),', operation)
                if match is None:
                    raise AssertionError('straceのopenatを解釈できません')
                descriptor, path = match[1], cbytes(match[2])
                base = base_for(descriptor,process) if not os.path.isabs(path) else process.cwd
            else:
                match = re.match(r'open\((' + STRING + r'),', operation)
                if match is None:
                    raise AssertionError('straceのopenを解釈できません')
                path, base = cbytes(match[1]), process.cwd
            path = os.path.normpath(os.path.join(base, path))
            result = re.search(r'\)\s+= (-?\d+)(?:<([^>]+)>)?', operation)
            if result is None:
                raise AssertionError('straceのopen結果がありません')
            fd = int(result[1])
            resolved = cbytes(chr(34) + result[2] + chr(34)) if result[2] else path
            if fd >= 0:
                process.fds[fd] = resolved
            target = secret(resolved) or secret(path)
            if target:
                path, (device, inode) = target
                events.append(AuditEvent('', 'Open', path, device, inode, fd,
                                         executable=process.executable, child_pid=pid))
            continue
        if operation.startswith(('read(', 'pread64(')):
            match = re.match(r'(?:read|pread64)\((0x[0-9a-f]+|\d+)(?:<([^>]+)>)?,', operation)
            result = re.search(r'\)\s+= (-1|0x[0-9a-f]+|\d+)(?:\s|$)', operation)
            if match is None or result is None:
                raise AssertionError('straceのraw readを解釈できません')
            if '"' in operation[match.end():]:
                raise AssertionError('本文バッファを記録するstrace設定です')
            fd, amount = number(match[1]), number(result[1])
            path = process.fds.get(fd)
            if match[2] is not None:
                annotated=cbytes(chr(34)+match[2]+chr(34))
                if path is None:raise AssertionError('raw readの注記に対応するfdが追跡されていません')
                if annotated!=path:
                    prior,observed=secret(path),secret(annotated)
                    if prior is None or observed is None or prior[1]!=observed[1]:
                        raise AssertionError('raw readの注記と保持fdの実体が一致しません')
            target = secret(path)
            if target and amount > 0:
                path, (device, inode) = target
                events.append(AuditEvent('', 'Read', path, device, inode, fd, amount,
                                         executable=process.executable, child_pid=pid))
            continue
        if operation.startswith('mmap('):
            match = re.match(r'mmap\([^,]+, (\d+), [^,]+, [^,]+, (\d+)(?:<[^>]+>)?,', operation)
            if match and ' = -1 ' not in operation:
                amount, fd = int(match[1]), int(match[2])
                path = process.fds.get(fd)
                target = secret(path)
                if target:
                    path, (device, inode) = target
                    events.append(AuditEvent('', 'Map', path, device, inode, fd, amount,
                                             executable=process.executable, child_pid=pid))
            continue
        if operation.startswith('close(') and operation.endswith('= 0'):
            match = re.match(r'close\((\d+)', operation)
            if match:
                process.fds.pop(int(match[1]), None)
            continue
        if operation.startswith(('dup(', 'dup2(', 'dup3(')):
            match = re.match(r'dup\d?\((\d+)', operation)
            result = re.search(r'\)\s+= (\d+)', operation)
            if match and result:
                old, new = int(match[1]), int(result[1])
                if old in process.fds:
                    process.fds[new] = process.fds[old]
                else:
                    process.fds.pop(new, None)
    if not git_calls:
        raise AssertionError('straceでGitの実行へ到達していません')
    # この2呼出しは旧版のpipeで同時に起動する。exec成功の通知順ではなく
    # pipeの入力側→出力側へ固定する。ほかの順序は変更しない。
    for index in range(len(git_calls) - 1):
        first, second = git_calls[index:index + 2]
        if b'--index-info' in first.argv and b'ls-files' in second.argv and b'-s' in second.argv:
            git_calls[index:index + 2] = [second, first]
    phases = {event.child_pid: f'git-{i:03d}' for i, event in enumerate(git_calls)}
    calls = [replace(event, phase=phases[event.child_pid]) for event in git_calls]
    bodies = [replace(event, phase=phases.get(event.child_pid, 'non-git'))
              for event in events if event.operation != 'Spawn']
    return calls + bodies


def traced_pair(fixture, binary, *, legacy, marker, link_creation_mutant=False):
    tool = shutil.which('strace')
    if tool is None:
        raise RuntimeError('Linuxの読取り監査にはstraceが必要です')
    secret = fixture.repo / '.env'
    info = secret.stat()
    identity = (info.st_dev, info.st_ino)
    identities = {os.fsencode(secret): identity}
    # 追跡開始前のmetadata観測で、同じ実体のhardlink/symlink表記も固定する。
    for directory, _, files in os.walk(fixture.repo):
        for name in files:
            path = Path(directory)/name
            try: current = path.stat()
            except OSError: continue
            if (current.st_dev, current.st_ino) == identity:
                identities[os.fsencode(path)] = identity
    git = os.fsencode(os.path.realpath(shutil.which('git')))
    all_events, results = [], []
    for name, command in (('legacy', ['bash', str(legacy)]), ('rust', [str(binary)])):
        fixture.clean_outputs()
        before = fixture.input_state()
        trace = fixture.root / (name + '.strace')
        argv = [tool, '-f', '-yy', '-s', '4096', '-e', 'trace=' + TRACE_CALLS,
                '-e', 'raw=read,pread64', '-e', 'signal=none', '-o', str(trace),
                *command, *fixture.normal(), '--patch-out', str(fixture.patch)]
        result = run(argv, cwd=fixture.root, env=fixture.env, report=fixture.out, patch=fixture.patch)
        after=fixture.input_state()
        if link_creation_mutant and name=='rust':
            # この反例はlink作成/削除を意図的に加えるため、その実体のctimeだけが変わる。
            # 本文・mode・size・mtime・inode・他ファイルの変化は引き続き拒む。
            def without_link_ctime(values):
                return {path:(state[:5]+state[6:] if state and state[:2]==identity else state)
                        for path,state in values.items()}
            unchanged(without_link_ctime(before),without_link_ctime(after))
        else:unchanged(before,after)
        fixed(result, 0)
        raw = trace.read_bytes()
        if marker in raw:
            raise AssertionError('straceに機密本文が記録されました')
        events = parse_trace(raw, cwd=fixture.root, secret_identities=identities, git_executables={git})
        all_events.append(events)
        results.append(result)
    same_result(*results, markers=(marker,))
    audit_reads(*all_events, set(identities) | set(identities.values()), git_executables={git})
    return {'old': len(all_events[0]), 'new': len(all_events[1])}
