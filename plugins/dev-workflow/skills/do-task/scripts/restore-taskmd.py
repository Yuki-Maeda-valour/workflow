#!/usr/bin/env python3
"""検査済みのタスク本文を準備してから置換する内部入口。Git は起動しない。"""
import hashlib
import os
import re
import select
import secrets
import signal
import stat
import subprocess
import sys
import time

try:
    import resource
except ImportError:
    resource = None

WORK_SECONDS = 300
STOP_SECONDS = 5
CLEANUP_SECONDS = 5
POLL_SECONDS = 0.05
CHUNK_SIZE = 65536
MAX_RESPONSE = 4 * 1024 * 1024
VERSION_BYTES = 8192
FD_ROOTS = ('/proc/self/fd', '/dev/fd')
SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


class Failure(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def path_field(path):
    """既存 shell と同じ C 風引用。非 UTF-8 の実名も bytes のまま保つ。"""
    raw = os.fsencode(path)
    if not any(c < 32 or c == 127 or c in (34, 92) for c in raw):
        return raw
    escapes = {7: b'\\a', 8: b'\\b', 9: b'\\t', 10: b'\\n',
               11: b'\\v', 12: b'\\f', 13: b'\\r', 34: b'\\"', 92: b'\\\\'}
    return b'"' + b''.join(escapes.get(c, ('\\%03o' % c).encode('ascii')
                                      if c < 32 or c == 127 else bytes((c,)))
                           for c in raw) + b'"'


def parse_arguments(argv):
    keys = ('--source', '--destination', '--expected-mode', '--expected-sha256', '--cp')
    if len(argv) != 11 or argv[0] != 'restore':
        raise Failure('internal-argument')
    values = {}
    for i in range(1, len(argv), 2):
        key, value = argv[i:i + 2]
        if key not in keys or key in values or not value:
            raise Failure('internal-argument')
        values[key] = value
    if set(values) != set(keys):
        raise Failure('internal-argument')
    for key in ('--source', '--destination', '--cp'):
        value = values[key]
        if not value.startswith('/') or '\x00' in value:
            raise Failure('internal-argument')
        if key != '--cp' and any(p in ('', '.', '..') for p in value[1:].split('/')):
            raise Failure('internal-argument')
    if not re.fullmatch('[0-7]{1,4}', values['--expected-mode']):
        raise Failure('internal-argument')
    if not re.fullmatch('[0-9a-f]{64}', values['--expected-sha256']):
        raise Failure('internal-argument')
    return {key[2:].replace('-', '_'): value for key, value in values.items()}


def identity(info):
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mode,
            info.st_mtime_ns, info.st_ctime_ns)


