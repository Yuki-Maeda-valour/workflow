//! CPython 3.10 の二段出力 buffer を再現する R4A 専用状態機械。

use std::io;

pub const TEXT_CAPACITY: usize = 8192;
pub const DEFAULT_BINARY_CAPACITY: usize = 4096;

/// FileIO の一回の write。`Ok(None)` は EAGAIN を表す。
pub trait Sink {
    fn write_once(&mut self, bytes: &[u8]) -> io::Result<Option<usize>>;
    fn flush(&mut self) -> io::Result<()>;
    /// 部分的に成功した write の後の、信号の確認(CPython の `PyErr_CheckSignals`)。
    /// 届いている SIGINT があれば旗を下ろして `true`。呼出側が `KeyboardInterrupt` の失敗にする。
    fn interrupted(&mut self) -> bool {
        false
    }
}

/// SIGINT で中断された write の失敗(`EINTR`)。上位が `KeyboardInterrupt` にする。
fn interrupted_error() -> io::Error {
    io::Error::from_raw_os_error(4)
}

impl<S: Sink + ?Sized> Sink for Box<S> {
    fn write_once(&mut self, bytes: &[u8]) -> io::Result<Option<usize>> {
        (**self).write_once(bytes)
    }
    fn flush(&mut self) -> io::Result<()> {
        (**self).flush()
    }
    fn interrupted(&mut self) -> bool {
        (**self).interrupted()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Buffering {
    Buffered,
    Unbuffered,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct BlockingWrite {
    pub errno: i32,
    pub characters_written: usize,
}

/// TextIOWrapper の T と BufferedWriter の B を別々に持つ。
pub struct TextOutput<W> {
    pub sink: W,
    mode: Buffering,
    line_buffered: bool,
    text: Option<Vec<u8>>,
    binary: Vec<u8>,
    binary_start: usize,
    binary_end: usize,
    binary_capacity: usize,
    last_blocking: Option<BlockingWrite>,
    blocking_errno: i32,
}

impl<W: Sink> TextOutput<W> {
    pub fn new(sink: W, mode: Buffering, blocking_errno: i32) -> Self {
        Self::with_capacity(sink, mode, DEFAULT_BINARY_CAPACITY, blocking_errno)
    }

    pub fn with_capacity(
        sink: W,
        mode: Buffering,
        binary_capacity: usize,
        blocking_errno: i32,
    ) -> Self {
        assert!(binary_capacity > 0);
        Self {
            sink,
            mode,
            line_buffered: false,
            text: None,
            binary: Vec::new(),
            binary_start: 0,
            binary_end: 0,
            binary_capacity,
            last_blocking: None,
            blocking_errno,
        }
    }

    pub fn line_buffered(mut self, enabled: bool) -> Self {
        self.line_buffered = enabled;
        self
    }

    fn would_block(&mut self, characters_written: usize) -> io::Error {
        self.last_blocking = Some(BlockingWrite {
            errno: self.blocking_errno,
            characters_written,
        });
        io::Error::new(
            io::ErrorKind::WouldBlock,
            "write could not complete without blocking",
        )
    }

    /// BF。短い成功は B が空になるまで再送する。
    fn binary_flush(&mut self) -> io::Result<()> {
        while self.binary_start < self.binary_end {
            let pending = self.binary[self.binary_start..self.binary_end].to_vec();
            match self.sink.write_once(&pending)? {
                Some(0) => continue, // CPython は WriteZero を新設せず同じ BF を続ける。
                Some(written) => {
                    self.binary_start += written.min(self.binary_end - self.binary_start);
                    // 部分的な成功は、次に塞がる前に信号を調べる(旧 Python の BufferedWriter と同じ)。
                    if self.sink.interrupted() {
                        return Err(interrupted_error());
                    }
                }
                None => return Err(self.would_block(0)),
            }
        }
        self.binary_start = 0;
        self.binary_end = 0;
        self.binary.clear();
        Ok(())
    }

    /// BW。通常の raw 故障では新しい入力を B に残さない。
    fn binary_write(&mut self, bytes: &[u8]) -> io::Result<()> {
        if self.mode == Buffering::Unbuffered {
            let _ = self.sink.write_once(bytes)?;
            // 旧 Python は、短い成功の後も次の切れ目で届いた SIGINT を KeyboardInterrupt にする。
            if self.sink.interrupted() {
                return Err(interrupted_error());
            }
            return Ok(());
        }
        if bytes.len() <= self.binary_capacity - self.binary_end {
            self.binary.extend_from_slice(bytes);
            self.binary_end += bytes.len();
            return Ok(());
        }
        match self.binary_flush() {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                let remaining = self.binary[self.binary_start..self.binary_end].to_vec();
                self.binary.clear();
                self.binary.extend_from_slice(&remaining);
                self.binary_end -= self.binary_start;
                self.binary_start = 0;
                let accepted = (self.binary_capacity - self.binary_end).min(bytes.len());
                self.binary.extend_from_slice(&bytes[..accepted]);
                self.binary_end += accepted;
                return if accepted == bytes.len() {
                    Ok(())
                } else {
                    Err(self.would_block(accepted))
                };
            }
            Err(error) => return Err(error),
        }
        let mut offset = 0;
        while bytes.len() - offset > self.binary_capacity {
            match self.sink.write_once(&bytes[offset..])? {
                Some(written) => {
                    offset += written.min(bytes.len() - offset);
                    if self.sink.interrupted() {
                        return Err(interrupted_error());
                    }
                }
                None => {
                    let retained = self.binary_capacity.min(bytes.len() - offset);
                    self.binary.clear();
                    self.binary
                        .extend_from_slice(&bytes[offset..offset + retained]);
                    self.binary_start = 0;
                    self.binary_end = retained;
                    return Err(self.would_block(offset + retained));
                }
            }
        }
        self.binary.extend_from_slice(&bytes[offset..]);
        self.binary_end += bytes.len() - offset;
        Ok(())
    }

    /// TD。T を先に消してから下位 write を実行する。
    fn text_drain(&mut self) -> io::Result<()> {
        if let Some(bytes) = self.text.take() {
            self.binary_write(&bytes)?;
        }
        Ok(())
    }

    /// TW。bytes は UTF-8 + surrogateescape 済みとする。
    pub fn write(&mut self, bytes: &[u8]) -> io::Result<()> {
        let line = self.line_buffered && (bytes.contains(&b'\n') || bytes.contains(&b'\r'));
        match self.text.as_mut() {
            None => self.text = Some(bytes.to_vec()),
            Some(text) if text.len() + bytes.len() <= TEXT_CAPACITY => {
                text.extend_from_slice(bytes)
            }
            Some(_) => {
                self.text_drain()?;
                self.text = Some(bytes.to_vec());
            }
        }
        if self
            .text
            .as_ref()
            .is_some_and(|text| text.len() >= TEXT_CAPACITY)
            || self.mode == Buffering::Unbuffered
            || line
        {
            self.text_drain()?;
        }
        if line {
            self.binary_flush()?;
            self.sink.flush()?;
        }
        Ok(())
    }

    /// builtin print は本文と LF を別の TW へ渡す。
    pub fn print(&mut self, bytes: &[u8]) -> io::Result<()> {
        self.write(bytes)?;
        self.write(b"\n")
    }

    /// TF。TD 成功後だけ BF と raw flush を行う。
    pub fn flush(&mut self) -> io::Result<()> {
        self.text_drain()?;
        self.binary_flush()?;
        self.sink.flush()
    }

    /// ファイル実行の preflush。例外だけを消し、状態を戻さない。
    pub fn ignored_flush(&mut self) {
        let _ = self.flush();
    }

    /// Z1/Z2/close の各 flush を呼出側が順番に使う。
    pub fn finalize_flush(&mut self) -> bool {
        self.flush().is_err()
    }

    /// 通常の破棄(`TextIOWrapper.close` の後に `BufferedWriter.close`)。
    /// TextIOWrapper 側の flush が失敗しても、下位の BufferedWriter の flush を試みる。
    /// どちらかが失敗したら `true`。
    pub fn close(&mut self) -> bool {
        let text_failed = self.flush().is_err();
        let binary_failed = self.binary_flush().is_err();
        text_failed || binary_failed
    }

    pub fn last_blocking(&self) -> Option<BlockingWrite> {
        self.last_blocking
    }

    #[cfg(test)]
    fn pending_text_len(&self) -> Option<usize> {
        self.text.as_ref().map(Vec::len)
    }
    #[cfg(test)]
    fn pending_binary_len(&self) -> usize {
        self.binary_end - self.binary_start
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Clone, Debug)]
    enum Outcome {
        Write(usize),
        Error,
        Block,
    }
    struct Record {
        plan: Vec<Outcome>,
        writes: Vec<Vec<u8>>,
        flushes: usize,
    }
    impl Record {
        fn new(plan: impl IntoIterator<Item = Outcome>) -> Self {
            Self {
                plan: plan.into_iter().collect(),
                writes: vec![],
                flushes: 0,
            }
        }
    }
    impl Sink for Record {
        fn write_once(&mut self, bytes: &[u8]) -> io::Result<Option<usize>> {
            self.writes.push(bytes.to_vec());
            match self.plan.remove(0) {
                Outcome::Write(n) => Ok(Some(n.min(bytes.len()))),
                Outcome::Error => Err(io::Error::from(io::ErrorKind::StorageFull)),
                Outcome::Block => Ok(None),
            }
        }
        fn flush(&mut self) -> io::Result<()> {
            self.flushes += 1;
            Ok(())
        }
    }
    fn failures() -> Vec<Outcome> {
        vec![Outcome::Error; 16]
    }

    struct BudgetSink {
        remaining: usize,
        accepted: usize,
        accepted_bytes: Vec<u8>,
        writes: Vec<Vec<u8>>,
    }
    impl BudgetSink {
        fn new(remaining: usize) -> Self {
            Self {
                remaining,
                accepted: 0,
                accepted_bytes: Vec::new(),
                writes: Vec::new(),
            }
        }
    }
    impl Sink for BudgetSink {
        fn write_once(&mut self, bytes: &[u8]) -> io::Result<Option<usize>> {
            self.writes.push(bytes.to_vec());
            if self.remaining == 0 {
                return Err(io::Error::from(io::ErrorKind::StorageFull));
            }
            let accepted = self.remaining.min(bytes.len());
            self.remaining -= accepted;
            self.accepted += accepted;
            self.accepted_bytes.extend_from_slice(&bytes[..accepted]);
            Ok(Some(accepted))
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn print_body_and_lf_are_separate_unbuffered_writes() {
        let mut out = TextOutput::new(
            Record::new([Outcome::Write(1), Outcome::Write(1)]),
            Buffering::Unbuffered,
            11,
        );
        out.print(b"x").unwrap();
        assert_eq!(out.sink.writes, vec![b"x".to_vec(), b"\n".to_vec()]);
    }

    #[test]
    fn buffered_short_write_retries_but_unbuffered_does_not() {
        let mut buffered = TextOutput::new(
            Record::new([Outcome::Write(2), Outcome::Write(2)]),
            Buffering::Buffered,
            11,
        );
        buffered.write(b"abcd").unwrap();
        buffered.flush().unwrap();
        assert_eq!(buffered.sink.writes, vec![b"abcd".to_vec(), b"cd".to_vec()]);
        let mut raw = TextOutput::new(
            Record::new([Outcome::Write(1), Outcome::Write(1)]),
            Buffering::Unbuffered,
            11,
        );
        raw.print(b"abc").unwrap();
        assert_eq!(raw.sink.writes, vec![b"abc".to_vec(), b"\n".to_vec()]);
    }

    #[test]
    fn candidate_eight_boundaries() {
        let cases = [
            (4095, 120, 4095),
            (4096, 120, 4096),
            (4097, 0, 0),
            (8190, 0, 0),
            (8191, 0, 0),
            (8192, 1, 0),
            (8193, 1, 0),
            (65601, 1, 0),
        ];
        for (total, want_exit, want_binary) in cases {
            let mut out = TextOutput::new(Record::new(failures()), Buffering::Buffered, 11);
            let planned = if out.print(&vec![b'x'; total - 1]).is_err() {
                1
            } else {
                0
            };
            out.ignored_flush(); // F2
            let got_exit = if out.finalize_flush() { 120 } else { planned }; // Z1
            assert_eq!(got_exit, want_exit, "H={total}");
            assert_eq!(out.pending_binary_len(), want_binary, "H={total}");
        }
    }

    #[test]
    fn origin_six_boundaries() {
        let cases = [
            (8153, 120, 0),
            (8154, 9, 0),
            (8155, 9, 0),
            (8186, 120, 32),
            (8187, 120, 32),
            (65536, 120, 32),
        ];
        for (repo, want_exit, want_binary) in cases {
            let mut out = TextOutput::new(Record::new(failures()), Buffering::Buffered, 11);
            out.write(&vec![b'p'; 32]).unwrap();
            let planned = if out.print(&vec![b'r'; repo + 5]).is_err() {
                9
            } else {
                0
            };
            let got_exit = if out.finalize_flush() { 120 } else { planned }; // -c: no F2
            assert_eq!(got_exit, want_exit, "N={repo}");
            assert_eq!(out.pending_binary_len(), want_binary, "N={repo}");
        }
    }

    #[test]
    fn partial_write_budgets_accumulate_across_all_sixteen_cases() {
        for mode in [Buffering::Buffered, Buffering::Unbuffered] {
            for budget in [0, 32, 64, 60000] {
                let mut out = TextOutput::new(BudgetSink::new(budget), mode, 11);
                let planned = if out.print(&vec![b'x'; 65600]).is_err() {
                    1
                } else {
                    0
                };
                let final_exit = if out.finalize_flush() { 120 } else { planned };
                assert_eq!(out.sink.accepted, budget, "candidate {mode:?} {budget}");
                assert_eq!(
                    out.sink.accepted_bytes,
                    vec![b'x'; budget],
                    "candidate prefix {mode:?} {budget}"
                );
                assert_eq!(
                    out.sink.writes[0],
                    vec![b'x'; 65600],
                    "candidate first write {mode:?} {budget}"
                );
                if mode == Buffering::Unbuffered && budget > 0 {
                    assert_eq!(out.sink.writes[1], b"\n", "candidate LF {budget}");
                }
                let expected_writes = if mode == Buffering::Buffered && budget > 0 {
                    vec![vec![b'x'; 65600], vec![b'x'; 65600 - budget]]
                } else if mode == Buffering::Unbuffered && budget > 0 {
                    vec![vec![b'x'; 65600], b"\n".to_vec()]
                } else {
                    vec![vec![b'x'; 65600]]
                };
                assert_eq!(
                    out.sink.writes, expected_writes,
                    "candidate writes {mode:?} {budget}"
                );
                assert_eq!(planned, 1, "candidate {mode:?} {budget}");
                assert_eq!(final_exit, 1, "candidate {mode:?} {budget}");
            }
        }
        for mode in [Buffering::Buffered, Buffering::Unbuffered] {
            for budget in [0, 32, 64, 60000] {
                let mut out = TextOutput::new(BudgetSink::new(budget), mode, 11);
                let prefix_ok = (0..4).all(|_| out.print(b"1234567").is_ok());
                let repo_ok = prefix_ok && out.print(&vec![b'r'; 65541]).is_ok();
                let planned = if !repo_ok { 9 } else { 0 };
                let final_exit = if out.finalize_flush() { 120 } else { planned };
                assert_eq!(out.sink.accepted, budget, "origin {mode:?} {budget}");
                let mut prefix = b"1234567\n".repeat(4);
                if budget > 32 {
                    prefix.extend(std::iter::repeat_n(b'r', budget - 32));
                }
                prefix.truncate(budget);
                assert_eq!(
                    out.sink.accepted_bytes, prefix,
                    "origin prefix {mode:?} {budget}"
                );
                if mode == Buffering::Buffered && budget > 0 {
                    assert_eq!(
                        out.sink.writes[0],
                        b"1234567\n".repeat(4),
                        "origin initial B {budget}"
                    );
                    assert!(
                        out.sink.writes[1].iter().all(|byte| *byte == b'r'),
                        "origin repo {budget}"
                    );
                }
                let p = b"1234567\n".repeat(4);
                let expected_writes = match mode {
                    Buffering::Buffered if budget == 0 => vec![p.clone(), p.clone()],
                    Buffering::Buffered if budget == 32 => vec![p, vec![b'r'; 65541]],
                    Buffering::Buffered => {
                        vec![p, vec![b'r'; 65541], vec![b'r'; 65541 - (budget - 32)]]
                    }
                    Buffering::Unbuffered if budget == 0 => vec![b"1234567".to_vec()],
                    Buffering::Unbuffered => {
                        let mut writes = Vec::new();
                        for _ in 0..4 {
                            writes.push(b"1234567".to_vec());
                            writes.push(b"\n".to_vec());
                        }
                        writes.push(vec![b'r'; 65541]);
                        if budget > 32 {
                            writes.push(b"\n".to_vec());
                        }
                        writes
                    }
                };
                assert_eq!(
                    out.sink.writes, expected_writes,
                    "origin writes {mode:?} {budget}"
                );
                assert_eq!(planned, 9, "origin {mode:?} {budget}");
                let expected = if mode == Buffering::Buffered && budget == 0 {
                    120
                } else {
                    9
                };
                assert_eq!(final_exit, expected, "origin {mode:?} {budget}");
            }
        }
    }

    #[test]
    fn line_buffer_and_text_drain_failure_are_distinct() {
        let mut line = TextOutput::new(Record::new([Outcome::Write(2)]), Buffering::Buffered, 11)
            .line_buffered(true);
        line.write(b"x\n").unwrap();
        assert_eq!(line.sink.writes, vec![b"x\n".to_vec()]);
        assert_eq!(line.sink.flushes, 1);
        let mut out = TextOutput::new(Record::new([Outcome::Error]), Buffering::Buffered, 11);
        assert!(out.write(&vec![b'x'; TEXT_CAPACITY]).is_err());
        assert_eq!(out.pending_text_len(), None);
        assert_eq!(out.pending_binary_len(), 0);
    }

    #[test]
    fn blocking_flush_keeps_old_binary_and_accepts_free_space() {
        let mut out =
            TextOutput::with_capacity(Record::new([Outcome::Block]), Buffering::Buffered, 4, 11);
        out.write(b"abc").unwrap();
        out.write(&vec![b'd'; 8190]).unwrap();
        assert!(out.write(b"xyz").is_err());
        assert_eq!(out.pending_binary_len(), 4);
        assert_eq!(
            out.last_blocking(),
            Some(BlockingWrite {
                errno: out.blocking_errno,
                characters_written: 1,
            })
        );
        let mut mac =
            TextOutput::with_capacity(Record::new([Outcome::Block]), Buffering::Buffered, 4, 35);
        mac.write(b"abc").unwrap();
        mac.write(&vec![b'd'; 8190]).unwrap();
        assert!(mac.write(b"xyz").is_err());
        assert_eq!(mac.last_blocking().unwrap().errno, 35);
    }

    #[test]
    fn file_preflush_and_finalize_and_close_each_retry_remaining_binary() {
        let mut out = TextOutput::new(Record::new(failures()), Buffering::Buffered, 11);
        out.write(b"x").unwrap();
        out.ignored_flush(); // file F2: failure is discarded, B remains.
        assert!(out.finalize_flush()); // Z1 records the 120 override condition.
        assert!(out.finalize_flush()); // TextIOWrapper.close / BufferedWriter.close retry.
        assert_eq!(
            out.sink.writes,
            vec![b"x".to_vec(), b"x".to_vec(), b"x".to_vec()]
        );
        assert_eq!(out.pending_binary_len(), 1);
    }
}
