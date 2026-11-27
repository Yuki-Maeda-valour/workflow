//! 観測 1 回ごとの予算(旧版の `Budget`)。項目 10,000・1 ファイル 8 MiB・総量 64 MiB・15 秒。
//!
//! C0b の `dev_workflow_core::budget::Budget` が旧版と同じ順序を守れるので、それを使う。
//! - `tick`: 項目の加算 → 項目上限 → 期限(旧版は `entries > MAX or now > deadline` の 1 文)。
//! - `reserve`: `tick` → 1 ファイル上限 → 総量の確認と加算。
//!
//! 違いは、総量を超えた予約で C0b が超えた後の値を残すことだけ。旧版は、確かめてから加算するので、止まった予約は
//! 総量を変えない。`bytes()` は旧版と同じく、止まった予約を足さない値を返す(固定期待が予算の値を比べる)。
use super::depth::{Depth, call, compare, native};
use super::error::PyException;
use super::fsio::Identity;
use dev_workflow_core::budget::{Budget, BudgetLimits};
use dev_workflow_platform::environment_guard as os;
use std::collections::HashMap;
use std::time::{Duration, Instant};

pub const MAX_FILE: u64 = 8 * 1024 * 1024;
pub const MAX_TOTAL: u64 = 64 * 1024 * 1024;
pub const MAX_ENTRIES: u64 = 10_000;
pub const MAX_SECONDS: u64 = 15;

/// `tick` の `Stop` の理由。
pub const TICK_STOP: &str = "環境の項目数または検査時間が上限を超える";
/// `reserve` の `Stop` の理由。
pub const RESERVE_STOP: &str = "環境の読取バイト上限を超える";

/// 旧版の `Budget` と、`component_bytes`(同じ物理ファイルを観測 1 回につき 1 度だけ読む控え)。
/// 控えの寿命は予算と同じ(旧版は予算の属性として持つ)。
#[derive(Debug)]
pub struct EnvBudget {
    core: Budget,
    /// 旧版の `Budget.bytes`(止まった予約は足さない)。
    bytes_seen: u64,
    /// `identity(st)` → (本文, mode)。
    pub component_bytes: HashMap<Identity, (Vec<u8>, u32)>,
}

impl EnvBudget {
    pub fn new() -> Self {
        Self::with_deadline(Instant::now() + Duration::from_secs(MAX_SECONDS))
    }

    /// 期限を決めて作る(単体テストが期限の境界を作るため)。
    pub fn with_deadline(deadline: Instant) -> Self {
        Self {
            core: Budget::new(
                BudgetLimits {
                    items: MAX_ENTRIES,
                    total_bytes: MAX_TOTAL,
                    file_bytes: MAX_FILE,
                },
                deadline,
            ),
            bytes_seen: 0,
            component_bytes: HashMap::new(),
        }
    }

    /// `Budget.tick(count)`。
    ///
    /// `SIGINT` が届いていれば、まず `KeyboardInterrupt` にする(旧版は、次のバイトコードの境目で投げる。
    /// 観測の各段が `tick` を通るので、ここで印を見る)。
    pub fn tick(&mut self, count: u64) -> Result<(), PyException> {
        self.tick_at(count, None)
    }

    /// `Budget.tick(count)` を、深さ `caller` のフレームから呼ぶ形。旧版の `tick` の中の呼出しの順:
    /// フレームに入る → `self.entries += count` → `self.entries > MAX_ENTRIES`(比較)→ 超えていなければ
    /// `time.monotonic()`(C の呼出し)→ `> self.deadline`(比較)。項目上限を超えたときは、時計を読まない。
    pub fn tick_at(&mut self, count: u64, caller: Depth) -> Result<(), PyException> {
        if os::take_sigint() {
            return Err(PyException::new("KeyboardInterrupt", ""));
        }
        let inside = call(caller)?;
        let over_items = self.core.add_items(count).is_err();
        compare(inside)?;
        if over_items {
            return Err(PyException::stop(TICK_STOP));
        }
        native(inside)?;
        compare(inside)?;
        if self.core.check_time().is_err() {
            return Err(PyException::stop(TICK_STOP));
        }
        Ok(())
    }

    /// `Budget.reserve(size)`。
    pub fn reserve(&mut self, size: u64) -> Result<(), PyException> {
        self.reserve_at(size, None)
    }

    /// `Budget.reserve(size)` を、深さ `caller` のフレームから呼ぶ形。旧版の順: フレームに入る → `self.tick()` →
    /// `size > MAX_FILE`(比較。超えていれば止まる)→ `self.bytes + size > MAX_TOTAL`(比較)→ `self.bytes += size`。
    pub fn reserve_at(&mut self, size: u64, caller: Depth) -> Result<(), PyException> {
        let inside = call(caller)?;
        self.tick_at(0, inside)?;
        compare(inside)?;
        if self.core.check_file_bytes(size).is_err() {
            return Err(PyException::stop(RESERVE_STOP));
        }
        compare(inside)?;
        if self.core.add_bytes(size).is_err() {
            return Err(PyException::stop(RESERVE_STOP));
        }
        self.bytes_seen += size;
        Ok(())
    }

    pub fn entries(&self) -> u64 {
        self.core.items()
    }

    pub fn bytes(&self) -> u64 {
        self.bytes_seen
    }
}

impl Default for EnvBudget {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn message(result: Result<(), PyException>) -> String {
        super::super::pystr::to_string(result.unwrap_err().message.as_codepoints())
    }

