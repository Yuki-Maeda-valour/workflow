//! 同じ保持実体と呼出元の予算による読取り。失敗時に途中本文を返さない。
use crate::budget::{Budget, BudgetError};
use dev_workflow_platform::fs::{FsError, HeldFile, ReadSession};
use std::io;

#[derive(Debug)]
pub enum ReadError {
    Fs(FsError),
    Budget(BudgetError),
}
impl From<FsError> for ReadError {
    fn from(error: FsError) -> Self {
        Self::Fs(error)
    }
}
impl From<BudgetError> for ReadError {
    fn from(error: BudgetError) -> Self {
        Self::Budget(error)
    }
}

/// 最初の拒否も使用済みにする。再呼出しは OS と予算を参照しない。
pub fn read_verified(file: &mut HeldFile, budget: &mut Budget) -> Result<Vec<u8>, ReadError> {
    read_with(file, budget, |session, budget| {
        read_session(session, budget)
    })
}

fn read_with(
    file: &mut HeldFile,
    budget: &mut Budget,
    operation: impl FnOnce(&mut ReadSession<'_>, &mut Budget) -> Result<Vec<u8>, ReadError>,
) -> Result<Vec<u8>, ReadError> {
    let mut session = file.begin_read()?;
    operation(&mut session, budget)
}

// 公開する読取り経路を増やさず、短読・割込み・IO 異常の境界だけを試験で差し替える。
trait Reader {
    fn declared_size(&self) -> i128;
    fn read_chunk(&mut self, buffer: &mut [u8]) -> Result<usize, FsError>;
    fn verify_unchanged(&self) -> Result<(), FsError>;
}
impl Reader for ReadSession<'_> {
    fn declared_size(&self) -> i128 {
        self.opened_snapshot().size
    }
    fn read_chunk(&mut self, buffer: &mut [u8]) -> Result<usize, FsError> {
        self.read_chunk(buffer)
    }
    fn verify_unchanged(&self) -> Result<(), FsError> {
        self.verify_unchanged()
    }
}

