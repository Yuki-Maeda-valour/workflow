#!/usr/bin/env python3
"""S2L のコピーで共通 Graph 10 件と安全観測・YAML 11 件の誤変更を検出する。"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import hashlib
import re
import sys
import stat

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path('crates/cli/src/commands/loop_state/config.rs')
TESTS = {
 'early-effective': ('graph_orders_all_roots_conditions_rechecks_candidates', 'CONFIG_ORDER'),
 'truncated-queue': ('graph_drains_growing_queue', 'CONFIG_QUEUE'),
 'new-budget': ('graph_forwards_one_budget_to_every_operation', 'CONFIG_BUDGET_ID'),
 'extended-deadline': ('graph_keeps_deadline_at_three_late_boundaries', 'CONFIG_DEADLINE'),
 'missing-cycle-check': ('graph_rejects_normal_and_deferred_cycles_itself', 'CONFIG_CYCLE'),
 'missing-recheck': ('graph_rejects_origin_change_after_effective_operation', 'CONFIG_RECHECK'),
 'result-key': ('graph_returns_exact_keys_digest_origin_order_candidates', 'CONFIG_KEYS'),
 'result-digest': ('graph_returns_exact_keys_digest_origin_order_candidates', 'CONFIG_DIGEST'),
 'result-origin-order': ('graph_returns_exact_keys_digest_origin_order_candidates', 'CONFIG_ORIGINS_ORDER'),
 'result-candidate-state': ('graph_returns_exact_keys_digest_origin_order_candidates', 'CONFIG_CANDIDATES'),
}

def replace_once(source: str, old: str, new: str) -> str:
    # rustfmt前後で同じRust token列を探す。文字列リテラルの中身は変えない。
    tokens=re.findall(r'"(?:\\.|[^"\\])*"|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|[^\s]',old)
    pattern=r'\s*'.join(re.escape(token) for token in tokens)
    matches=list(re.finditer(pattern,source))
    if len(matches)!=1:raise AssertionError(f'mutation anchor count != 1: {old!r}: {len(matches)}')
    match=matches[0]
    return source[:match.start()]+new+source[match.end():]

def mutate(source: str, name: str) -> str:
    if name == 'early-effective':
        return replace_once(source, 'let base=operations.roots(top,budget)?;', 'let base=operations.roots(top,budget)?; operations.effective(top,budget)?;')
    if name == 'truncated-queue':
        return replace_once(source, 'while cursor < graph.pending.len() {', 'let initial_pending=graph.pending.len(); while cursor < initial_pending {')
    if name == 'new-budget':
        return replace_once(source, 'let raw = graph.operations.effective(top,graph.budget)?;', 'let mut replacement=Budget::new(graph.budget.limits.clone())?; let raw = graph.operations.effective(top,&mut replacement)?;')
    if name == 'extended-deadline':
        return replace_once(source, 'let raw = graph.operations.effective(top,graph.budget)?;', 'graph.budget.started += std::time::Duration::from_secs(60); let raw = graph.operations.effective(top,graph.budget)?;')
    if name == 'missing-cycle-check':
        return replace_once(source, 'graph.cycle(Path::new(node), &mut HashSet::new(), &mut HashSet::new())?;', 'let _=node;')
    if name == 'missing-recheck':
        return replace_once(source, 'for path in actual {', 'for path in actual.into_iter().take(0) {')
    if name == 'result-key':
        return replace_once(source, '("effective_sha256", V::from(state::hash(&raw))),', '("wrong_sha256", V::from(state::hash(&raw))),')
    if name == 'result-digest':
        return replace_once(source, '("effective_sha256", V::from(state::hash(&raw))),', '("effective_sha256", V::from(state::hash(b"wrong"))),')
    if name == 'result-origin-order':
        return replace_once(source, 'let observed = std::mem::take(&mut graph.observed);', 'graph.observed.reverse(); let observed = std::mem::take(&mut graph.observed);')
    if name == 'result-candidate-state':
        return replace_once(source, 'let candidates = graph.operations.candidates(top, common, git_dir, graph.budget)?;', 'let mut candidates = graph.operations.candidates(top, common, git_dir, graph.budget)?; if let V::List(rows)=&mut candidates{rows[1].set("state",V::from("present"));}')
    raise AssertionError(name)

EXTRA={
 'secret-body-open':('crates/cli/src/commands/loop_state/scan.rs','i01_secret_globs_never_open_body','I01_NO_OPEN'),
 'names-without-charge':('crates/cli/src/commands/loop_state/scan.rs','i03_git_and_empty_names_share_consumption','I03_NAMES_EXACT_CHARGE'),
 'ancestor-full-comparison':('crates/platform/src/loop_state.rs','i09_ancestor_sibling_allowed_inode_change_rejected','I09_SIBLING_ALLOWED'),
 'new-admin-directory-open':('crates/cli/src/commands/loop_state/preflight.rs','m07_changed_git_pointer_never_opens_new_admin','SAFETY_NO_DIRECTORY'),
 'new-common-directory-open':('crates/cli/src/commands/loop_state/preflight.rs','m08_changed_commondir_rejected_before_git','SAFETY_NO_DIRECTORY'),
 'missing-kill-wait':('crates/cli/src/commands/loop_state/process.rs','l05_run_expired_after_spawn_reaps_direct_child','L05_REAPED_AND_CLOSED'),
 'missing-stdout-close':('crates/cli/src/commands/loop_state/process.rs','l05_run_expired_after_spawn_reaps_direct_child','L05_REAPED_AND_CLOSED'),
 'digests-no-io-continuation':('crates/cli/src/commands/loop_state/mod.rs','i07_read_oserror_is_not_promoted_to_stop','I07_FIXED_DIAGNOSTIC'),
 'yaml-no-second-compose':('crates/core/src/loop_state/mod.rs','yaml_recursion_temporary_second_compose_and_merge_frames','YAML_TEMPORARY_AND_SECOND_COMPOSE'),
 'yaml-no-temporary-call':('crates/core/src/loop_state/legacy_scanner.rs','yaml_recursion_temporary_second_compose_and_merge_frames','YAML_TEMPORARY_AND_SECOND_COMPOSE'),
 'yaml-no-merge-push':('crates/core/src/loop_state/construct.rs','yaml_recursion_temporary_second_compose_and_merge_frames','YAML_MERGE_FRAMES'),
}

def extra_mutate(source,name):
    if name=='secret-body-open':
        # LinuxはH1を迂回する実openもOS監視で落とす。Macは同じ実hash入口で観測する。
        if sys.platform.startswith('linux'):
            # 木の根は旧 tree と同じく os.path.dirname/basename のまま分ける(r6)。変異の意味は同じ。
            return replace_once(source,'let (parent, name) = state::raw_parent(root)?;','let _=std::fs::read(root.join("private.txt"))?;let (parent,name)=state::raw_parent(root)?;')
        return replace_once(source,'} else if secret {','} else if secret {if before.is_regular(){let _=hash_at(parent,name,&before,budget)?;}')
    if name=='names-without-charge':
        return replace_once(source,'budget.tick(0)?; names.push(child);','names.push(child);')
    if name=='ancestor-full-comparison':
        return replace_once(source,'open_dir(name,&before,crate::fs::SnapshotMatch::DirectoryPath)','open_dir(name,&before,crate::fs::SnapshotMatch::Full)')
    if name=='new-admin-directory-open':
        # 基準は保存状態の str のまま比べる(r5)。比べる前に新しい管理先を開く変異は同じ。
        return replace_once(source,'if config::path_text(&held) != state::abspath_text(expected) {','let _=state::directory(&held)?;if config::path_text(&held) != state::abspath_text(expected) {')
    if name=='new-common-directory-open':
        # 旧版の str の比較と同じく、バイトで比べる(r7)。比べる前に新しい common を開く変異は同じ。
        return replace_once(source,'if resolved.as_os_str() != state::absolute(common)?.as_os_str() {','let _=state::directory(&resolved)?;if resolved.as_os_str() != state::absolute(common)?.as_os_str() {')
    if name=='missing-kill-wait':
        return replace_once(source,'&&child.try_wait()?.is_none(){child.kill_wait()?;}','&&child.try_wait()?.is_none(){}')
    if name=='missing-stdout-close':return replace_once(source,'child.close_stdout();','')
    if name=='digests-no-io-continuation':
        return replace_once(source,'Err(e) if e.is_os()=>{lines.extend(b"-\\n");if failure.is_none(){failure=Some(E::stop("状態を読めない"));}}','Err(e) if e.is_os()=>return Err(e),')
    if name=='yaml-no-second-compose':
        return replace_once(source,'legacy_recursion::compose(text,recursion,true).map_err(unreadable)?;','')
    if name=='yaml-no-temporary-call':
        return replace_once(source,'while self.need(d+1)? {','while self.need(d)? {')
    if name=='yaml-no-merge-push':
        return replace_once(source,'self.probe(self.recursion.map_or(0,|c|c.entry_depth+7)+stack.len())?;','self.probe(self.recursion.map_or(0,|c|c.entry_depth+7))?;')
    raise AssertionError(name)

def validate_anchors(native):
    """Every required mutation must replace exactly once, before any build."""
    source=(native/SOURCE).read_text()
    for name in TESTS:mutate(source,name)
    for name,(path,_,_)in EXTRA.items():extra_mutate((native/path).read_text(),name)
    return len(TESTS)+len(EXTRA)

def asserted_failure(result: subprocess.CompletedProcess[bytes], test: str, marker: str) -> bool:
    output=result.stdout+result.stderr
    return (result.returncode == 101 and f'{test} ... FAILED'.encode() in output
            and b'panicked at' in output and marker.encode() in output
            and b'1 failed' in output and b'0 ignored' in output)

def run(arguments: list[str], cwd: Path, env: dict[str,str], timeout: int=240) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(arguments,cwd=cwd,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,check=False)

def executable(build: subprocess.CompletedProcess[bytes]) -> Path:
    if build.returncode:
        raise AssertionError('mutation compilation failed: '+build.stderr.decode(errors='backslashreplace'))
    found=[]
    for line in build.stdout.splitlines():
        item=json.loads(line)
        if item.get('reason')=='compiler-artifact' and item.get('executable') and item['target']['name']=='loop_state_fixture':
            found.append(Path(item['executable']))
    if len(found)!=1:
        raise AssertionError(f'expected one test executable, got {found}')
    return found[0]

def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only',choices=sorted({**TESTS,**EXTRA}))
    args=parser.parse_args()
    started=time.monotonic()
    selected=[args.only] if args.only else [*TESTS,*EXTRA]
    with tempfile.TemporaryDirectory(prefix='s2l-mutations-') as temporary:
        base=Path(temporary); checkout=base/'checkout'; native=checkout/'native'
        shutil.copytree(ROOT/'native',native,ignore=shutil.ignore_patterns('target','__pycache__'))
        legacy=Path('plugins/dev-workflow/skills/ship-task/scripts/loop-state.py')
        (checkout/legacy).parent.mkdir(parents=True)
        shutil.copyfile(ROOT/legacy,checkout/legacy)
        env=os.environ.copy();env['CARGO_TARGET_DIR']=str(base/'target')
        # source_input_hash がコピーの入力だけを列挙するための自作 scratch index。
        for command in [['git','init','-q'],['git','add','native']]:
            result=run(command,checkout,env)
            if result.returncode: raise AssertionError(result.stderr)
        assert validate_anchors(native)==21,'S2L_MUTATION_ANCHOR_INVENTORY'
        source=(native/SOURCE).read_text();
        inputs=[(p.relative_to(native).as_posix(),stat.S_IMODE(p.stat().st_mode),hashlib.sha256(p.read_bytes()).hexdigest())for p in sorted(native.rglob('*'))if p.is_file()]
        input_digest=hashlib.sha256(json.dumps(inputs,separators=(',',':')).encode()).hexdigest()
        print(json.dumps({"native_input_sha256":input_digest,"source_sha256":hashlib.sha256(source.encode()).hexdigest(),"properties_sha256":hashlib.sha256((native/"crates/cli/src/commands/loop_state/config_graph_tests.rs").read_bytes()).hexdigest(),"safety_sha256":hashlib.sha256((native/"crates/cli/src/commands/loop_state/safety_tests.rs").read_bytes()).hexdigest()}),flush=True)
        budget_path=native/'crates/cli/src/commands/loop_state/budget.rs';budget=budget_path.read_text()
        build_command=['cargo','test','--locked','-p','dev-workflow-cli','--example','loop_state_fixture','--no-run','--message-format=json']
        binary=executable(run(build_command,native,env))
        baseline=run([str(binary),'loop_state::config_graph_tests::','--nocapture'],native,env)
        if baseline.returncode or b'8 passed; 0 failed; 0 ignored' not in baseline.stdout:
            raise AssertionError('baseline did not execute all 8 properties: '+repr(baseline.stdout+baseline.stderr))
        safety=run([str(binary),'loop_state::safety_tests::','--nocapture'],native,env)
        if safety.returncode or b'26 passed; 0 failed; 0 ignored' not in safety.stdout:
            raise AssertionError('baseline did not execute all 26 safety/YAML observations: '+repr(safety.stdout+safety.stderr))
        extra_sources={path:(native/path).read_text() for path,_,_ in EXTRA.values()}
        for name in selected:
            (native/SOURCE).write_text(source)
            for path,original in extra_sources.items():(native/path).write_text(original)
            if name in TESTS:(native/SOURCE).write_text(mutate(source,name))
            else:
                path,_,_=EXTRA[name];(native/path).write_text(extra_mutate(extra_sources[path],name))
            budget_path.write_text(replace_once(budget,'    started: Instant,','    pub(super) started: Instant,') if name=='extended-deadline' else budget)
            binary=executable(run(build_command,native,env))
            if name in TESTS:test,marker=TESTS[name];module='config_graph_tests'
            else:_,test,marker=EXTRA[name];module='safety_tests'
            full=f'loop_state::{module}::{test}'
            result=run([str(binary),'--exact',full,'--nocapture'],native,env)
            if not asserted_failure(result,full,marker):
                raise AssertionError(f'{name}: specified assertion was not detected: '+repr(result.stdout+result.stderr))
            print(json.dumps({'mutation':name,'test':full,'assertion':marker,'returncode':result.returncode,'detected':True}),flush=True)
        print(f'結果: 合格 / baseline graph 8 + safety/YAML 26, graph mutation {sum(name in TESTS for name in selected)}, safety/yaml mutation {sum(name in EXTRA for name in selected)}, skip 0 / {time.monotonic()-started:.3f} 秒')
    return 0
if __name__=='__main__':
    raise SystemExit(main())
