//! 観測 1 回ごとの予算(旧版の `Budget`)。項目 10,000・1 ファイル 8 MiB・総量 64 MiB・15 秒。
//!
//! C0b の `dev_workflow_core::budget::Budget` が旧版と同じ順序を守れるので、それを使う。
//! - `tick`: 項目の加算 → 項目上限 → 期限(旧版は `entries > MAX or now > deadline` の 1 文)。
//! - `reserve`: `tick` → 1 ファイル上限 → 総量の確認と加算。
//!
//! 違いは、総量を超えた予約で C0b が超えた後の値を残すことだけ。旧版でも `Stop` で観測が終わり、
//! 予算は捨てられるので、観測できない。
use super::error::PyException;
use super::fsio::Identity;
use dev_workflow_core::budget::{Budget, BudgetLimits};
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
            component_bytes: HashMap::new(),
        }
    }

    /// `Budget.tick(count)`。
    pub fn tick(&mut self, count: u64) -> Result<(), PyException> {
        let over = self.core.add_items(count).is_err() || self.core.check_time().is_err();
        if over {
            Err(PyException::stop(TICK_STOP))
        } else {
            Ok(())
        }
    }

    /// `Budget.reserve(size)`。
    pub fn reserve(&mut self, size: u64) -> Result<(), PyException> {
        self.tick(0)?;
        if self.core.check_file_bytes(size).is_err() || self.core.add_bytes(size).is_err() {
            return Err(PyException::stop(RESERVE_STOP));
        }
        Ok(())
    }

    pub fn entries(&self) -> u64 {
        self.core.items()
    }

    pub fn bytes(&self) -> u64 {
        self.core.bytes()
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
}
