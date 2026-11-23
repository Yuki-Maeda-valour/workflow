//! 診断の予算(時間と量の上限)と失敗の型。旧版 `diagnostic-runtime.py` の `LIMITS`・`Failure`・`Budget` と同じ動作にする。
//!
//! C0b の `core::budget::Budget` は量が 1 系統なので使わない。期限を作るときに 1 回だけ決めること、
//! 量を工程ごとに戻さないことは同じにする。
use std::sync::Arc;
use std::sync::OnceLock;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

const MIB: u64 = 1024 * 1024;

/// 診断の量の 9 種類。名前と上限は旧版の `LIMITS` と同じ。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    ManagementCopy,
    ManagementOutput,
    ManagementEntries,
    WorktreeEntries,
    Body,
    PatchCopy,
    GitOutput,
    Stderr,
    Json,
}

impl Kind {
    pub const ALL: [Kind; 9] = [
        Kind::ManagementCopy,
        Kind::ManagementOutput,
        Kind::ManagementEntries,
        Kind::WorktreeEntries,
        Kind::Body,
        Kind::PatchCopy,
        Kind::GitOutput,
        Kind::Stderr,
        Kind::Json,
    ];

    fn slot(self) -> usize {
        self as usize
    }

    /// 旧版の量の名前(下線を含む字面のまま)。
    pub fn name(self) -> &'static str {
        match self {
            Kind::ManagementCopy => "management_copy",
            Kind::ManagementOutput => "management_output",
            Kind::ManagementEntries => "management_entries",
            Kind::WorktreeEntries => "worktree_entries",
            Kind::Body => "body",
            Kind::PatchCopy => "patch_copy",
            Kind::GitOutput => "git_output",
            Kind::Stderr => "stderr",
            Kind::Json => "json",
        }
    }

    /// 上限を超えたときの理由 `<量の名前>-limit`。
    pub fn limit_reason(self) -> &'static str {
        match self {
            Kind::ManagementCopy => "management_copy-limit",
            Kind::ManagementOutput => "management_output-limit",
            Kind::ManagementEntries => "management_entries-limit",
            Kind::WorktreeEntries => "worktree_entries-limit",
            Kind::Body => "body-limit",
            Kind::PatchCopy => "patch_copy-limit",
            Kind::GitOutput => "git_output-limit",
            Kind::Stderr => "stderr-limit",
            Kind::Json => "json-limit",
        }
    }

    pub fn default_limit(self) -> u64 {
        match self {
            Kind::ManagementCopy => 200 * MIB,
            Kind::ManagementOutput => 200 * MIB,
            Kind::ManagementEntries => 2000,
            Kind::WorktreeEntries => 200_000,
            Kind::Body => 1024 * MIB,
            Kind::PatchCopy => 200 * MIB,
            Kind::GitOutput => 200 * MIB,
            Kind::Stderr => MIB,
            Kind::Json => 280 * MIB,
        }
    }

    pub fn from_name(name: &str) -> Option<Kind> {
        Kind::ALL.into_iter().find(|kind| kind.name() == name)
    }
}

/// 理由の字面と終了値の組(旧版の `Failure`)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Failure {
    pub reason: &'static str,
    pub code: u8,
}

impl Failure {
    pub const fn new(reason: &'static str, code: u8) -> Self {
        Self { reason, code }
    }
}

/// 既定の期限(秒)。
pub const DEFAULT_SECONDS: f64 = 300.0;

/// 製品の時計。単調な時計の秒(浮動小数)。
fn monotonic() -> f64 {
    static START: OnceLock<Instant> = OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_secs_f64()
}

pub struct Budget {
    deadline: f64,
    limits: [u64; 9],
    used: [u64; 9],
    stop: Arc<AtomicBool>,
    #[cfg(test)]
    clock: Option<Box<dyn FnMut() -> f64>>,
    #[cfg(test)]
    charge_bias: u64,
}

impl Budget {
    /// 既定の期限(300 秒)と既定の上限。作るときに時計を 1 回呼んで期限を固定する。
    pub fn new() -> Self {
        Self::build(DEFAULT_SECONDS)
    }

    /// 検査のときだけ使う(実時間の期限の検査)。製品の経路から期限を変える口は作らない。
    #[cfg(test)]
    pub(crate) fn with_seconds(seconds: f64) -> Self {
        Self::build(seconds)
    }

