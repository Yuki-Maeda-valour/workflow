#!/usr/bin/env python3
"""R4Q(#494)の比較器の偽の監督。本物の監督(旧版も Rust も)が書かない記録と通知を、決めたバイト列で書く。

- 比較器が要求の監督を明示の形(R4Q_PYTHON と ["-B", "<…>/native/ci/r4q_fake_monitor.py"])にして、
  2 つの親(旧版・Rust)から起動させる。本物と同じ argv(--linux … か --anchor-fd …)を受ける。
- 決めたバイト列と終わり方は、環境変数 R4Q_FAKE_SPEC(要求の env に入れる)が指す JSON のファイルで受ける:
  {"data": "<16 進>", "record": "file"|"none"|"fifo"|"symlink", "end": "exit"|"pause"|"child-pause"|"silent-exit",
   "code": <終了値>, "child_pid": "<子の pid を書くファイル>"}
  data の中の `{nonce}` のバイト列は、argv の --nonce の値(surrogateescape で符号化)に置き換える。
- Linux の方式(--linux): data を --result へ書いて code で終わる(record が none なら書かない。fifo・symlink は
  その種類のファイルを記録の場所に作る。symlink のリンク先に data を書く)。
- POSIX の方式(--anchor-fd): 本物と同じく TERM・INT・HUP を何もしない処理にする。
  - pause: data を anchor の fd へ書き、KILL まで pause を続ける。
  - child-pause: 先に群の中に子を 1 つ作り(子も TERM・INT・HUP では終わらず、KILL まで眠る。pid を child_pid
    のファイルへ書く)、data を書いて pause を続ける。
  - exit: data を書いた後に code で終わる。
  - silent-exit: 何も書かずに code で終わる(終了通知の前に失われた anchor)。
- 比較器の Python と同じく -B で動かし、__pycache__ を作らない。
"""
import json
import os
import signal
import sys

sys.dont_write_bytecode = True


def argument(argv, name):
    if name in argv:
        return argv[argv.index(name) + 1]
    for item in argv:
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return None


def nothing(*_):
    return None


def main():
    argv = sys.argv[1:]
    with open(os.environ["R4Q_FAKE_SPEC"], encoding="utf-8") as stream:
        spec = json.load(stream)
    nonce = argument(argv, "--nonce") or ""
    data = bytes.fromhex(spec.get("data", "")).replace(b"{nonce}", nonce.encode("utf-8", "surrogateescape"))
    code = spec.get("code", 0)
    if "--linux" in argv:
        path = argument(argv, "--result")
        kind = spec.get("record", "file")
        if kind == "file":
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        elif kind == "fifo":
            os.mkfifo(path, 0o600)
        elif kind == "symlink":
            target = path + ".target"
            with open(target, "wb") as stream:
                stream.write(data)
            os.symlink(target, path)
        return code
    fd = int(argument(argv, "--anchor-fd"))
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, nothing)
    end = spec.get("end", "pause")
    if end == "silent-exit":
        return code
    if end == "child-pause":
        child = os.fork()
        if child == 0:
            os.close(fd)
            while True:
                signal.pause()
        with open(spec["child_pid"], "w", encoding="ascii") as stream:
            stream.write(f"{child}\n")
    os.write(fd, data)
    if end == "exit":
        return code
    while True:
        signal.pause()


sys.exit(main())
