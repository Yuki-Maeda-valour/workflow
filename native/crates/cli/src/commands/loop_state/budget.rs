use super::{
    error::{LegacyException, Result},
    json::PyInteger,
};
use std::{cmp::Ordering, time::Instant};
#[derive(Clone)]
pub struct Limits {
    pub items: PyInteger,
    pub total: PyInteger,
    pub one: PyInteger,
    pub seconds: PyInteger,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            items: 100000i128.into(),
            total: 1073741824i128.into(),
            one: 67108864i128.into(),
            seconds: 60i128.into(),
        }
    }
}
pub struct Budget {
    pub limits: Limits,
    pub count: u128,
    pub bytes: u128,
    started: Instant,
    seconds: f64,
}
impl Budget {
    #[cfg(test)]
    pub fn deadline_identity(&self) -> (Instant, u64) {
        (self.started, self.seconds.to_bits())
    }
    #[cfg(test)]
    pub fn expire(&mut self) {
        self.started = Instant::now() - std::time::Duration::from_secs_f64(self.seconds + 1.0);
    }
    pub fn new(limits: Limits) -> Result<Self> {
        let seconds = limits.seconds.to_f64();
        if !seconds.is_finite() {
            return Err(LegacyException::Python {
                kind: "OverflowError",
                reason: "int too large to convert to float".into(),
                location: "Budget.__init__",
            }
            .at("__init__", 56, "self.deadline = time.monotonic() + seconds"));
        }
        Ok(Self {
            limits,
            count: 0,
            bytes: 0,
            started: Instant::now(),
            seconds,
        })
    }
    fn check(&self) -> Result<()> {
        // SIGINT を受けていれば、旧版が次の命令で上げる KeyboardInterrupt と同じく止める。
        super::interrupt::check()?;
        if self.started.elapsed().as_secs_f64() > self.seconds {
            Err(LegacyException::stop("状態の観察が 60 秒の上限を超えた"))
        } else {
            Ok(())
        }
    }
    pub fn tick(&mut self, size: u128) -> Result<()> {
        self.check()?;
        self.count += 1;
        if self.limits.items.cmp_u128(self.count) == Ordering::Less {
            return Err(LegacyException::stop("状態の観察の項目数が上限を超えた"));
        }
        if self.limits.one.cmp_u128(size) == Ordering::Less {
            return Err(LegacyException::stop(
                "通常ファイル 1 件の読取量が上限を超えた",
            ));
        }
        self.bytes += size;
        if self.limits.total.cmp_u128(self.bytes) == Ordering::Less {
            return Err(LegacyException::stop(
                "通常ファイルの総読取量が上限を超えた",
            ));
        }
        Ok(())
    }
    pub fn read(&mut self, size: usize) -> Result<()> {
        self.check()?;
        self.bytes += size as u128;
        if self.limits.total.cmp_u128(self.bytes) == Ordering::Less {
            return Err(LegacyException::stop(
                "通常ファイルの総読取量が上限を超えた",
            ));
        }
        Ok(())
    }
    pub fn remaining(&self) -> Result<f64> {
        super::interrupt::check()?;
        let left = self.left();
        if left <= 0.0 {
            Err(LegacyException::stop("状態の観察が 60 秒の上限を超えた"))
        } else {
            Ok(left)
        }
    }
    pub fn left(&self) -> f64 {
        self.seconds - self.started.elapsed().as_secs_f64()
    }
    pub fn one(&self, size: u128) -> Result<()> {
        if self.limits.one.cmp_u128(size) == Ordering::Less {
            Err(LegacyException::stop(
                "通常ファイル 1 件の読取量が上限を超えた",
            ))
        } else {
            Ok(())
        }
    }
    pub fn fits_chunk(&self, size: usize) -> bool {
        self.limits.total.cmp_u128(self.bytes + size as u128) != Ordering::Less
    }
}
