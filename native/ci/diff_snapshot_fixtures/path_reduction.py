"""PATHにgitとgrepだけを置いてRustを動かし、旧版と同じ結果になることを確かめる(23:19)。

起動先だけを、PATHを縮めてからRustをexecするshのラッパーへ替える。shは信号の扱いとfdを変えない。
子の環境のPATHと`_`は比べない。gitとgrep以外の道具が要るケース(LFS・textconv・filter、
道具のスタブを置くケース)は、manifestの対象外の理由とともに外す。
"""
import os
from pathlib import Path
import shutil

from .compare import NotRun, exact_ids

# 固定入力の群ごとに、縮めたPATHで流すケース。型検査だけの群は外部のRust実行が無い。
SELECTED = {
    'args': ['args-normal', 'args-help-long', 'args-value-missing-cwd'],
    'ere': ['ere-glob-01-01', 'ere-ere-03-08', 'ere-reject-09'],
    'profile': ['profile-current-regular', 'profile-ref-100644', 'profile-current-fifo'],
    'review': ['review-sparse-control', 'review-output-path-dotdot', 'review-fail-mkdir-c',
               'review-max-bytes-overflow', 'review-info-exclude-nul'],
    'partial': ['partial-print-stdout-5', 'partial-usage-stderr-10'],
}
EXCLUDED = {
    'display': '型検査だけ(製品の実行物を起動しない)',
    'publish': '型検査だけ(製品の実行物を起動しない)。公開の成功はreview-sparse-controlなどの通常実行で通る',
    'signal': '型検査だけ(製品の実行物を起動しない)',
}


def reduced_folder(root, path):
    folder = Path(root) / 'reduced-path'
    folder.mkdir(exist_ok=True)
    for tool in ('git', 'grep'):
        real = shutil.which(tool, path=path)
        if real is None:
            raise AssertionError(f'縮めたPATHに置く道具がありません: {tool}')
        link = folder / tool
        if not link.exists():
            os.symlink(os.path.realpath(real), link)
    return folder


def wrapper(root, binary, folder, *, hidden_tool=None):
    """`binary`の代わりに渡す実行物。hidden_toolは故障検出の変異(ほかの道具を隠れて起動する)。"""
    name = 'reduced-' + (hidden_tool or 'binary')
    path = Path(root) / name
    quoted = lambda value: "'" + str(value).replace("'", "'\\''") + "'"
    body = '#!/bin/sh\nPATH=' + quoted(folder) + '\nexport PATH\n'
    if hidden_tool:
        body += quoted(binary) + ' "$@"\nrc=$?\n' + hidden_tool + ' >/dev/null 2>&1 || exit 99\nexit $rc\n'
    else:
        body += 'exec ' + quoted(binary) + ' "$@"\n'
    path.write_text(body)
    path.chmod(0o700)
    return path


class Only:
    """モジュールのケース一覧を、選んだIDだけに一時的に絞る。"""
    def __init__(self, module, name, ids):
        self.module, self.name, self.ids = module, name, ids

    def __enter__(self):
        self.original = getattr(self.module, self.name)
        chosen = [case for case in self.original() if case['id'] in self.ids]
        exact_ids(self.ids, [case['id'] for case in chosen])
        setattr(self.module, self.name, lambda: chosen)

    def __exit__(self, *exc):
        setattr(self.module, self.name, self.original)


def run(binary, *, external_vectors, vectors, fixture_type, legacy, root):
    from . import review_cases
    folder = reduced_folder(root, os.environ['PATH'])
    reduced = wrapper(root, binary, folder)
    done = []
    chosen = [case for case in vectors if case['id'] in SELECTED['args'] + SELECTED['ere']]
    done += external_vectors(reduced, chosen)
    profile_vectors = [case for case in vectors if case['id'] in SELECTED['profile']]
    done += external_vectors(reduced, profile_vectors)
    with Only(review_cases, 'cases', SELECTED['review']):
        done += [value['id'] for value in review_cases.run_cases(reduced, fixture_type=fixture_type, legacy=legacy)]
    not_run = []
    with Only(review_cases, 'partial_cases', SELECTED['partial']):
        try:
            done += [value['id'] for value in review_cases.run_partial(reduced, fixture_type=fixture_type, legacy=legacy)]
        except NotRun as error:
            not_run += error.ids
    expected = [i for ids in SELECTED.values() for i in ids]
    exact_ids(expected, done + not_run)
    # 変異: 隠れて`date`を起動する実行物は、縮めたPATHでは旧版と違う結果になる。
    mutant = wrapper(root, binary, folder, hidden_tool='date')
    try:
        external_vectors(mutant, [case for case in vectors if case['id'] == 'args-normal'])
    except AssertionError:
        rejected = True
    else:
        rejected = False
    if not rejected:
        raise AssertionError('ほかの道具を隠れて起動する実行物を、縮めたPATHで拒否できません')
    return {'cases': done, 'not_run': not_run, 'excluded_groups': EXCLUDED, 'mutant_rejected': ['hidden-date']}