class Restore:
    """単一の所有者。同期点は unit の patch 用で、外部の制御口ではない。"""
    def __init__(self, options):
        self.options = options
        self.started = time.monotonic()
        self.deadline = self.started + WORK_SECONDS
        self.stopped = None
        self.fds = []
        self.chains = []
        self.child = None
        self.child_unreaped = False
        self.temp_name = None
        self.temp_path = None
        self.temp_identity = None
        self.temp_fd = None
        self.parent_fd = None
        self.entries = {}
        self.touched = 'none'
        self.temp = 'none'
        self.reason = None
        self.source_fd = None
        self.prepared_fd = None
        self.source_info = None
        self.size = None
        self.mode = int(options['expected_mode'], 8)
        self.digest = options['expected_sha256']
        self.fd_root = None
        self.handlers = {}
        self.response_finalized = False
        self.response_stopped = None

    def on_signal(self, number, _frame):
        if self.stopped is None:
            self.stopped = number

    def checkpoint(self, stage):
        if self.stopped is not None:
            raise Failure('interrupted')
        if time.monotonic() >= self.deadline:
            raise Failure('budget-exceeded')

    def keep_fd(self, fd):
        self.fds.append(fd)
        return fd

    def check_features(self):
        required = ('O_DIRECTORY', 'O_NOFOLLOW', 'O_NONBLOCK', 'O_CLOEXEC',
                    'open', 'fstat', 'stat', 'mkdir', 'unlink', 'rmdir',
                    'replace', 'lseek', 'read', 'write', 'fchmod', 'ftruncate',
                    'supports_dir_fd', 'supports_follow_symlinks')
        if any(not hasattr(os, name) for name in required):
            raise Failure('runtime-unavailable')
        if any(not hasattr(signal, name) for name in ('pthread_sigmask', 'SIG_BLOCK', 'SIG_SETMASK')):
            raise Failure('runtime-unavailable')
        if resource is None or not all(hasattr(resource, n) for n in ('RLIMIT_FSIZE', 'setrlimit')):
            raise Failure('runtime-unavailable')
        # replace は rename と同じ dir_fd 実装でも supports_dir_fd に含まれない版がある。
        for operation in (os.open, os.stat, os.mkdir, os.unlink, os.rmdir):
            if operation not in os.supports_dir_fd:
                raise Failure('runtime-unavailable')
        if os.stat not in os.supports_follow_symlinks:
            raise Failure('runtime-unavailable')
        self.cp = os.path.realpath(self.options['cp'])
        if not stat.S_ISREG(os.stat(self.cp).st_mode) or not os.access(self.cp, os.X_OK):
            raise Failure('runtime-unavailable')
        self.check_version()

    def stop_child(self):
        child = self.child
        if self.child_unreaped:
            return False
        if child is None:
            return True
        for action in ('terminate', 'kill'):
            if child.poll() is not None:
                child.wait()
                self.child = None
                return True
            try:
                getattr(child, action)()
            except OSError:
                pass
            until = time.monotonic() + STOP_SECONDS
            while time.monotonic() < until:
                if child.poll() is not None:
                    child.wait()
                    self.child = None
                    return True
                time.sleep(min(POLL_SECONDS, max(0, until - time.monotonic())))
        if child.poll() is not None:
            child.wait()
            self.child = None
            return True
        self.child_unreaped = True
        return False

    def check_version(self):
        pipe = None
        try:
            self.child = subprocess.Popen([self.cp, '--version'], stdin=subprocess.DEVNULL,
                                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                          close_fds=True)
            pipe = self.child.stdout
            end = min(self.deadline, time.monotonic() + 5)
            output = bytearray()
            eof = False
            while not eof or self.child.poll() is None:
                self.checkpoint('version')
                if time.monotonic() >= end:
                    raise Failure('runtime-unavailable')
                if not eof and select.select([pipe], [], [], POLL_SECONDS)[0]:
                    block = os.read(pipe.fileno(), VERSION_BYTES + 1 - len(output))
                    if not block:
                        eof = True
                    else:
                        output.extend(block)
                        if len(output) > VERSION_BYTES:
                            raise Failure('runtime-unavailable')
                elif eof:
                    time.sleep(POLL_SECONDS)
            rc = self.child.wait()
            self.child = None
            if rc or not output.startswith(b'cp (GNU coreutils) '):
                raise Failure('runtime-unavailable')
        except (OSError, ValueError, subprocess.SubprocessError):
            raise Failure('runtime-unavailable')
        finally:
            if self.child is not None and not self.stop_child():
                self.reason = 'child-unreaped'
            if pipe is not None:
                pipe.close()
        if self.child_unreaped:
            raise Failure('child-unreaped')

    def open_parent(self, path, reason):
        try:
            fd = self.keep_fd(os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
            chain = []
            for component in path[1:].split('/')[:-1]:
                self.checkpoint('open-parent')
                child = self.keep_fd(os.open(component, os.O_RDONLY | os.O_DIRECTORY |
                                             os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd))
                chain.append((fd, component, identity(os.fstat(child))))
                fd = child
            self.chains.extend(chain)
            return fd, path.rsplit('/', 1)[1]
        except OSError:
            raise Failure(reason)

    def stat_at(self, fd, name):
        try:
            return os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def check_chains(self):
        for fd, name, expected in self.chains:
            info = self.stat_at(fd, name)
            if info is None or identity(info) != expected:
                raise Failure('verify-failed')

    def hash_fd(self, fd, reason):
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size != self.size:
            raise Failure(reason)
        os.lseek(fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        remaining = self.size
        while remaining:
            self.checkpoint('hash-before')
            block = os.read(fd, min(CHUNK_SIZE, remaining))
            self.checkpoint('hash-after')
            if not block:
                raise Failure(reason)
            digest.update(block)
            remaining -= len(block)
        self.checkpoint('hash-tail-before')
        extra = os.read(fd, 1)
        self.checkpoint('hash-tail-after')
        after = os.fstat(fd)
        if extra or fingerprint(before) != fingerprint(after) or digest.hexdigest() != self.digest:
            raise Failure(reason)
        if stat.S_IMODE(after.st_mode) != self.mode:
            raise Failure(reason)

    def read_source(self):
        fd, name = self.open_parent(self.options['source'], 'taskmd-body')
        try:
            self.source_fd = self.keep_fd(os.open(name, os.O_RDONLY | os.O_NOFOLLOW |
                                                 os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd))
            self.source_info = os.fstat(self.source_fd)
            if (not stat.S_ISREG(self.source_info.st_mode) or self.source_info.st_size < 0
                    or stat.S_IMODE(self.source_info.st_mode) != self.mode):
                raise Failure('taskmd-body')
            self.size = self.source_info.st_size
            self.hash_fd(self.source_fd, 'taskmd-body')
        except OSError:
            raise Failure('taskmd-body')

    def read_destination(self):
        self.parent_fd, self.destination_name = self.open_parent(self.options['destination'], 'dest-symlink')
        self.destination_info = self.stat_at(self.parent_fd, self.destination_name)
        if self.destination_info is None:
            return
        mode = self.destination_info.st_mode
        if stat.S_ISLNK(mode):
            raise Failure('dest-symlink')
        if stat.S_ISDIR(mode):
            raise Failure('dest-dir')
        if not stat.S_ISREG(mode):
            raise Failure('dest-special')

    def create_file(self, name):
        fd = self.keep_fd(os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL |
                                 os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.temp_fd))
        self.entries[name] = identity(os.fstat(fd))
        return fd

    def prepare_directory(self):
        self.checkpoint('before-prepare')
        self.temp_name = '.restore-taskmd-' + secrets.token_hex(16)
        self.temp_path = os.path.join(os.path.dirname(self.options['destination']), self.temp_name)
        if len(path_field(self.temp_path)) + 256 > MAX_RESPONSE:
            raise Failure('internal-argument')
        try:
            os.mkdir(self.temp_name, 0o700, dir_fd=self.parent_fd)
            self.temp = 'unknown'
            self.temp_identity = identity(os.stat(self.temp_name, dir_fd=self.parent_fd, follow_symlinks=False))
            self.temp_fd = self.keep_fd(os.open(self.temp_name, os.O_RDONLY | os.O_DIRECTORY |
                                               os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.parent_fd))
            info = os.fstat(self.temp_fd)
            if identity(info) != self.temp_identity or stat.S_IMODE(info.st_mode) != 0o700:
                raise Failure('prepare-failed')
            self.temp = 'retained'
            self.prepared_fd = self.create_file('prepared')
        except OSError:
            raise Failure('prepare-failed')

    def copy_file(self, source, destination, size, fd_root):
        def limit():
            resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
        self.checkpoint('before-copy')
        try:
            self.child = subprocess.Popen([self.cp, '-aL', '--', '%s/%d' % (fd_root, source),
                                           '%s/%d' % (fd_root, destination)],
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL, close_fds=True,
                                          pass_fds=(source, destination), preexec_fn=limit)
        except (OSError, ValueError, OverflowError, subprocess.SubprocessError):
            raise Failure('runtime-unavailable')
        try:
            while self.child.poll() is None:
                self.checkpoint('copy')
                if os.fstat(destination).st_size > size:
                    raise Failure('copy-failed')
                time.sleep(POLL_SECONDS)
            rc = self.child.wait()
            self.child = None
            self.checkpoint('after-copy')
            if rc != 0 or os.fstat(destination).st_size != size:
                raise Failure('copy-failed')
        finally:
            if self.child is not None and not self.stop_child():
                self.reason = 'child-unreaped'
        if self.child_unreaped:
            raise Failure('child-unreaped')

    def aliases_match(self, fd_root, *fds):
        for fd in fds:
            info = os.stat('%s/%d' % (fd_root, fd))
            if not stat.S_ISREG(info.st_mode) or identity(info) != identity(os.fstat(fd)):
                return False
        return True

    def probe(self):
        try:
            os.mkdir('probe-directory', 0o700, dir_fd=self.temp_fd)
            probe_dir = self.keep_fd(os.open('probe-directory', os.O_RDONLY | os.O_DIRECTORY |
                                             os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.temp_fd))
            self.entries['probe-directory'] = identity(os.fstat(probe_dir))
            self.remove_entry('probe-directory')
            source = self.create_file('probe-source')
            destination = self.create_file('probe-destination')
            payload = b'fd-probe\n'
            if os.write(source, payload) != len(payload):
                raise Failure('runtime-unavailable')
            os.fchmod(source, 0o640)
            for fd_root in FD_ROOTS:
                self.checkpoint('probe')
                try:
                    if not self.aliases_match(fd_root, self.source_fd, self.prepared_fd, source, destination):
                        continue
                    os.ftruncate(destination, 0)
                    self.copy_file(source, destination, len(payload), fd_root)
                    if self.child_unreaped:
                        raise Failure('child-unreaped')
                    os.lseek(destination, 0, os.SEEK_SET)
                    info = os.fstat(destination)
                    if os.read(destination, len(payload) + 1) != payload or stat.S_IMODE(info.st_mode) != 0o640:
                        continue
                    if identity(info) != self.entries['probe-destination']:
                        continue
                    os.replace('probe-destination', 'probe-renamed', src_dir_fd=self.temp_fd, dst_dir_fd=self.temp_fd)
                    self.entries['probe-renamed'] = self.entries.pop('probe-destination')
                    if identity(os.stat('probe-renamed', dir_fd=self.temp_fd, follow_symlinks=False)) != identity(info):
                        raise Failure('runtime-unavailable')
                    self.remove_entry('probe-renamed')
                    self.remove_entry('probe-source')
                    self.fd_root = fd_root
                    return
                except (OSError, ValueError, NotImplementedError):
                    continue
                except Failure as error:
                    if error.reason in ('interrupted', 'budget-exceeded', 'child-unreaped') or self.child_unreaped:
                        raise
                    continue
        except (OSError, ValueError, NotImplementedError):
            raise Failure('runtime-unavailable')
        raise Failure('runtime-unavailable')

    def check_prepared_name(self):
        info = self.stat_at(self.temp_fd, 'prepared')
        if info is None or identity(info) != identity(os.fstat(self.prepared_fd)):
            raise Failure('verify-failed')
        info = self.stat_at(self.parent_fd, self.temp_name)
        if info is None or identity(info) != self.temp_identity:
            raise Failure('verify-failed')

    def before_publish(self):
        self.checkpoint('before-publish')
        self.check_chains()
        self.check_prepared_name()
        if fingerprint(os.fstat(self.source_fd)) != fingerprint(self.source_info):
            raise Failure('taskmd-body')
        now = self.stat_at(self.parent_fd, self.destination_name)
        if (now is None) != (self.destination_info is None):
            raise Failure('verify-failed')
        if now is not None and fingerprint(now) != fingerprint(self.destination_info):
            raise Failure('verify-failed')
        self.checkpoint('publish-ready')

    def publish(self):
        self.before_publish()
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
        try:
            self.checkpoint('replace')
            # syscall が返らない/予期しない例外のときは無変更とは断言しない。
            self.touched = 'unknown'
            try:
                os.replace('prepared', self.destination_name,
                           src_dir_fd=self.temp_fd, dst_dir_fd=self.parent_fd)
            except OSError:
                self.touched = 'none'
                raise Failure('replace-failed')
            self.touched = 'copied' if self.destination_info is None else 'replaced'
            self.entries.pop('prepared')
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        self.checkpoint('after-publish')

    def verify_published(self):
        self.check_chains()
        info = self.stat_at(self.parent_fd, self.destination_name)
        if info is None or identity(info) != identity(os.fstat(self.prepared_fd)):
            raise Failure('verify-failed')
        self.hash_fd(self.prepared_fd, 'verify-failed')
        info = self.stat_at(self.parent_fd, self.destination_name)
        if info is None or identity(info) != identity(os.fstat(self.prepared_fd)):
            raise Failure('verify-failed')
        self.checkpoint('after-verify')

    def remove_entry(self, name):
        info = self.stat_at(self.temp_fd, name)
        if info is None or identity(info) != self.entries[name]:
            raise Failure('cleanup-failed')
        if stat.S_ISDIR(info.st_mode):
            os.rmdir(name, dir_fd=self.temp_fd)
        else:
            os.unlink(name, dir_fd=self.temp_fd)
        del self.entries[name]

    def cleanup(self):
        if self.temp == 'none':
            return
        end = time.monotonic() + CLEANUP_SECONDS
        try:
            self.temp = 'unknown'
            self.check_chains()
            info = self.stat_at(self.parent_fd, self.temp_name)
            if info is None or self.temp_identity is None or identity(info) != self.temp_identity:
                self.temp = 'unknown'
                raise Failure('cleanup-failed')
            self.temp = 'retained'
            if self.child_unreaped:
                return
            if self.temp_fd is None:
                raise Failure('cleanup-failed')
            for name in list(self.entries):
                if time.monotonic() >= end:
                    raise Failure('cleanup-failed')
                self.remove_entry(name)
            if time.monotonic() >= end:
                raise Failure('cleanup-failed')
            # 親の名前と保持 fd の両方がまだ自分の専用領域であることを確認する。
            info = self.stat_at(self.parent_fd, self.temp_name)
            if info is None or identity(info) != self.temp_identity:
                self.temp = 'unknown'
                raise Failure('cleanup-failed')
            os.rmdir(self.temp_name, dir_fd=self.parent_fd)
            self.temp = 'removed'
            if time.monotonic() >= end:
                raise Failure('cleanup-failed')
        except Failure:
            raise
        except (OSError, ValueError, NotImplementedError):
            raise Failure('cleanup-failed')

    def response(self):
        stopped = self.response_stopped if self.response_finalized else self.stopped
        if stopped is not None:
            self.reason = 'interrupted'
        elif self.child_unreaped:
            self.reason = 'child-unreaped'
        rc = 20 if stopped is not None else 33 if self.reason else 0
        result = ('RESTORED=%s\nTOUCHED=%s\nTEMP=%s\n' %
                  ('no' if rc else 'yes', self.touched, self.temp)).encode('ascii')
        if self.reason:
            result += ('REASON=%s\n' % self.reason).encode('ascii')
        if self.temp == 'retained':
            result += b'TEMP_PATH=' + path_field(self.temp_path) + b'\n'
        return rc, result

    def run(self):
        try:
            for number in SIGNALS:
                self.handlers[number] = signal.signal(number, self.on_signal)
            self.checkpoint('start')
            self.check_features()
            self.read_source()
            self.read_destination()
            self.prepare_directory()
            self.probe()
            self.copy_file(self.source_fd, self.prepared_fd, self.size, self.fd_root)
            if fingerprint(os.fstat(self.source_fd)) != fingerprint(self.source_info):
                raise Failure('taskmd-body')
            self.hash_fd(self.prepared_fd, 'verify-failed')
            self.publish()
            self.verify_published()
        except Failure as error:
            self.reason = error.reason
        except (OSError, ValueError, OverflowError, NotImplementedError, subprocess.SubprocessError):
            self.reason = 'runtime-unavailable' if self.temp == 'none' else 'verify-failed'
        except Exception:
            # 内容・パス・traceback を機械応答へ混入させない。
            self.reason = 'runtime-unavailable' if self.temp == 'none' else 'verify-failed'
        finally:
            if self.child is not None:
                self.stop_child()
            try:
                self.checkpoint('before-cleanup')
            except Failure as error:
                if self.reason is None:
                    self.reason = error.reason
            try:
                self.cleanup()
            except Failure:
                if self.reason is None:
                    self.reason = 'cleanup-failed'
            for fd in reversed(self.fds):
                try:
                    os.close(fd)
                except OSError:
                    if self.reason is None:
                        self.reason = 'cleanup-failed'
            try:
                self.checkpoint('before-response')
            except Failure as error:
                if self.reason is None:
                    self.reason = error.reason
        return self.response()


def emit_result(operation, rc, result):
    # mask の設定成功を応答確定点にする。設定できない場合は有効な応答を
    # 出さず、caller の結果不明停止へ倒す。送出待ちは外側の timeout が止める。
    if (not callable(getattr(signal, 'pthread_sigmask', None)) or
            not all(hasattr(signal, name) for name in ('SIG_BLOCK', 'SIG_SETMASK'))):
        return 33
    try:
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
    except Exception:
        return 33
    try:
        if operation is not None:
            operation.response_stopped = operation.stopped
            operation.response_finalized = True
            rc, result = operation.response()
        try:
            if sys.stdout.buffer.write(result) != len(result):
                return 33
            sys.stdout.buffer.flush()
        except (OSError, ValueError):
            return 33
        return rc
    finally:
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        except Exception:
            # 送出済みの応答と終了値を維持する。再報告も traceback も出さない。
            pass


def main(argv=None):
    operation = None
    try:
        options = parse_arguments(sys.argv[1:] if argv is None else argv)
    except Failure:
        result = b'RESTORED=no\nTOUCHED=none\nTEMP=none\nREASON=internal-argument\n'
        rc = 33
    else:
        operation = Restore(options)
        rc, result = operation.run()
        try:
            operation.checkpoint('before-output')
        except Failure as error:
            if operation.reason is None:
                operation.reason = error.reason
    return emit_result(operation, rc, result)


if __name__ == '__main__':
    sys.exit(main())
