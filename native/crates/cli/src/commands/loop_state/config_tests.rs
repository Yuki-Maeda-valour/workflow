use super::{
    budget::{Budget, Limits},
    config::{self, ConfigEvent, Observer},
    error::Result,
    json::PyValue as V,
    process, state,
};
use std::{
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::atomic::{AtomicU64, Ordering},
};
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Fixture {
    base: PathBuf,
    root: PathBuf,
    common: PathBuf,
    global: PathBuf,
    ordinary: PathBuf,
    conditional: PathBuf,
    first: PathBuf,
    second: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        let base = std::fs::canonicalize(std::env::temp_dir())
            .unwrap()
            .join(format!(
                "s2l-config-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
        Self::at(base, true)
    }
    fn at(base: PathBuf, prepare: bool) -> Self {
        if prepare {
            std::fs::create_dir(&base).unwrap();
        }
        let root = base.join("repo");
        if prepare {
            std::fs::create_dir(&root).unwrap();
        }
        let common = root.join(".git");
        let global = base.join("global");
        let ordinary = base.join("m-ordinary.inc");
        let conditional = base.join("n-conditional.inc");
        let first = base.join("z-first.inc");
        let second = base.join("a-second.inc");
        let f = Self {
            base,
            root,
            common,
            global,
            ordinary,
            conditional,
            first,
            second,
        };
        if !prepare {
            return f;
        }
        f.git(&["-c", "init.defaultBranch=main", "init", "-q"]);
        f.git(&[
            "config",
            "remote.origin.url",
            "https://example.invalid/repo",
        ]);
        let condition = "hasconfig:remote.*.url:https://example.invalid/**";
        std::fs::write(&f.ordinary, "[contract]\nvalue = ordinary\n").unwrap();
        std::fs::write(&f.conditional, "[contract]\nvalue = conditional\n").unwrap();
        std::fs::write(&f.second, "[contract]\nvalue = nested\n").unwrap();
        std::fs::write(
            &f.first,
            format!(
                "[includeIf \"{condition}\"]\npath = {}\n",
                f.second.display()
            ),
        )
        .unwrap();
        std::fs::write(&f.global,format!("[include]\npath = {}\n[includeIf \"onbranch:main\"]\npath = {}\n[includeIf \"{condition}\"]\npath = {}\n",f.ordinary.display(),f.conditional.display(),f.first.display())).unwrap();
        f
    }
    fn git(&self, args: &[&str]) {
        assert!(
            Command::new("git")
                .arg("-C")
                .arg(&self.root)
                .args(process::SAFE_GIT)
                .args(args)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .env("GIT_CONFIG_NOSYSTEM", "1")
                .env("GIT_CONFIG_GLOBAL", "/dev/null")
                .status()
                .unwrap()
                .success()
        );
    }
    fn snapshot(&self, budget: &mut Budget, observer: &mut dyn Observer) -> Result<V> {
        // テストの環境変更は単一テスト内だけ。並行テストへグローバル設定を渡さない。
        config::snapshot_observed(
            &self.root,
            &self.common,
            &self.common,
            budget,
            None,
            observer,
        )
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.base);
    }
}
#[derive(Default)]
struct Trace {
    events: Vec<(String, Option<PathBuf>)>,
    times: Vec<std::time::Instant>,
    raw: Vec<u8>,
    expire: Option<String>,
    change: Option<PathBuf>,
    cycle: Option<(PathBuf, PathBuf, bool)>,
    effective_reached: bool,
}
impl Observer for Trace {
    fn event(
        &mut self,
        event: ConfigEvent<'_>,
        budget: &mut Budget,
        raw: Option<&[u8]>,
    ) -> Result<()> {
        let (name, path) = match event {
            ConfigEvent::RootsStart => ("roots-start", None),
            ConfigEvent::RootsDone => ("roots-done", None),
            ConfigEvent::ReadStart(p) => ("read-start", Some(p)),
            ConfigEvent::ReadDone(p) => ("read-done", Some(p)),
            ConfigEvent::ConditionStart(p) => ("condition-start", Some(p)),
            ConfigEvent::IncludePath(p) => ("include-path", Some(p)),
            ConfigEvent::EffectiveStart => ("effective-start", None),
            ConfigEvent::EffectiveDone => ("effective-done", None),
            ConfigEvent::CandidatesStart => ("candidates-start", None),
            ConfigEvent::CandidatesDone => ("candidates-done", None),
        };
        self.events.push((name.into(), path.map(Path::to_owned)));
        self.times.push(std::time::Instant::now());
        if name == "effective-start" {
            self.effective_reached = true;
        }
        if name == "effective-done" {
            self.raw = raw.unwrap().to_vec();
            if let Some(path) = &self.change {
                std::fs::write(path, "[contract]\nvalue = changed-after-effective\n").unwrap();
            }
        }
        if self.expire.as_deref() == Some(name) {
            budget.expire();
        }
        if let Some((second, first, deferred)) = &self.cycle {
            if path == Some(second.as_path()) {
                if name == "read-start" {
                    std::fs::write(
                        second,
                        format!(
                            "[{}]\npath = {}\n",
                            if *deferred {
                                "includeIf \"hasconfig:remote.*.url:https://example.invalid/**\""
                            } else {
                                "include"
                            },
                            first.display()
                        ),
                    )
                    .unwrap();
                } else if name == "read-done" {
                    std::fs::write(second, "[contract]\nvalue = acyclic\n").unwrap();
                }
            }
        }
        Ok(())
    }
}
fn budget() -> Budget {
    Budget::new(Limits {
        items: 10000i128.into(),
        total: 10000000i128.into(),
        one: 1000000i128.into(),
        seconds: 10i128.into(),
    })
    .unwrap()
}
fn isolated(name: &str, body: fn(&Fixture)) {
    if std::env::var("S2L_CONFIG_TEST_ISOLATED").ok().as_deref() != Some(name) {
        let f = Fixture::new();
        let status = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                &format!("loop_state::config_tests::{name}"),
                "--nocapture",
            ])
            .env("S2L_CONFIG_TEST_ISOLATED", name)
            .env("S2L_CONFIG_FIXTURE", &f.base)
            .env("GIT_CONFIG_GLOBAL", &f.global)
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .status()
            .unwrap();
        assert!(status.success(), "CONFIG_ISOLATED_CHILD: {name}");
    } else {
        let fixture = Fixture::at(
            std::env::var_os("S2L_CONFIG_FIXTURE").unwrap().into(),
            false,
        );
        #[cfg(target_os = "linux")]
        body(&fixture);
        #[cfg(target_os = "macos")]
        {
            let _ = body;
            mac_actual_boundary(&fixture, name);
        }
    }
}
type Operations = Vec<(std::time::Instant, super::observe::Event)>;
fn snapshot(f: &Fixture, budget: &mut Budget, trace: &mut Trace) -> (Result<V>, Operations) {
    let events = std::rc::Rc::new(std::cell::RefCell::new(Vec::new()));
    let sink = events.clone();
    let guard = super::observe::install(move |event| {
        sink.borrow_mut()
            .push((std::time::Instant::now(), event.clone()));
        Ok(())
    });
    let result = f.snapshot(budget, trace);
    drop(guard);
    let events = std::rc::Rc::try_unwrap(events).unwrap().into_inner();
    (result, events)
}
fn effective(event: &super::observe::Event) -> bool {
    matches!(event,super::observe::Event::Spawn{arguments,index:false,..}if arguments.iter().any(|v|v=="--includes")&&arguments.iter().any(|v|v=="--show-scope"))
}
fn position(trace: &Trace, name: &str, path: Option<&Path>) -> usize {
    trace
        .events
        .iter()
        .position(|(n, p)| n == name && p.as_deref() == path)
        .unwrap()
}
#[test]
fn config_snapshot_orders_reads_conditions_rechecks_and_candidates() {
    isolated(
        "config_snapshot_orders_reads_conditions_rechecks_and_candidates",
        |f| {
            let mut trace = Trace::default();
            let (result, operations) = snapshot(f, &mut budget(), &mut trace);
            result.unwrap();
            let roots = position(&trace, "roots-done", None);
            let condition = trace
                .events
                .iter()
                .position(|(n, _)| n == "condition-start")
                .unwrap();
            let before = position(&trace, "effective-start", None);
            let after = position(&trace, "effective-done", None);
            let candidate = position(&trace, "candidates-start", None);
            let done = position(&trace, "candidates-done", None);
            for path in [&f.global, &f.common.join("config")] {
                assert!(
                    roots < position(&trace, "read-done", Some(path)),
                    "CONFIG_ORDER: roots before reads"
                );
                assert!(
                    position(&trace, "read-done", Some(path)) < condition,
                    "CONFIG_ORDER: every root before conditions"
                );
            }
            let real_git = operations.iter().find(|(_, e)| effective(e)).unwrap().0;
            for path in [
                &f.global,
                &f.common.join("config"),
                &f.ordinary,
                &f.conditional,
                &f.first,
                &f.second,
            ] {
                let reads = trace
                    .events
                    .iter()
                    .enumerate()
                    .filter(|(_, e)| e.0 == "read-done" && e.1.as_deref() == Some(path.as_path()))
                    .map(|(i, _)| i)
                    .collect::<Vec<_>>();
                assert!(
                    reads[0] < before && trace.times[reads[0]] < real_git,
                    "CONFIG_ORDER: graph before effective Git"
                );
                assert!(
                    reads.iter().any(|i| after < *i && *i < candidate),
                    "CONFIG_ORDER: every origin rechecked"
                );
            }
            assert!(candidate < done, "CONFIG_ORDER: candidates completion");
        },
    );
}
#[test]
fn config_snapshot_returns_exact_values_and_ordered_candidates() {
    isolated(
        "config_snapshot_returns_exact_values_and_ordered_candidates",
        |f| {
            let mut trace = Trace::default();
            let (result, _) = snapshot(f, &mut budget(), &mut trace);
            let result = result.unwrap();
            let V::Dict(keys) = &result else {
                panic!("CONFIG_KEYS")
            };
            assert_eq!(keys.len(), 3, "CONFIG_KEYS");
            assert!(
                keys.iter()
                    .all(|(k, _)| ["effective_sha256", "origins", "candidates"]
                        .iter()
                        .any(|s| k == &super::json::Text::from(*s))),
                "CONFIG_KEYS"
            );
            assert_eq!(
                result.get("effective_sha256"),
                &V::from(state::hash(&trace.raw)),
                "CONFIG_DIGEST"
            );
            let origins = result.get("origins").list().unwrap();
            let paths = origins
                .iter()
                .map(|r| r.get("path").string().unwrap().as_codepoints())
                .collect::<Vec<_>>();
            assert!(
                paths.windows(2).all(|p| p[0] < p[1]),
                "CONFIG_ORIGINS_ORDER"
            );
            let candidates = result.get("candidates").list().unwrap();
            assert_eq!(
                candidates[0].get("kind"),
                &V::from("common"),
                "CONFIG_CANDIDATES"
            );
            assert_eq!(
                candidates[1].get("state"),
                &V::from("absent"),
                "CONFIG_CANDIDATES"
            );
            let legacy = legacy_snapshot(f);
            assert_eq!(
                result,
                *legacy.get("result"),
                "CONFIG_VALUES: all keys, values, paths, metadata and ordered candidates"
            );
        },
    );
}
#[test]
fn config_snapshot_drains_nested_hasconfig_queue() {
    isolated("config_snapshot_drains_nested_hasconfig_queue", |f| {
        let mut trace = Trace::default();
        let (result, _) = snapshot(f, &mut budget(), &mut trace);
        let result = result.unwrap_or_else(|e| {
            panic!(
                "CONFIG_QUEUE: unexpected Stop: {}",
                e.stop_reason().unwrap_or("uncaught exception")
            )
        });
        assert_eq!(
            result.get("origins").list().unwrap().len(),
            6,
            "CONFIG_QUEUE"
        );
        assert!(
            trace
                .events
                .contains(&("condition-start".into(), Some(f.first.clone()))),
            "CONFIG_QUEUE"
        );
        assert!(
            position(&trace, "read-done", Some(&f.second))
                < position(&trace, "effective-start", None),
            "CONFIG_QUEUE"
        );
    });
}
#[test]
fn config_snapshot_rejects_normal_and_deferred_cycles_before_effective_git() {
    isolated(
        "config_snapshot_rejects_normal_and_deferred_cycles_before_effective_git",
        |f| {
            let (result, _) = snapshot(f, &mut budget(), &mut Trace::default());
            result.unwrap();
            for deferred in [false, true] {
                std::fs::write(&f.second, "[contract]\nvalue = acyclic\n").unwrap();
                std::fs::write(
                    &f.first,
                    format!("[include]\npath = {}\n", f.second.display()),
                )
                .unwrap();
                let mut trace = Trace {
                    cycle: Some((f.second.clone(), f.first.clone(), deferred)),
                    ..Trace::default()
                };
                let (result, operations) = snapshot(f, &mut budget(), &mut trace);
                assert!(
                    !trace.effective_reached && !operations.iter().any(|(_, e)| effective(e)),
                    "CONFIG_CYCLE: effective Git reached before cycle rejection"
                );
                assert!(
                    result
                        .err()
                        .is_some_and(|e| e.stop_reason() == Some("include が循環している")),
                    "CONFIG_CYCLE"
                );
            }
        },
    );
}
#[test]
fn config_snapshot_passes_same_budget_through_every_phase() {
    isolated(
        "config_snapshot_passes_same_budget_through_every_phase",
        |f| {
            let mut b = budget();
            b.tick(0).unwrap();
            b.read(1).unwrap();
            let id = &b as *const Budget as usize;
            let deadline = b.deadline_identity();
            let (result, operations) = snapshot(f, &mut b, &mut Trace::default());
            result.unwrap();
            let mut phases = std::collections::BTreeSet::new();
            for (_, event) in operations {
                if let super::observe::Event::Budget {
                    phase,
                    address,
                    count,
                    bytes,
                    deadline: actual,
                } = event
                {
                    phases.insert(phase);
                    assert_eq!(address, id, "CONFIG_BUDGET_ID: {phase}");
                    assert_eq!(actual, deadline, "CONFIG_BUDGET_ID: deadline {phase}");
                    assert!(count >= 1 && bytes >= 1, "CONFIG_BUDGET_ID: seed {phase}");
                }
            }
            assert_eq!(
                phases,
                std::collections::BTreeSet::from([
                    "git",
                    "file",
                    "condition",
                    "include-path",
                    "candidates",
                    "bytes"
                ]),
                "CONFIG_BUDGET_ID: every real callee"
            );
            assert_eq!(
                b.deadline_identity(),
                deadline,
                "CONFIG_BUDGET_ID: unchanged deadline"
            );
        },
    );
}
#[test]
fn config_snapshot_keeps_callers_item_and_byte_consumption() {
    isolated(
        "config_snapshot_keeps_callers_item_and_byte_consumption",
        |f| {
            let mut measured = budget();
            snapshot(f, &mut measured, &mut Trace::default()).0.unwrap();
            for items in [true, false] {
                let mut limits = budget().limits;
                if items {
                    limits.items = (measured.count as i128).into();
                } else {
                    limits.total = (measured.bytes as i128).into();
                }
                let mut exact = Budget::new(limits.clone()).unwrap();
                snapshot(f, &mut exact, &mut Trace::default()).0.unwrap();
                let mut used = Budget::new(limits).unwrap();
                if items {
                    used.tick(0).unwrap()
                } else {
                    used.read(1).unwrap()
                }
                let error = snapshot(f, &mut used, &mut Trace::default()).0.unwrap_err();
                assert_eq!(
                    error.stop_reason(),
                    Some(if items {
                        "状態の観察の項目数が上限を超えた"
                    } else {
                        "通常ファイルの総読取量が上限を超えた"
                    }),
                    "CONFIG_BUDGET"
                );
            }
        },
    );
}
#[test]
fn config_snapshot_keeps_callers_deadline_at_late_phases() {
    isolated(
        "config_snapshot_keeps_callers_deadline_at_late_phases",
        |f| {
            snapshot(f, &mut budget(), &mut Trace::default()).0.unwrap();
            for phase in ["effective-start", "effective-done", "candidates-start"] {
                let mut trace = Trace {
                    expire: Some(phase.into()),
                    ..Trace::default()
                };
                let (result, operations) = snapshot(f, &mut budget(), &mut trace);
                assert_eq!(
                    result.unwrap_err().stop_reason(),
                    Some(if phase == "effective-start" {
                        "git の状態読取が時間内に終わらない"
                    } else {
                        "状態の観察が 60 秒の上限を超えた"
                    }),
                    "CONFIG_DEADLINE: {phase}"
                );
                for (_, event) in operations {
                    if let super::observe::Event::Spawned { pid, .. } = event {
                        assert!(
                            !Command::new("kill")
                                .args(["-0", &pid.to_string()])
                                .stdout(Stdio::null())
                                .stderr(Stdio::null())
                                .status()
                                .unwrap()
                                .success(),
                            "CONFIG_DEADLINE: child not reaped"
                        );
                    }
                }
            }
        },
    );
}
#[test]
fn config_snapshot_rejects_origin_change_after_effective_git() {
    isolated(
        "config_snapshot_rejects_origin_change_after_effective_git",
        |f| {
            snapshot(f, &mut budget(), &mut Trace::default()).0.unwrap();
            let mut trace = Trace {
                change: Some(f.second.clone()),
                ..Trace::default()
            };
            let result = snapshot(f, &mut budget(), &mut trace).0;
            assert_eq!(
                result.unwrap_err().stop_reason(),
                Some("config が実効値の読取中に変わった"),
                "CONFIG_RECHECK"
            );
        },
    );
}
fn legacy_snapshot(f: &Fixture) -> V {
    let source = Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .unwrap()
        .join("plugins/dev-workflow/skills/ship-task/scripts/loop-state.py");
    let script = r#"import importlib.util,json,sys
spec=importlib.util.spec_from_file_location('s2l_contract_old',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
b=m.Budget(10000,10000000,1000000,10);result=m.config_snapshot(sys.argv[2],sys.argv[3],b,git_dir=sys.argv[3]);print(json.dumps({'result':result,'count':b.count,'bytes':b.bytes},sort_keys=True,separators=(',',':')))
"#;
    let out = Command::new("python3")
        .args(["-B", "-c", script])
        .arg(source)
        .arg(&f.root)
        .arg(&f.common)
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "CONFIG_LEGACY: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    super::json::load(&out.stdout, 4300).unwrap()
}

#[cfg(target_os = "macos")]
fn mac_actual_boundary(f: &Fixture, name: &str) {
    // 同じ8テスト入口から実拒否を旧版と照合する。未到達の後段は成功に数えない。
    // 共通Graph8性質/10変異は全OSで別途必須。Linuxの実接続8件は維持する。
    let mut b = budget();
    let mut trace = Trace::default();
    let (result, operations) = snapshot(f, &mut b, &mut trace);
    let error = result.expect_err("MAC_REAL_SNAPSHOT_MUST_NOT_SUCCEED");
    let source = Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .unwrap()
        .join("plugins/dev-workflow/skills/ship-task/scripts/loop-state.py");
    let script = r#"import importlib.util,json,sys
spec=importlib.util.spec_from_file_location('s2l_mac_boundary',sys.argv[1]);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
b=m.Budget(10000,10000000,1000000,10)
try:
 m.config_snapshot(sys.argv[2],sys.argv[3],b,git_dir=sys.argv[3]);result={'success':True}
except m.Stop as error:
 result={'success':False,'reason':str(error),'count':b.count,'bytes':b.bytes}
print(json.dumps(result,separators=(',',':')))
"#;
    let old = Command::new("python3")
        .args(["-B", "-c", script])
        .arg(source)
        .arg(&f.root)
        .arg(&f.common)
        .output()
        .unwrap();
    assert!(
        old.status.success(),
        "MAC_OLD_BOUNDARY_CAPTURE: {}",
        String::from_utf8_lossy(&old.stderr)
    );
    let old = super::json::load(&old.stdout, 4300).unwrap();
    assert_eq!(old.get("success"), &V::from(false), "MAC_OLD_MUST_REJECT");
    assert_eq!(
        old.get("reason"),
        &V::from(error.stop_reason().expect("MAC_STOP")),
        "MAC_REAL_REJECTION_REASON"
    );
    assert_eq!(
        old.get("count"),
        &V::from(i128::try_from(b.count).unwrap()),
        "MAC_REAL_REJECTION_ITEMS"
    );
    assert_eq!(
        old.get("bytes"),
        &V::from(i128::try_from(b.bytes).unwrap()),
        "MAC_REAL_REJECTION_BYTES"
    );
    assert!(operations.iter().any(|(_,e)|matches!(e,super::observe::Event::Spawn{arguments,..}if arguments.iter().any(|v|v=="--no-includes"))),"MAC_ROOTS_ACTUALLY_SPAWNED");
    assert!(
        !operations.iter().any(|(_, e)| effective(e)),
        "MAC_EFFECTIVE_NOT_REACHED"
    );
    println!(
        "S2L_OS_BOUNDARY name={name} os=macos old_reject=true rust_reject=true effective_reached=false graph_properties_from_real_io=0"
    );
}
