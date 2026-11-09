//! 呼出元が決めた上限・絶対期限と、戻さない累積値。
use std::time::{Duration, Instant};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BudgetLimits {
    pub items: u64,
    pub total_bytes: u64,
    pub file_bytes: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BudgetError {
    Deadline,
    Items,
    FileBytes,
    TotalBytes,
    Overflow,
}

#[derive(Debug)]
pub struct Budget {
    limits: BudgetLimits,
    deadline: Instant,
    items: u64,
    bytes: u64,
    overflowed: bool,
}

impl Budget {
    pub fn new(limits: BudgetLimits, deadline: Instant) -> Self {
        Self {
            limits,
            deadline,
            items: 0,
            bytes: 0,
            overflowed: false,
        }
    }

    pub fn items(&self) -> u64 {
        self.items
    }
    pub fn bytes(&self) -> u64 {
        self.bytes
    }
    pub fn deadline(&self) -> Instant {
        self.deadline
    }
    pub fn limits(&self) -> BudgetLimits {
        self.limits
    }

    pub fn check_time(&self) -> Result<(), BudgetError> {
        self.check_time_at(Instant::now())
    }

    fn check_time_at(&self, now: Instant) -> Result<(), BudgetError> {
        if now > self.deadline {
            Err(BudgetError::Deadline)
        } else {
            Ok(())
        }
    }

    pub fn remaining(&self) -> Result<Duration, BudgetError> {
        self.remaining_at(Instant::now())
    }

    fn remaining_at(&self, now: Instant) -> Result<Duration, BudgetError> {
        self.deadline
            .checked_duration_since(now)
            .filter(|left| !left.is_zero())
            .ok_or(BudgetError::Deadline)
    }

    pub fn add_items(&mut self, count: u64) -> Result<(), BudgetError> {
        add(&mut self.items, count, &mut self.overflowed)?;
        if self.items > self.limits.items {
            Err(BudgetError::Items)
        } else {
            Ok(())
        }
    }

    pub fn add_bytes(&mut self, count: u64) -> Result<(), BudgetError> {
        add(&mut self.bytes, count, &mut self.overflowed)?;
        if self.bytes > self.limits.total_bytes {
            Err(BudgetError::TotalBytes)
        } else {
            Ok(())
        }
    }

    pub fn check_file_bytes(&self, count: u64) -> Result<(), BudgetError> {
        if count > self.limits.file_bytes {
            Err(BudgetError::FileBytes)
        } else {
            Ok(())
        }
    }
}

fn add(value: &mut u64, count: u64, overflowed: &mut bool) -> Result<(), BudgetError> {
    if *overflowed {
        return Err(BudgetError::Overflow);
    }
    match value.checked_add(count) {
        Some(next) => {
            *value = next;
            Ok(())
        }
        None => {
            *overflowed = true;
            Err(BudgetError::Overflow)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn budget() -> Budget {
        Budget::new(
            BudgetLimits {
                items: 4,
                total_bytes: 4,
                file_bytes: 4,
            },
            Instant::now(),
        )
    }

    #[test]
    fn limits_accept_below_and_equal_and_keep_excess() {
        let mut b = budget();
        assert_eq!(b.add_items(3), Ok(()));
        assert_eq!(b.add_items(1), Ok(()));
        assert_eq!(b.add_items(1), Err(BudgetError::Items));
        assert_eq!(b.items(), 5);
        assert_eq!(b.add_bytes(3), Ok(()));
        assert_eq!(b.add_bytes(1), Ok(()));
        assert_eq!(b.add_bytes(1), Err(BudgetError::TotalBytes));
        assert_eq!(b.bytes(), 5);
        for n in [3, 4] {
            assert_eq!(b.check_file_bytes(n), Ok(()));
        }
        assert_eq!(b.check_file_bytes(5), Err(BudgetError::FileBytes));
        assert_eq!((b.items(), b.bytes()), (5, 5));
    }

    #[test]
    fn time_boundary_is_exact_and_does_not_consume() {
        let b = budget();
        let at = b.deadline();
        assert_eq!(b.check_time_at(at - Duration::from_nanos(1)), Ok(()));
        assert_eq!(b.check_time_at(at), Ok(()));
        assert_eq!(
            b.check_time_at(at + Duration::from_nanos(1)),
            Err(BudgetError::Deadline)
        );
        assert_eq!(
            b.remaining_at(at - Duration::from_nanos(1)),
            Ok(Duration::from_nanos(1))
        );
        assert_eq!(b.remaining_at(at), Err(BudgetError::Deadline));
        assert_eq!(
            b.remaining_at(at + Duration::from_nanos(1)),
            Err(BudgetError::Deadline)
        );
        assert_eq!((b.items(), b.bytes()), (0, 0));
    }

    #[test]
    fn overflow_is_sticky_for_both_counters() {
        for items in [false, true] {
            let mut b = Budget::new(
                BudgetLimits {
                    items: u64::MAX,
                    total_bytes: u64::MAX,
                    file_bytes: u64::MAX,
                },
                Instant::now(),
            );
            if items {
                b.add_items(u64::MAX).unwrap();
            } else {
                b.add_bytes(u64::MAX).unwrap();
            }
            let prior = (b.items(), b.bytes());
            let result = if items {
                b.add_items(1)
            } else {
                b.add_bytes(1)
            };
            assert_eq!(result, Err(BudgetError::Overflow));
            assert_eq!(b.add_items(0), Err(BudgetError::Overflow));
            assert_eq!(b.add_bytes(0), Err(BudgetError::Overflow));
            assert_eq!((b.items(), b.bytes()), prior);
        }
    }

    #[test]
    fn separate_operations_preserve_tick_order() {
        fn tick(b: &mut Budget, size: u64) -> Result<(), BudgetError> {
            b.check_time()?;
            b.add_items(1)?;
            b.check_file_bytes(size)?;
            b.add_bytes(size)
        }
        let mut b = Budget::new(
            BudgetLimits {
                items: 1,
                total_bytes: 0,
                file_bytes: 1,
            },
            Instant::now() + Duration::from_secs(10),
        );
        assert_eq!(tick(&mut b, 2), Err(BudgetError::FileBytes));
        assert_eq!((b.items(), b.bytes()), (1, 0));
        assert_eq!(tick(&mut b, 1), Err(BudgetError::Items));
        assert_eq!((b.items(), b.bytes()), (2, 0));
    }
}
