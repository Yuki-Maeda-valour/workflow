#!/usr/bin/env python3
"""無人ループの子の出力(result・permlog・origin-json)の解析。親は信頼コピーの本文を保持して -c で動かす。"""
import json
import os
import re
import stat
import sys

# unattended-mode.md §2 の 5 値のパターン(両モード共通。モードで取りえない値は判定で失敗にする — loop.md §5)
OUTCOME = re.compile(r"^無人の周の結果: (PR|縮退|保留|失敗扱い|候補なし) — (.*)$")


def one_line(text, limit=500):
    return re.sub(r"[\x00-\x1f\x7f]", " ", text)[:limit]


def cmd_result(path):
    raw = open(path, "rb").read().decode("utf-8", "replace")
    try:
        obj = json.loads(raw)
    except ValueError:
        print("json=bad")
        return
    # --output-format json の出力は、結果の 1 つのオブジェクトか、全メッセージの配列(利用者の設定の verbose が
    # 効いたとき。最後の要素が type: "result")。配列なら最後の type: "result" の要素を結果として読む。
    # 拒否の欄は、配列ならすべての type: "result" の要素から順につなぐ(周の中でバックグラウンドに委託すると、
    # result の要素がターンごとに出て、拒否はそのターンの要素にだけ載り、最後の要素の欄は空になる — 実測)
    results = [obj]
    if isinstance(obj, list):
        results = [e for e in obj if isinstance(e, dict) and e.get("type") == "result"]
        obj = results[-1] if results else None
    if not isinstance(obj, dict):
        print("json=bad")
        return
    print("json=ok")
    text = obj.get("result") if isinstance(obj.get("result"), str) else ""
    last = None
    for line in text.split("\n"):
        m = OUTCOME.match(line.rstrip("\r"))
        if m:
            last = m
    if last:
        print("outcome=" + last.group(1))
        print("detail=" + one_line(last.group(2)))
    # 拒否の鍵("denial" を含む鍵)ごとに、値を要素の順に集める。鍵を持つ要素が 1 つならその値のまま(オブジェクトの
    # ときと同じ形)。複数なら、どれもリストのときはつないだリスト、そうでなければ値を順に並べたリストにする
    collected = {}
    for e in results:
        for k, v in e.items():
            if "denial" in str(k).lower():
                collected.setdefault(k, []).append(v)
    denials = {}
    for k, vs in collected.items():
        if len(vs) == 1:
            denials[k] = vs[0]
        elif all(isinstance(v, list) for v in vs):
            denials[k] = [x for v in vs for x in v]
        else:
            denials[k] = vs
    if denials:
        print("denials=" + one_line(json.dumps(denials, ensure_ascii=False), 2000))


def cmd_permlog(path):
    # 子が書ける参考表示。内容・欠落・エラーのどれも制御判断に使わない。
    allow = deny = 0
    kinds, lines = {}, []
    issue = ''
    fds = []
    try:
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY); fds.append(fd)
        parts = os.path.abspath(path).split('/')[1:]
        for name in parts[:-1]:
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd); fds.append(fd)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd); fds.append(fd)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > 1048576:
            raise ValueError('通常ファイルでないか読取上限を超える')
        raw = b''
        while len(raw) < st.st_size:
            chunk = os.read(fd, min(65536, st.st_size - len(raw)))
            if not chunk: raise ValueError('読取中に切り詰められた')
            raw += chunk
        after = os.fstat(fd)
        if os.read(fd, 1) or (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns):
            raise ValueError('読取中に変わった')
        if raw and not raw.endswith(b'\n'):
            raise ValueError('末尾の行が途中で切れている')
        records = raw.decode('utf-8').splitlines()
        if len(records) > 10000: raise ValueError('行数上限を超える')
        for raw_line in records:
            e = json.loads(raw_line)
            if not isinstance(e, dict) or e.get('decision') not in ('allow', 'deny'):
                raise ValueError('行の形式が違う')
            if e['decision'] == 'allow':
                allow += 1
            else:
                deny += 1
                kind = e.get('kind', 'other')
                if not isinstance(kind, str): raise ValueError('kind が文字列でない')
                kind = one_line(kind, 100)
                kinds[kind] = kinds.get(kind, 0) + 1
                if len(lines) < 100:
                    lines.append(one_line(f"{e.get('tool_name')} [{kind}] {e.get('reason')} — {e.get('subject')}", 600))
    except (OSError, ValueError, UnicodeError) as exc:
        issue = one_line(str(exc), 300)
    finally:
        for fd in reversed(fds): os.close(fd)
    print(f'allow={allow}')
    print(f'deny={deny}')
    print('kinds=' + ','.join(f'{k}:{v}' for k,v in sorted(kinds.items())))
    if issue: print('reference-error=' + issue)
    for line in lines: print('line=' + line)


def cmd_origin_json():
    # origin-repo.py の出力(discover-mode.md §3)を読む。欄が欠けるか型が違えば 1(URL の字面は出力に無い)
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except ValueError:
        sys.exit(1)
    if not isinstance(data, dict) or not all(isinstance(data.get(k), bool) for k in ("origin", "same", "vcs")):
        sys.exit(1)
    reason = data.get("reason")
    repo = data.get("repo")
    if repo is not None and not isinstance(repo, str):
        sys.exit(1)
    for key in ("origin", "same", "vcs"):
        print(f"{key}={1 if data[key] else 0}")
    print("reason=" + one_line(reason if isinstance(reason, str) else "", 300))
    print("repo=" + (repo or ""))


COMMANDS = {
    "result": cmd_result, "permlog": cmd_permlog, "origin-json": cmd_origin_json,
}
try:
    COMMANDS[sys.argv[1]](*sys.argv[2:])
except SystemExit:
    raise
except Exception as exc:  # 想定外の失敗は 9(呼び出し側は「差分あり」や成功に読み替えない)
    print(f"補助の失敗: {exc!r}", file=sys.stderr)
    sys.exit(9)