    fn build(seconds: f64) -> Self {
        Self {
            deadline: monotonic() + seconds,
            limits: Kind::ALL.map(Kind::default_limit),
            used: [0; 9],
            stop: Arc::new(AtomicBool::new(false)),
            #[cfg(test)]
            clock: None,
            #[cfg(test)]
            charge_bias: 0,
        }
    }

    /// 検査のときだけ使う。時計と上限を引数で渡す(旧版の `Budget(clock, seconds, limits)` と同じ)。
    #[cfg(test)]
    pub(crate) fn for_test(
        seconds: f64,
        mut clock: Box<dyn FnMut() -> f64>,
        limits: [u64; 9],
    ) -> Self {
        let deadline = clock() + seconds;
        Self {
            deadline,
            limits,
            used: [0; 9],
            stop: Arc::new(AtomicBool::new(false)),
            clock: Some(clock),
            charge_bias: 0,
        }
    }

    /// 検査のときだけ使う。`management_output` に足す量を、毎回この値だけ多くする(既定 0)。
    /// 値は予算 1 つごとの欄に持つ。
    #[cfg(test)]
    pub(crate) fn set_test_charge_bias(&mut self, bias: u64) {
        self.charge_bias = bias;
    }

    #[cfg(test)]
    pub(crate) fn test_charge_bias(&self) -> u64 {
        self.charge_bias
    }

    /// 外から停止の印を立てるための共有の印。
    pub fn stop_flag(&self) -> Arc<AtomicBool> {
        Arc::clone(&self.stop)
    }

    pub fn request_stop(&self) {
        self.stop.store(true, Ordering::SeqCst);
    }

    #[cfg(test)]
    pub(crate) fn clear_stop(&self) {
        self.stop.store(false, Ordering::SeqCst);
    }

    pub fn used(&self, kind: Kind) -> u64 {
        self.used[kind.slot()]
    }

    fn now(&mut self) -> f64 {
        #[cfg(test)]
        if let Some(clock) = self.clock.as_mut() {
            return clock();
        }
        monotonic()
    }

    /// 残り時間(秒)。`max(0.0, 期限 - 時計())`。時計を 1 回呼ぶ。
    pub fn remaining(&mut self) -> f64 {
        let now = self.now();
        (self.deadline - now).max(0.0)
    }

    /// 停止の印 → 残り時間の順に確かめる。
    pub fn check(&mut self) -> Result<(), Failure> {
        if self.stop.load(Ordering::SeqCst) {
            return Err(Failure::new("interrupted", 20));
        }
        if self.remaining() <= 0.0 {
            return Err(Failure::new("time-limit", 20));
        }
        Ok(())
    }

    /// `check()` の後に量を足し、上限を超えれば `<量の名前>-limit`。足した量は失敗しても戻さない。
    /// 足し算が桁あふれするときは `u64::MAX` に張り付ける(旧版は上限を超えた値が残るのと同じ結果)。
    pub fn charge(&mut self, kind: Kind, count: u64) -> Result<(), Failure> {
        self.check()?;
        #[cfg(test)]
        let count = if kind == Kind::ManagementOutput {
            count.saturating_add(self.charge_bias)
        } else {
            count
        };
        let slot = kind.slot();
        self.used[slot] = self.used[slot].saturating_add(count);
        if self.used[slot] > self.limits[slot] {
            return Err(Failure::new(kind.limit_reason(), 20));
        }
        Ok(())
    }
}

impl Default for Budget {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;
    use std::rc::Rc;

    fn fake(now: &Rc<Cell<f64>>) -> Box<dyn FnMut() -> f64> {
        let now = Rc::clone(now);
        Box::new(move || now.get())
    }

    fn defaults() -> [u64; 9] {
        Kind::ALL.map(Kind::default_limit)
    }

