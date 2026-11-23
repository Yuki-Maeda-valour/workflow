#!/usr/bin/env python3
"""S3I-A: coreutils 9.4 の 2 つの引用(quote = locale 形・quoteaf = shell-escape-always 形)を、基準の容器で
GNU ls の --quoting-style から機械的に測る。使い方: measure_quoting.py <空の作業フォルダ> > quoting.tsv
欄: 名前の16進 TAB locale 形の16進 TAB shell-escape-always 形の16進。LC_ALL=C で流す。"""
import os
import subprocess
import sys


def names():
    single = [bytes([b]) for b in range(1, 256) if b not in (ord('/'), ord('.'))]
    combos = [b"a'b", b"it's", b"a b", b'a"b', b'a\\b', b"'", b"''", b'\xe6\x97\xa5', b'a\n', b'\x01a', b'a\x01b',
              b"x'\x01", b"\x01'x", b'#a', b'a#', b'~a', b'a~', b'{', b'{}', b'}', b'a$b', b'a!b', b'-a', b'.a',
              b'a\tb\nc', b"a'b c", b"a'b!", b'\x7f', b'\xff\xfe', b'guard-a b', b'owner-x', b"a'\xff"]
    return single + combos


def main(work):
    out = []
    env = {'PATH': os.environ.get('PATH', ''), 'LC_ALL': 'C'}
    for name in names():
        path = os.path.join(os.fsencode(work), name)
        with open(path, 'wb'):
            pass
        row = [name.hex()]
        for style in ('locale', 'shell-escape-always'):
            done = subprocess.run(['ls', '-d', f'--quoting-style={style}', '--', name], cwd=work, env=env,
                                  capture_output=True, check=True)
            row.append(done.stdout.rstrip(b'\n').hex())
        os.unlink(path)
        out.append('\t'.join(row))
    sys.stdout.write('\n'.join(out) + '\n')


if __name__ == '__main__':
    main(sys.argv[1])