fn read_session(reader: &mut impl Reader, budget: &mut Budget) -> Result<Vec<u8>, ReadError> {
    let declared = u64::try_from(reader.declared_size()).map_err(|_| FsError::InvalidInput)?;
    budget.check_file_bytes(declared)?;
    let mut result = Vec::new();
    let mut got = 0_u64;
    let mut buffer = [0; 65_536];
    loop {
        let count = match reader.read_chunk(&mut buffer) {
            Ok(count) => count,
            Err(FsError::Io(error)) if error.kind() == io::ErrorKind::Interrupted => {
                budget.check_time()?;
                continue;
            }
            Err(error) => return Err(error.into()),
        };
        if count == 0 {
            break;
        }
        budget.check_time()?;
        budget.add_bytes(count as u64)?;
        got = got.checked_add(count as u64).ok_or(BudgetError::Overflow)?;
        budget.check_file_bytes(got)?;
        result.extend_from_slice(&buffer[..count]);
    }
    reader.verify_unchanged()?;
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::budget::BudgetLimits;
    use dev_workflow_platform::fs::HeldDir;
    use std::collections::VecDeque;
    use std::ffi::OsStr;
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::{Duration, Instant};

    static NEXT: AtomicU64 = AtomicU64::new(0);
    struct Tree(PathBuf);
    impl Tree {
        fn new(bytes: &[u8]) -> (Self, HeldFile) {
            let path = std::env::temp_dir().join(format!(
                "c0b-core-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&path).unwrap();
            let tree = Self(path.canonicalize().unwrap());
            fs::write(tree.0.join("file"), bytes).unwrap();
            let parent = HeldDir::open_absolute(&tree.0).unwrap();
            fs::File::open(tree.0.join("file"))
                .unwrap()
                .set_times(
                    fs::FileTimes::new()
                        .set_modified(std::time::SystemTime::UNIX_EPOCH + Duration::from_secs(1)),
                )
                .unwrap();
            let before = parent.metadata(OsStr::new("file")).unwrap();
            let held = HeldFile::open_at(&parent, OsStr::new("file"), &before).unwrap();
            (tree, held)
        }
    }
    impl Drop for Tree {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.0).unwrap();
        }
    }
    fn budget(total: u64, one: u64) -> Budget {
        Budget::new(
            BudgetLimits {
                items: 10,
                total_bytes: total,
                file_bytes: one,
            },
            Instant::now() + Duration::from_secs(20),
        )
    }
    fn again(file: &mut HeldFile, b: &mut Budget) {
        let usage = (b.items(), b.bytes(), b.deadline());
        assert!(matches!(
            read_verified(file, b),
            Err(ReadError::Fs(FsError::AlreadyRead))
        ));
        assert_eq!((b.items(), b.bytes(), b.deadline()), usage);
    }

    #[test]
    fn raw_bytes_empty_and_multiblock_are_exact_without_item_count() {
        for bytes in [vec![], b"text\0\xff\xfe".to_vec(), vec![0xff; 131_073]] {
            let (_tree, mut file) = Tree::new(&bytes);
            let mut b = budget(200_000, 200_000);
            assert_eq!(read_verified(&mut file, &mut b).unwrap(), bytes);
            assert_eq!((b.items(), b.bytes()), (0, bytes.len() as u64));
            again(&mut file, &mut b);
        }
    }

    #[test]
    fn declaration_and_block_limits_keep_usage_and_consume_start() {
        let (_tree, mut file) = Tree::new(&vec![0; 65_537]);
        let mut b = budget(1, 100_000);
        b.add_items(1).unwrap();
        assert!(matches!(
            read_verified(&mut file, &mut b),
            Err(ReadError::Budget(BudgetError::TotalBytes))
        ));
        assert_eq!((b.items(), b.bytes()), (1, 65_536));
        again(&mut file, &mut b);
        let (_tree, mut file) = Tree::new(b"abc");
        let mut b = budget(10, 2);
        assert!(matches!(
            read_verified(&mut file, &mut b),
            Err(ReadError::Budget(BudgetError::FileBytes))
        ));
        assert_eq!(b.bytes(), 0);
        again(&mut file, &mut b);
    }

    #[test]
    fn raw_session_even_unread_prevents_budget_checks_and_reads() {
        for amount in [None, Some(0), Some(1), Some(10)] {
            let (_tree, mut file) = Tree::new(b"abc");
            {
                let mut session = file.begin_read().unwrap();
                if let Some(amount) = amount {
                    session.read_chunk(&mut vec![0; amount]).unwrap();
                    if amount == 10 {
                        assert_eq!(session.read_chunk(&mut [0; 10]).unwrap(), 0);
                    }
                }
            }
            // 期限切れも 0 の宣言上限も、AlreadyRead より先に検査しない。
            let mut b = Budget::new(
                BudgetLimits {
                    items: 0,
                    total_bytes: 0,
                    file_bytes: 0,
                },
                Instant::now() - Duration::from_secs(1),
            );
            again(&mut file, &mut b);
        }
    }

    #[test]
    fn same_budget_is_borrowed_for_two_files_and_expired_deadline() {
        let mut b = budget(100, 100);
        b.add_items(3).unwrap();
        b.add_bytes(7).unwrap();
        let deadline = b.deadline();
        for _ in 0..2 {
            let (_tree, mut file) = Tree::new(b"abc");
            b.add_items(1).unwrap();
            assert_eq!(read_verified(&mut file, &mut b).unwrap(), b"abc");
        }
        assert_eq!((b.items(), b.bytes(), b.deadline()), (5, 13, deadline));
        let (_tree, mut file) = Tree::new(b"x");
        let mut expired = Budget::new(b.limits(), Instant::now() - Duration::from_secs(1));
        assert!(matches!(
            read_verified(&mut file, &mut expired),
            Err(ReadError::Budget(BudgetError::Deadline))
        ));
        assert_eq!(expired.bytes(), 0);
        again(&mut file, &mut expired);
    }

    #[test]
    fn mutation_after_open_refuses_bytes_and_keeps_consumption() {
        for action in ["shrink", "grow", "overwrite", "mode", "mtime-restored"] {
            let (tree, mut file) = Tree::new(b"old");
            let path = tree.0.join("file");
            let modified = fs::metadata(&path).unwrap().modified().unwrap();
            let before = file.snapshot().unwrap();
            match action {
                "shrink" => fs::write(&path, b"x").unwrap(),
                "grow" => fs::write(&path, b"longer").unwrap(),
                "overwrite" | "mtime-restored" => fs::write(&path, b"new").unwrap(),
                "mode" => fs::set_permissions(&path, fs::Permissions::from_mode(0o400)).unwrap(),
                _ => unreachable!(),
            }
            if action == "mtime-restored" {
                let until = Instant::now() + Duration::from_secs(2);
                loop {
                    fs::File::open(&path)
                        .unwrap()
                        .set_times(fs::FileTimes::new().set_modified(modified))
                        .unwrap();
                    let after = file.snapshot().unwrap();
                    if (before.ctime_seconds, before.ctime_nanoseconds)
                        != (after.ctime_seconds, after.ctime_nanoseconds)
                    {
                        break;
                    }
                    assert!(Instant::now() < until, "ctime が変わらない入力環境です");
                }
            }
            let length = fs::metadata(&path).unwrap().len();
            let mut b = budget(100, 100);
            assert!(
                matches!(
                    read_verified(&mut file, &mut b),
                    Err(ReadError::Fs(FsError::Changed))
                ),
                "{action}"
            );
            assert_eq!(b.bytes(), length);
            again(&mut file, &mut b);
        }
    }

    struct FaultAtBoundary<'a, 'b> {
        session: &'a mut ReadSession<'b>,
        reads: usize,
        final_error: bool,
    }
    impl Reader for FaultAtBoundary<'_, '_> {
        fn declared_size(&self) -> i128 {
            self.session.opened_snapshot().size
        }
        fn read_chunk(&mut self, buffer: &mut [u8]) -> Result<usize, FsError> {
            self.reads += 1;
            if self.reads == 2 && !self.final_error {
                Err(FsError::Io(io::Error::from(io::ErrorKind::Other)))
            } else {
                self.session.read_chunk(buffer)
            }
        }
        fn verify_unchanged(&self) -> Result<(), FsError> {
            if self.final_error {
                Err(FsError::Io(io::Error::from(
                    io::ErrorKind::PermissionDenied,
                )))
            } else {
                self.session.verify_unchanged()
            }
        }
    }

    #[test]
    fn raw_read_error_also_prevents_verified_read() {
        let (_tree, mut file) = Tree::new(b"abc");
        {
            let mut session = file.begin_read().unwrap();
            let mut fault = FaultAtBoundary {
                session: &mut session,
                reads: 1,
                final_error: false,
            };
            assert!(matches!(fault.read_chunk(&mut [0; 8]), Err(FsError::Io(_))));
        }
        let mut b = Budget::new(
            BudgetLimits {
                items: 0,
                total_bytes: 0,
                file_bytes: 0,
            },
            Instant::now() - Duration::from_secs(1),
        );
        again(&mut file, &mut b);
    }

    #[test]
    fn io_failure_after_actual_read_and_final_stat_failure_consume_start() {
        for final_error in [false, true] {
            let (_tree, mut file) = Tree::new(b"abc");
            let mut b = budget(100, 100);
            let result = read_with(&mut file, &mut b, |session, budget| {
                read_session(
                    &mut FaultAtBoundary {
                        session,
                        reads: 0,
                        final_error,
                    },
                    budget,
                )
            });
            assert!(matches!(result, Err(ReadError::Fs(FsError::Io(_)))));
            assert_eq!(b.bytes(), 3);
            let prior = (b.items(), b.bytes(), b.deadline());
            let second = read_with(&mut file, &mut b, |_, _| {
                panic!("再呼出しで読取り境界へ到達した")
            });
            assert!(matches!(second, Err(ReadError::Fs(FsError::AlreadyRead))));
            assert_eq!((b.items(), b.bytes(), b.deadline()), prior);
            again(&mut file, &mut b);
        }
    }

    struct Fake {
        blocks: VecDeque<Result<Vec<u8>, io::ErrorKind>>,
        verify: Result<(), io::ErrorKind>,
        requested: Vec<usize>,
    }
    impl Reader for Fake {
        fn declared_size(&self) -> i128 {
            0
        }
        fn read_chunk(&mut self, buffer: &mut [u8]) -> Result<usize, FsError> {
            self.requested.push(buffer.len());
            match self.blocks.pop_front().expect("無期限の読取り") {
                Ok(bytes) => {
                    buffer[..bytes.len()].copy_from_slice(&bytes);
                    Ok(bytes.len())
                }
                Err(kind) => Err(FsError::Io(io::Error::from(kind))),
            }
        }
        fn verify_unchanged(&self) -> Result<(), FsError> {
            self.verify
                .map_err(|kind| FsError::Io(io::Error::from(kind)))
        }
    }
    fn fake(blocks: Vec<Result<Vec<u8>, io::ErrorKind>>) -> Fake {
        Fake {
            blocks: blocks.into(),
            verify: Ok(()),
            requested: vec![],
        }
    }

    #[test]
    fn short_reads_continue_with_full_buffer_and_no_declared_size_equality() {
        let mut reader = fake(vec![Ok(b"a".to_vec()), Ok(b"bc".to_vec()), Ok(vec![])]);
        let mut b = budget(3, 3);
        assert_eq!(read_session(&mut reader, &mut b).unwrap(), b"abc");
        assert_eq!(reader.requested, vec![65_536; 3]);
        assert_eq!(b.bytes(), 3);
    }

    #[test]
    fn interrupted_retries_only_within_deadline() {
        let mut reader = fake(vec![
            Err(io::ErrorKind::Interrupted),
            Ok(b"x".to_vec()),
            Ok(vec![]),
        ]);
        let mut b = budget(2, 2);
        assert_eq!(read_session(&mut reader, &mut b).unwrap(), b"x");
        assert_eq!(reader.requested, vec![65_536; 3]);
        let mut reader = fake(vec![Err(io::ErrorKind::Interrupted)]);
        let mut b = Budget::new(b.limits(), Instant::now() - Duration::from_secs(1));
        assert!(matches!(
            read_session(&mut reader, &mut b),
            Err(ReadError::Budget(BudgetError::Deadline))
        ));
        assert_eq!(reader.requested.len(), 1);
    }

    #[test]
    fn io_failures_and_actual_size_limit_never_return_partial_bytes() {
        for final_error in [false, true] {
            let mut reader = fake(vec![
                Ok(b"abc".to_vec()),
                if final_error {
                    Ok(vec![])
                } else {
                    Err(io::ErrorKind::Other)
                },
            ]);
            if final_error {
                reader.verify = Err(io::ErrorKind::PermissionDenied);
            }
            let mut b = budget(100, 100);
            assert!(matches!(
                read_session(&mut reader, &mut b),
                Err(ReadError::Fs(FsError::Io(_)))
            ));
            assert_eq!(b.bytes(), 3);
        }
        let mut reader = fake(vec![Ok(b"abc".to_vec())]);
        let mut b = budget(100, 2);
        assert!(matches!(
            read_session(&mut reader, &mut b),
            Err(ReadError::Budget(BudgetError::FileBytes))
        ));
        assert_eq!(b.bytes(), 3);
    }
}
