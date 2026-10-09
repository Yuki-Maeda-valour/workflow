"""診断の完成応答だけを書く内部worker。公開CLIではない。"""
import os
import select
import sys

def write_all(fd,raw):
    view=memoryview(raw)
    while view:
        try:
            n=os.write(fd,view)
            if n<=0: return False
            view=view[n:]
        except InterruptedError: continue
        except BlockingIOError:
            try: select.select([], [fd], [], .05)
            except InterruptedError: pass
    return True

def main():
    if len(sys.argv)!=2 or sys.argv[1] not in ('stdout','stderr'): return 20
    fd=1 if sys.argv[1]=='stdout' else 2
    try:
        while True:
            try: raw=os.read(0,65536)
            except InterruptedError: continue
            if not raw: return 0
            if not write_all(fd,raw): return 20
    except BaseException: return 20

if __name__=='__main__':
    raise SystemExit(main())