    #[test]
    fn tick_stops_just_after_ten_thousand_items() {
        let mut budget = EnvBudget::new();
        budget.tick(MAX_ENTRIES).unwrap();
        budget.tick(0).unwrap();
        assert_eq!(message(budget.tick(1)), TICK_STOP);
        // 失敗後も加算は残り、以後の `tick(0)` も止まる(旧版は加算してから確かめる)。
        assert_eq!(budget.entries(), MAX_ENTRIES + 1);
        assert_eq!(message(budget.tick(0)), TICK_STOP);
        // 1 度に超える加算も同じ理由。
        assert_eq!(message(EnvBudget::new().tick(MAX_ENTRIES + 1)), TICK_STOP);
        let mut one_by_one = EnvBudget::new();
        for _ in 0..MAX_ENTRIES {
            one_by_one.tick(1).unwrap();
        }
        assert!(one_by_one.tick(1).is_err());
    }

    #[test]
    fn tick_stops_only_after_the_deadline() {
        let now = Instant::now();
        EnvBudget::with_deadline(now + Duration::from_secs(3600))
            .tick(0)
            .unwrap();
        let past = now.checked_sub(Duration::from_millis(5)).unwrap();
        assert_eq!(message(EnvBudget::with_deadline(past).tick(0)), TICK_STOP);
        // `reserve` も、先に `tick` の期限を見る(理由は `tick` のもの)。
        assert_eq!(
            message(EnvBudget::with_deadline(past).reserve(0)),
            TICK_STOP
        );
        // 期限が過ぎていても、`tick` の理由は項目上限と同じ 1 つの文。
        let mut late = EnvBudget::with_deadline(past);
        assert_eq!(message(late.reserve(MAX_FILE + 1)), TICK_STOP);
    }

    #[test]
    fn reserve_checks_the_file_limit_just_before_and_after() {
        let mut budget = EnvBudget::new();
        budget.reserve(MAX_FILE).unwrap();
        assert_eq!(budget.bytes(), MAX_FILE);
        assert_eq!(message(budget.reserve(MAX_FILE + 1)), RESERVE_STOP);
        // 1 ファイルの上限で止まった予約は、総量を足さない。
        assert_eq!(budget.bytes(), MAX_FILE);
        assert_eq!(message(EnvBudget::new().reserve(u64::MAX)), RESERVE_STOP);
    }

    #[test]
    fn reserve_checks_the_total_limit_just_before_and_after() {
        let mut budget = EnvBudget::new();
        for _ in 0..8 {
            budget.reserve(MAX_FILE).unwrap();
        }
        assert_eq!(budget.bytes(), MAX_TOTAL);
        budget.reserve(0).unwrap();
        assert_eq!(message(budget.reserve(1)), RESERVE_STOP);
        // 総量の直前: 64 MiB から 1 バイト足りない所に 1 バイトは入り、次の 1 バイトで止まる。
        let mut near = EnvBudget::new();
        for _ in 0..7 {
            near.reserve(MAX_FILE).unwrap();
        }
        near.reserve(MAX_FILE - 1).unwrap();
        near.reserve(1).unwrap();
        assert_eq!(message(near.reserve(1)), RESERVE_STOP);
    }

    #[test]
    fn reserve_runs_tick_first_and_counts_no_item() {
        let mut budget = EnvBudget::new();
        budget.reserve(10).unwrap();
        assert_eq!(budget.entries(), 0);
        // 項目上限に当たっていれば、大きすぎる予約でも `tick` の理由が先に出る。
        assert!(budget.tick(MAX_ENTRIES + 1).is_err());
        assert_eq!(message(budget.reserve(MAX_FILE + 1)), TICK_STOP);
    }

    /// `SIGINT` の印を立てるテストは、プロセス全体の信号の扱いを変えるので、別プロセスで行う。
    fn isolated(name: &str) -> bool {
        if std::env::var_os("S2E_BUDGET_ISOLATED").is_some_and(|value| value == name) {
            return true;
        }
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                &format!("environment_guard::budget::tests::{name}"),
                "--nocapture",
                "--test-threads=1",
            ])
            .env("S2E_BUDGET_ISOLATED", name)
            .output()
            .unwrap();
        let text = format!(
            "{}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(
            output.status.success(),
            "別プロセスの検査が失敗({name}): {text}"
        );
        assert!(
            text.contains("1 passed"),
            "別プロセスが検査を流していない({name}): {text}"
        );
        false
    }

    #[test]
    fn a_pending_sigint_is_a_keyboard_interrupt_at_the_next_tick() {
        if !isolated("a_pending_sigint_is_a_keyboard_interrupt_at_the_next_tick") {
            return;
        }
        os::signal_set_default(os::SIGINT).unwrap();
        assert!(os::install_sigint_handler().unwrap());
        // 印が無ければ、ふつうの `tick`。
        let mut budget = EnvBudget::new();
        budget.tick(0).unwrap();
        let sent = std::process::Command::new("kill")
            .args(["-INT", &std::process::id().to_string()])
            .status()
            .unwrap();
        assert!(sent.success());
        let start = Instant::now();
        while !os::sigint_pending() {
            assert!(
                start.elapsed() < Duration::from_secs(10),
                "SIGINT が届かない"
            );
            std::thread::sleep(Duration::from_millis(1));
        }
        let error = budget.tick(1).unwrap_err();
        assert_eq!(error.type_name, "KeyboardInterrupt");
        // 印は消え、項目も数えない(旧版は、次のバイトコードの境目で投げる)。
        assert!(!os::sigint_pending());
        assert_eq!(budget.entries(), 0);
        budget.tick(0).unwrap();
    }
}
