"""文字コード設定だけによる差のUTF-8固定(2026-11-11 22:24)。印・参照の環境・実効の値。"""
import dataclasses
import json
from pathlib import Path
import re
import subprocess

from .compare import Result, report_time

SETTINGS = ('PYTHONIOENCODING', 'PYTHONUTF8', 'PYTHONCOERCECLOCALE')
_FIXED = json.loads(Path(__file__).with_name('utf8_fixed.json').read_text())
MARKED = frozenset(_FIXED['ids'])
PROBE = ("import json,sys;print(json.dumps([sys.flags.utf8_mode,sys.stdout.encoding,sys.stdout.errors,"
         "sys.stderr.encoding,sys.stderr.errors,sys.getfilesystemencoding(),sys.getfilesystemencodeerrors()]))")
EXPECTED_EFFECTIVE = [1, 'utf-8', 'surrogateescape', 'utf-8', 'backslashreplace', 'utf-8', 'surrogateescape']
_checked = {}


def has_settings(env):
    return any(key in env for key in SETTINGS)


def reference_env(env):
    """文字コードの3つの設定だけを参照値へ替える。LC_*などは替えない。"""
    result = {k: v for k, v in env.items() if k not in _FIXED['reference']['remove']}
    result.update(_FIXED['reference']['set'])
    return result


def check_difference(original, reference):
    changed = {k for k in set(original) | set(reference) if original.get(k) != reference.get(k)}
    if not changed or not changed <= set(SETTINGS):
        raise AssertionError('参照の環境が文字コードの3つの設定のほかも替えています')


def check_effective(env, *, python='python3', runner=subprocess.run):
    """同じpython3・同じ環境・同じfdで、参照の環境の実効の値を確かめる。"""
    key = tuple(sorted(env.items()))
    if key not in _checked:
        done = runner([python, '-c', PROBE], env=env, stdin=subprocess.DEVNULL,
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        actual = json.loads(done.stdout) if done.returncode == 0 and not done.stderr else None
        _checked[key] = actual == EXPECTED_EFFECTIVE
    if not _checked[key]:
        raise AssertionError('参照の環境の実効の値がUTF-8固定になっていません')


def _thread(value):
    if value is None:
        return None
    return re.sub(rb'Current thread 0x[0-9a-f]{16} \(most recent call first\)',
                  b'Current thread <runtime-thread> (most recent call first)', value)


def comparable(result):
    report = report_time(result.report, result.started, result.ended) if result.report is not None else None
    return (result.returncode, result.stdout, _thread(result.stderr), report, result.patch)


def expectation(case_id, original, reference, *, marked=None):
    """元と参照の旧版の結果から、印の正しさを確かめて期待を選ぶ。"""
    marked = case_id in MARKED if marked is None else marked
    differs = comparable(original) != comparable(reference)
    if marked and not differs:
        raise AssertionError('UTF-8固定の印の誤りです(元と参照の結果が同じ)')
    if differs and not marked:
        raise AssertionError('UTF-8固定の印の抜けです(元と参照の結果が違う)')
    return reference if marked else original


def same_with_thread(expected, actual, same_result):
    """Current threadの欄だけを位置と形式で対応づけて、残りの全バイトを比べる。"""
    same_result(dataclasses.replace(expected, stderr=_thread(expected.stderr)),
                dataclasses.replace(actual, stderr=_thread(actual.stderr)))
