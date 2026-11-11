//! OS の memfd 可否とは独立した同一 Graph の検査。実 IO 到達件数には算入しない。
use super::{
    budget::{Budget, Limits},
    config::{self, ConfigEvent, Observer, Operations, Rows, Trusted},
    error::Result,
    json::{PyValue as V, Text},
    runtime, state,
};
use std::{
    cell::RefCell,
    collections::BTreeSet,
    path::{Path, PathBuf},
    rc::Rc,
};
#[derive(Default)]
struct Record {
    calls: Vec<(String, PathBuf, usize, u128, u128)>,
    expire: Option<&'static str>,
    change: bool,
    cycle: Option<bool>,
    effective: bool,
}
struct Ops(Rc<RefCell<Record>>);
impl Ops {
    fn call(&mut self, phase: &str, path: &Path, b: &mut Budget) -> Result<()> {
        self.0.borrow_mut().calls.push((
            phase.into(),
            path.into(),
            b as *const Budget as usize,
            b.count,
            b.bytes,
        ));
        b.tick(0)?;
        b.read(1)
    }
    fn raw() -> Vec<u8> {
        b"file:/global\0file:/repo/config\0file:/ordinary\0file:/conditional\0file:/first\0file:/second\0".to_vec()
    }
}
impl Operations for Ops {
    fn roots(&mut self, top: &Path, b: &mut Budget) -> Result<Vec<u8>> {
        self.call("git-roots", top, b)?;
        Ok(b"file:/global\0file:/repo/config\0".to_vec())
    }
    fn file(
        &mut self,
        _: &Path,
        p: &Path,
        b: &mut Budget,
        _: Option<&Trusted>,
    ) -> Result<(V, Rows, (i128, i128, u32))> {
        self.call("file", p, b)?;
        self.call("bytes", p, b)?;
        let condition = "includeif.hasconfig:remote.*.url:https://example.invalid/**.path";
        let rows: Vec<(&str, &str)> = match p.to_str().unwrap() {
            "/global" => vec![
                ("include.path", "/ordinary"),
                ("includeif.onbranch:main.path", "/conditional"),
                (condition, "/first"),
            ],
            "/repo/config" => vec![("remote.origin.url", "https://example.invalid/repo")],
            "/first" => vec![(condition, "/second")],
            "/second" => match self.0.borrow().cycle {
                Some(false) => vec![("include.path", "/first")],
                Some(true) => vec![(condition, "/first")],
                None => vec![("contract.value", "nested")],
            },
            _ => vec![("contract.value", "leaf")],
        };
        let id = match p.to_str().unwrap() {
            "/global" => 1,
            "/repo/config" => 2,
            "/ordinary" => 3,
            "/conditional" => 4,
            "/first" => 5,
            "/second" => 6,
            _ => panic!("unexpected graph path"),
        };
        let revision = if self.0.borrow().effective && self.0.borrow().change {
            1
        } else {
            0
        };
        let item = V::object([
            ("path", V::from(format!("config:{}", p.display()))),
            ("kind", V::from("file")),
            (
                "meta",
                V::object([("dev", V::from(1i128)), ("ino", V::from(id))]),
            ),
            ("sha256", V::from(format!("revision-{revision}"))),
        ]);
        Ok((
            item,
            rows.into_iter()
                .map(|(k, v)| (Text::from(k), Some(Text::from(v))))
                .collect(),
            (1, 100, 0o700),
        ))
    }
    fn include(
        &mut self,
        _: &Path,
        value: Option<&Text>,
        origin: &Path,
        b: &mut Budget,
    ) -> Result<PathBuf> {
        self.call("include-path", origin, b)?;
        Ok(runtime::utf8(value.unwrap()).into())
    }
    fn condition(
        &mut self,
        _: &Path,
        _: &Text,
        origin: &Path,
        _: &Rows,
        b: &mut Budget,
    ) -> Result<bool> {
        self.call("condition", origin, b)?;
        Ok(true)
    }
    fn effective(&mut self, top: &Path, b: &mut Budget) -> Result<Vec<u8>> {
        self.call("git-effective", top, b)?;
        self.0.borrow_mut().effective = true;
        Ok(Self::raw())
    }
    fn candidates(&mut self, _: &Path, common: &Path, _: &Path, b: &mut Budget) -> Result<V> {
        self.call("candidates", common, b)?;
        Ok(V::List(vec![
            V::object([("kind", V::from("common")), ("state", V::from("present"))]),
            V::object([("kind", V::from("worktree")), ("state", V::from("absent"))]),
        ]))
    }
}
struct Trace(Rc<RefCell<Record>>);
impl Observer for Trace {
    fn event(
        &mut self,
        event: ConfigEvent<'_>,
        budget: &mut Budget,
        _: Option<&[u8]>,
    ) -> Result<()> {
        let phase = match event {
            ConfigEvent::EffectiveStart => "effective-start",
            ConfigEvent::EffectiveDone => "effective-done",
            ConfigEvent::CandidatesStart => "candidates-start",
            _ => "other",
        };
        if self.0.borrow().expire == Some(phase) {
            budget.expire()
        }
        Ok(())
    }
}
fn budget() -> Budget {
    Budget::new(Limits {
        items: 10000i128.into(),
        total: 10000i128.into(),
        one: 10000i128.into(),
        seconds: 10i128.into(),
    })
    .unwrap()
}
fn run(record: &Rc<RefCell<Record>>, b: &mut Budget) -> Result<V> {
    config::snapshot_with_operations(
        Path::new("/repo"),
        Path::new("/repo"),
        Path::new("/repo"),
        b,
        None,
        &mut Trace(record.clone()),
        &mut Ops(record.clone()),
    )
}
fn fresh() -> Rc<RefCell<Record>> {
    Rc::new(RefCell::new(Record::default()))
}
#[test]
fn graph_orders_all_roots_conditions_rechecks_candidates() {
    let r = fresh();
    run(&r, &mut budget()).unwrap();
    let r = r.borrow();
    let calls = &r.calls;
    let position = |phase: &str, path: &str| {
        calls
            .iter()
            .position(|c| c.0 == phase && c.1 == Path::new(path))
            .unwrap()
    };
    let condition = calls.iter().position(|c| c.0 == "condition").unwrap();
    for path in ["/global", "/repo/config"] {
        assert!(position("file", path) < condition, "CONFIG_ORDER: roots");
    }
    let effective = position("git-effective", "/repo");
    let candidates = position("candidates", "/repo");
    for path in [
        "/global",
        "/repo/config",
        "/ordinary",
        "/conditional",
        "/first",
        "/second",
    ] {
        let indices = calls
            .iter()
            .enumerate()
            .filter(|(_, c)| c.0 == "file" && c.1 == Path::new(path))
            .map(|(i, _)| i)
            .collect::<Vec<_>>();
        assert!(
            indices[0] < effective,
            "CONFIG_ORDER: effective before graph"
        );
        assert!(
            indices.iter().any(|i| effective < *i && *i < candidates),
            "CONFIG_RECHECK: missing origin recheck"
        );
    }
}
#[test]
fn graph_returns_exact_keys_digest_origin_order_candidates() {
    let r = fresh();
    let v = run(&r, &mut budget()).unwrap();
    let V::Dict(keys) = &v else {
        panic!("CONFIG_KEYS")
    };
    assert_eq!(keys.len(), 3, "CONFIG_KEYS");
    assert!(
        keys.iter()
            .all(|(key, _)| ["effective_sha256", "origins", "candidates"]
                .iter()
                .any(|v| key == &Text::from(*v))),
        "CONFIG_KEYS"
    );
    assert_eq!(
        v.get("effective_sha256"),
        &V::from(state::hash(&Ops::raw())),
        "CONFIG_DIGEST"
    );
    let paths = v
        .get("origins")
        .list()
        .unwrap()
        .iter()
        .map(|v| runtime::utf8(v.get("path").string().unwrap()))
        .collect::<Vec<_>>();
    assert_eq!(
        paths,
        vec![
            "config:/conditional",
            "config:/first",
            "config:/global",
            "config:/ordinary",
            "config:/repo/config",
            "config:/second"
        ],
        "CONFIG_ORIGINS_ORDER"
    );
    assert_eq!(
        v.get("candidates").list().unwrap()[1].get("state"),
        &V::from("absent"),
        "CONFIG_CANDIDATES"
    );
}
#[test]
fn graph_drains_growing_queue() {
    let r = fresh();
    run(&r, &mut budget())
        .unwrap_or_else(|e| panic!("CONFIG_QUEUE: {}", e.stop_reason().unwrap_or("uncaught")));
    let r = r.borrow();
    let second = r
        .calls
        .iter()
        .position(|c| c.0 == "file" && c.1 == Path::new("/second"))
        .unwrap();
    let effective = r.calls.iter().position(|c| c.0 == "git-effective").unwrap();
    assert!(second < effective, "CONFIG_QUEUE");
}
#[test]
fn graph_rejects_normal_and_deferred_cycles_itself() {
    for deferred in [false, true] {
        let r = fresh();
        r.borrow_mut().cycle = Some(deferred);
        let result = run(&r, &mut budget());
        assert!(
            !r.borrow().effective,
            "CONFIG_CYCLE: effective operation reached before rejection"
        );
        assert!(
            result
                .err()
                .is_some_and(|e| e.stop_reason() == Some("include が循環している")),
            "CONFIG_CYCLE"
        );
        assert!(
            !r.borrow().effective,
            "CONFIG_CYCLE: effective operation reached"
        );
    }
    run(&fresh(), &mut budget()).unwrap();
}
#[test]
fn graph_forwards_one_budget_to_every_operation() {
    let r = fresh();
    let mut b = budget();
    b.tick(0).unwrap();
    b.read(1).unwrap();
    let id = &b as *const Budget as usize;
    let deadline = b.deadline_identity();
    run(&r, &mut b).unwrap();
    let r = r.borrow();
    let mut phases = BTreeSet::new();
    for (phase, _, actual, count, bytes) in &r.calls {
        phases.insert(phase.as_str());
        assert_eq!(*actual, id, "CONFIG_BUDGET_ID");
        assert!(*count >= 1 && *bytes >= 1, "CONFIG_BUDGET_ID: seed");
    }
    assert_eq!(
        phases,
        BTreeSet::from([
            "git-roots",
            "file",
            "bytes",
            "condition",
            "include-path",
            "git-effective",
            "candidates"
        ]),
        "CONFIG_BUDGET_ID: coverage"
    );
    assert_eq!(
        b.deadline_identity(),
        deadline,
        "CONFIG_BUDGET_ID: deadline"
    );
}
#[test]
fn graph_keeps_preconsumed_item_and_byte_limits() {
    let mut measured = budget();
    run(&fresh(), &mut measured).unwrap();
    for items in [true, false] {
        let mut limits = budget().limits;
        if items {
            limits.items = (measured.count as i128).into()
        } else {
            limits.total = (measured.bytes as i128).into()
        }
        run(&fresh(), &mut Budget::new(limits.clone()).unwrap()).unwrap();
        let mut used = Budget::new(limits).unwrap();
        if items {
            used.tick(0).unwrap()
        } else {
            used.read(1).unwrap()
        }
        assert_eq!(
            run(&fresh(), &mut used).unwrap_err().stop_reason(),
            Some(if items {
                "状態の観察の項目数が上限を超えた"
            } else {
                "通常ファイルの総読取量が上限を超えた"
            }),
            "CONFIG_BUDGET"
        );
    }
}
#[test]
fn graph_keeps_deadline_at_three_late_boundaries() {
    for phase in ["effective-start", "effective-done", "candidates-start"] {
        let r = fresh();
        r.borrow_mut().expire = Some(phase);
        assert!(
            run(&r, &mut budget())
                .err()
                .is_some_and(|e| e.stop_reason() == Some("状態の観察が 60 秒の上限を超えた")),
            "CONFIG_DEADLINE: {phase}"
        );
    }
    run(&fresh(), &mut budget()).unwrap();
}
#[test]
fn graph_rejects_origin_change_after_effective_operation() {
    let r = fresh();
    r.borrow_mut().change = true;
    assert!(
        run(&r, &mut budget())
            .err()
            .is_some_and(|e| e.stop_reason() == Some("config が実効値の読取中に変わった")),
        "CONFIG_RECHECK"
    );
    run(&fresh(), &mut budget()).unwrap();
}
