"""R4G比較専用の包み: 旧`pending-implementation.py`へ、Rustの検査専用の注入(`--r4g-pending-fault`)と
同じ故障・記録・信号・わざと候補を開く対照を与え、`runpy`で旧版を`__main__`として動かす。

形: python3 pending_wrapper.py <指定のJSON> <旧スクリプト> [旧スクリプトの引数]...

指定のJSONは Rust の入口と同じ形に、時計の値の並び`clock`(Rust は`--r4g-clock`)を足したもの。
差し替えるのは`os`の関数と`time.monotonic`と`builtins.any`で、旧スクリプトのコードから直接呼んだ
ときだけ数え・記録し・故障を当てる(標準ライブラリや`runpy`の中の呼出しには当てない)。
製品の起動の形の代わりではない。故障の無い比較は`python3 <旧スクリプト>`で起動する。
"""
import builtins
import json
import os
import runpy
import sys
import time


def main():
    spec = json.loads(sys.argv[1])
    script = sys.argv[2]
    args = sys.argv[3:]
    known = {'trace', 'hits', 'rules', 'signal_at_clock', 'overlap_unexpected', 'open_candidates', 'clock'}
    if set(spec) - known:
        raise SystemExit('pending_wrapper: 知らない指定: ' + ', '.join(sorted(set(spec) - known)))
    ops = {'stat', 'fstat', 'open', 'close', 'read', 'readlink', 'scandir', 'getcwd', 'clock'}
    if any(rule.get('op') not in ops for rule in spec.get('rules', [])):
        raise SystemExit('pending_wrapper: 知らない呼出しの名前')
    trace = spec.get('trace')
    rules = [dict(rule, count=0, applied=0) for rule in spec.get('rules', [])]
    if spec.get('hits'):
        import atexit

        def write_hits():
            with open(spec['hits'], 'w', encoding='ascii') as handle:
                handle.write(json.dumps([rule['applied'] for rule in rules], separators=(',', ':')) + '\n')
        atexit.register(write_hits)
    original = {name: getattr(os, name) for name in ('stat', 'fstat', 'open', 'close', 'read', 'readlink',
                                                      'scandir', 'getcwdb')}
    original_monotonic = time.monotonic
    original_any = builtins.any
    clock_values = spec.get('clock')
    clock_calls = [0]

    def from_script():
        # 呼出元のフレームが旧スクリプトのコードのときだけ当てる。
        return sys._getframe(2).f_code.co_filename == script

    def log(entry):
        if trace:
            with open(trace, 'a', encoding='ascii') as handle:
                handle.write(json.dumps(entry, separators=(',', ':')) + '\n')

    def hit(op, dir_fd=None, path=None, ident=None):
        chosen = None
        for rule in rules:
            if rule['op'] != op:
                continue
            if 'path_hex' in rule and (path is None or path.hex() != rule['path_hex']):
                continue
            if 'last_hex' in rule and (path is None or path.split(b'/')[-1].hex() != rule['last_hex']):
                continue
            if 'dir' in rule and rule['dir'] != (dir_fd is not None):
                continue
            if 'ident' in rule and list(ident or ()) != rule['ident']:
                continue
            rule['count'] += 1
            if 'calls' in rule:
                due = rule['count'] in rule['calls']
            elif 'from' in rule:
                due = rule['count'] >= rule['from']
            else:
                due = True
            if due and chosen is None:
                rule['applied'] += 1
                chosen = rule
        return chosen

    def raise_for(rule):
        if rule is None:
            return
        if 'errno' in rule:
            raise OSError(rule['errno'], os.strerror(rule['errno']))
        if 'unexpected' in rule:
            raise getattr(builtins, rule['unexpected'])('fixture')

    class Changed:
        pass

    def apply(rule, info):
        if rule is None or 'change' not in rule:
            return info
        change = rule['change']
        fake = Changed()
        for name in dir(info):
            if name.startswith('st_'):
                setattr(fake, name, getattr(info, name))
        for key in ('ino', 'uid', 'size', 'mtime_ns', 'ctime_ns'):
            setattr(fake, 'st_' + key, getattr(info, 'st_' + key) + change.get(key, 0))
        fake.st_mode = info.st_mode ^ change.get('mode_xor', 0)
        return fake

    def stat_record(info):
        return ['ok', [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_size,
                       info.st_mtime_ns, info.st_ctime_ns]]

    def failure(exc):
        if isinstance(exc, OSError) and exc.errno is not None:
            return ['err', exc.errno]
        return ['exc', type(exc).__name__]

    def open_candidate(dir_fd, name):
        mode = spec.get('open_candidates')
        flags = os.O_RDONLY | os.O_NONBLOCK
        try:
            if mode == 'relative':
                fd = original['open'](name, flags, dir_fd=dir_fd)
            elif mode == 'absolute':
                base = original['readlink']('/proc/self/fd/%d' % dir_fd)
                fd = original['open'](os.fsencode(base) + b'/' + name, flags)
            else:
                return
            original['close'](fd)
        except OSError:
            pass

    def stat(path, *, dir_fd=None, follow_symlinks=True):
        if not from_script():
            return original['stat'](path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        raw = os.fsencode(path)
        if dir_fd is not None and raw.startswith(b'guard-'):
            open_candidate(dir_fd, raw)
        rule = hit('stat', dir_fd, raw)
        try:
            raise_for(rule)
            info = apply(rule, original['stat'](path, dir_fd=dir_fd, follow_symlinks=follow_symlinks))
        except Exception as exc:
            log(['stat', dir_fd, raw.hex(), failure(exc)])
            raise
        log(['stat', dir_fd, raw.hex(), stat_record(info)])
        return info

    def fstat(fd):
        if not from_script():
            return original['fstat'](fd)
        try:
            info = original['fstat'](fd)
            rule = hit('fstat', ident=(info.st_dev, info.st_ino))
            raise_for(rule)
            info = apply(rule, info)
        except Exception as exc:
            log(['fstat', fd, failure(exc)])
            raise
        log(['fstat', fd, stat_record(info)])
        return info

    def open_(path, flags, mode=0o777, *, dir_fd=None):
        if not from_script():
            return original['open'](path, flags, mode, dir_fd=dir_fd)
        raw = os.fsencode(path)
        rule = hit('open', dir_fd, raw)
        try:
            raise_for(rule)
            fd = original['open'](path, flags, mode, dir_fd=dir_fd)
        except Exception as exc:
            log(['open', dir_fd, raw.hex(), flags, failure(exc)])
            raise
        log(['open', dir_fd, raw.hex(), flags, ['ok', fd]])
        return fd

    def close(fd):
        if not from_script():
            return original['close'](fd)
        rule = hit('close')
        try:
            raise_for(rule)
            original['close'](fd)
        except Exception as exc:
            log(['close', fd, failure(exc)])
            raise
        log(['close', fd, ['ok', None]])

    def read(fd, length):
        if not from_script():
            return original['read'](fd, length)
        rule = hit('read')
        try:
            raise_for(rule)
            data = original['read'](fd, length)
        except Exception as exc:
            log(['read', fd, length, failure(exc)])
            raise
        log(['read', fd, length, ['ok', len(data)]])
        return data

    def readlink(path, *, dir_fd=None):
        if not from_script():
            return original['readlink'](path, dir_fd=dir_fd)
        raw = os.fsencode(path)
        rule = hit('readlink', None, raw)
        try:
            raise_for(rule)
            target = original['readlink'](path, dir_fd=dir_fd)
        except Exception as exc:
            log(['readlink', raw.hex(), failure(exc)])
            raise
        log(['readlink', raw.hex(), ['ok', os.fsencode(target).hex()]])
        return target

    def scandir(path='.'):
        if not from_script():
            return original['scandir'](path)
        rule = hit('scandir')
        try:
            raise_for(rule)
            entries = original['scandir'](path)
        except Exception as exc:
            log(['scandir', path, failure(exc)])
            raise
        log(['scandir', path, ['ok', None]])
        return entries

    def getcwdb():
        if not from_script():
            return original['getcwdb']()
        rule = hit('getcwd')
        try:
            raise_for(rule)
            cwd = original['getcwdb']()
        except Exception as exc:
            log(['getcwd', failure(exc)])
            raise
        log(['getcwd', ['ok', cwd.hex()]])
        return cwd

    def monotonic():
        if not from_script():
            return original_monotonic()
        clock_calls[0] += 1
        log(['clock'])
        planned = spec.get('signal_at_clock')
        if planned and planned['call'] == clock_calls[0]:
            os.kill(os.getpid(), planned['signal'])
        raise_for(hit('clock'))
        if clock_values is None:
            return original_monotonic()
        return float(clock_values[min(clock_calls[0], len(clock_values)) - 1])

    def exploding_any(iterable):
        # decide_outcome の any(...) だけ: 最初の要素を評価した所で例外にする(要素が無ければ例外にしない)。
        if spec.get('overlap_unexpected') and sys._getframe(1).f_code.co_name == 'decide_outcome':
            for _ in iterable:
                raise getattr(builtins, spec['overlap_unexpected'])('fixture decision failure')
            return False
        return original_any(iterable)

    replacements = {'stat': stat, 'fstat': fstat, 'open': open_, 'close': close, 'read': read,
                    'readlink': readlink, 'scandir': scandir, 'getcwdb': getcwdb}
    # 旧版の check_features は機能の表(supports_*)を関数の同一性で見るので、差し替えも表へ足す。
    for support in ('supports_fd', 'supports_dir_fd', 'supports_follow_symlinks'):
        table = getattr(os, support)
        added = {replacements[name] for name in replacements if original[name] in table}
        setattr(os, support, table | added)
    for name, function in replacements.items():
        setattr(os, name, function)
    time.monotonic = monotonic
    builtins.any = exploding_any
    sys.argv = [script] + args
    runpy.run_path(script, run_name='__main__')


if __name__ == '__main__':
    main()