    #[test]
    fn limits_match_the_legacy_table() {
        let expected = [
            ("management_copy", 200 * MIB),
            ("management_output", 200 * MIB),
            ("management_entries", 2000),
            ("worktree_entries", 200_000),
            ("body", 1024 * MIB),
            ("patch_copy", 200 * MIB),
            ("git_output", 200 * MIB),
            ("stderr", MIB),
            ("json", 280 * MIB),
        ];
        for (kind, (name, limit)) in Kind::ALL.into_iter().zip(expected) {
            assert_eq!(kind.name(), name);
            assert_eq!(kind.default_limit(), limit);
            assert_eq!(kind.limit_reason(), format!("{name}-limit"));
            assert_eq!(Kind::from_name(name), Some(kind));
        }
        assert_eq!(Kind::from_name("nope"), None);
    }

    #[test]
    fn fresh_budget_has_no_injected_bias() {
        assert_eq!(Budget::new().test_charge_bias(), 0);
        let now = Rc::new(Cell::new(0.0));
        assert_eq!(
            Budget::for_test(1.0, fake(&now), defaults()).test_charge_bias(),
            0
        );
    }

    #[test]
    fn every_quantity_allows_the_boundary_and_rejects_one_more() {
        for kind in Kind::ALL {
            let mut budget = Budget::new();
            assert_eq!(budget.charge(kind, kind.default_limit()), Ok(()));
            let failure = budget.charge(kind, 1).unwrap_err();
            assert_eq!(failure, Failure::new(kind.limit_reason(), 20));
            // 足した量は戻さない。
            assert_eq!(budget.used(kind), kind.default_limit() + 1);
            assert!(budget.charge(kind, 0).is_err());
        }
    }

    #[test]
    fn overflow_sticks_at_u64_max_and_fails() {
        let mut budget = Budget::new();
        assert_eq!(
            budget.charge(Kind::Body, u64::MAX),
            Err(Failure::new("body-limit", 20))
        );
        assert_eq!(
            budget.charge(Kind::Body, u64::MAX),
            Err(Failure::new("body-limit", 20))
        );
        assert_eq!(budget.used(Kind::Body), u64::MAX);
        assert_eq!(budget.used(Kind::Json), 0);
    }

    #[test]
    fn deadline_is_fixed_at_creation_and_stop_is_checked_first() {
        let now = Rc::new(Cell::new(10.0));
        let mut budget = Budget::for_test(1.0, fake(&now), defaults());
        now.set(10.75);
        assert_eq!(budget.remaining(), 0.25);
        assert_eq!(budget.charge(Kind::Body, 1), Ok(()));
        now.set(11.0);
        assert_eq!(budget.remaining(), 0.0);
        assert_eq!(budget.check(), Err(Failure::new("time-limit", 20)));
        // 期限切れの charge は量を足さない。
        assert_eq!(
            budget.charge(Kind::Body, 1),
            Err(Failure::new("time-limit", 20))
        );
        assert_eq!(budget.used(Kind::Body), 1);
        now.set(0.0);
        let mut budget = Budget::for_test(1.0, fake(&now), defaults());
        budget.request_stop();
        now.set(100.0);
        assert_eq!(budget.check(), Err(Failure::new("interrupted", 20)));
        assert_eq!(
            budget.charge(Kind::Body, 1),
            Err(Failure::new("interrupted", 20))
        );
        assert_eq!(budget.used(Kind::Body), 0);
        let flag = budget.stop_flag();
        flag.store(false, Ordering::SeqCst);
        assert_eq!(budget.check(), Err(Failure::new("time-limit", 20)));
    }

    #[test]
    fn clock_is_called_once_per_check() {
        let calls = Rc::new(Cell::new(0_u32));
        let counter = Rc::clone(&calls);
        let clock = Box::new(move || {
            counter.set(counter.get() + 1);
            0.0
        });
        let mut budget = Budget::for_test(300.0, clock, defaults());
        assert_eq!(calls.get(), 1);
        budget.check().unwrap();
        assert_eq!(calls.get(), 2);
        budget.charge(Kind::Json, 1).unwrap();
        assert_eq!(calls.get(), 3);
    }

    #[test]
    fn charge_bias_applies_only_to_management_output() {
        let mut budget = Budget::new();
        budget.set_test_charge_bias(1);
        budget.charge(Kind::ManagementOutput, 5).unwrap();
        budget.charge(Kind::Body, 5).unwrap();
        assert_eq!(budget.used(Kind::ManagementOutput), 6);
        assert_eq!(budget.used(Kind::Body), 5);
        assert_eq!(Budget::new().used(Kind::ManagementOutput), 0);
    }
}
