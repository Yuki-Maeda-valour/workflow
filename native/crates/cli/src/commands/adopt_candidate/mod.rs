//! R4Cの専用部分。製品CLIの登録表には接続しない。
//! 依存部品(#471 の候補解析・#472 の事前検査)は `flow` の trait の向こうにあり、実物への接続は両方のマージ後。
mod args;
#[allow(dead_code)]
#[path = "../r4b_support/bytes.rs"]
mod bytes;
mod candidate;
mod diagnostics;
mod flow;
#[cfg(test)]
mod flow_tests;
mod index;
mod inject;
#[cfg(test)]
mod inject_tests;
mod output;
mod paths;
mod plan;
mod program;
mod real;
mod runtime;
#[allow(dead_code)]
#[path = "../r4b_support/unicode.rs"]
mod unicode;

#[cfg(test)]
pub(crate) use candidate::{CandidateError, FileIdentity, HeldCandidate, verify_candidate_sha};
pub(crate) use flow::{Candidates, Child, Ops, Precheck, Problem};
#[cfg(test)]
pub(crate) use index::{FileMode, IndexError, parse_stage_zero, verify_index_unchanged};
pub(crate) use inject::{Injected, InjectedPrecheck};
pub(crate) use program::{Context, run};
pub(crate) use real::{ProcessEnv, RealOps};
pub(crate) use runtime::Standard;

#[cfg(test)]
mod tests {
    use super::*;

    const EMPTY_SHA256: &[u8] = b"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

    fn index_record(mode: &[u8], oid: &[u8], stage: &[u8], path: &[u8]) -> Vec<u8> {
        let mut value = Vec::new();
        value.extend_from_slice(mode);
        value.push(b' ');
        value.extend_from_slice(oid);
        value.push(b' ');
        value.extend_from_slice(stage);
        value.push(b'\t');
        value.extend_from_slice(path);
        value.push(0);
        value
    }

    #[test]
    fn candidate_sha_requires_exact_lowercase_shape_and_value() {
        assert_eq!(verify_candidate_sha(b"", EMPTY_SHA256), Ok(()));
        for invalid in [
            &EMPTY_SHA256[..63],
            b"E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855",
            b"g3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            b"03b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        ] {
            assert_eq!(
                verify_candidate_sha(b"", invalid),
                Err(CandidateError::DigestMismatch)
            );
        }
    }

    #[test]
    fn candidate_comparison_uses_body_and_dev_ino_only() {
        let identity = FileIdentity { dev: 7, ino: 11 };
        let held = HeldCandidate::capture(Vec::new(), identity, EMPTY_SHA256).unwrap();
        assert_eq!(held.verify_after(b"", identity), Ok(()));
        assert_eq!(
            held.verify_after(b"changed", identity),
            Err(CandidateError::Changed)
        );
        assert_eq!(
            held.verify_after(b"", FileIdentity { dev: 7, ino: 12 }),
            Err(CandidateError::Changed)
        );
        assert_eq!(held.body(), b"");
        assert_eq!(held.identity(), identity);
    }

    #[test]
    fn index_accepts_every_oid_length_from_40_through_64() {
        let path = b"candidate";
        for length in 40..=64 {
            let oid = vec![b'a'; length];
            let parsed =
                parse_stage_zero(true, &index_record(b"100644", &oid, b"0", path), path).unwrap();
            assert_eq!(parsed.mode(), FileMode::Regular);
            assert_eq!(parsed.oid(), oid);
            assert_eq!(parsed.path(), path);
        }
    }

    #[test]
    fn index_rejects_oid_lengths_outside_the_old_boundary() {
        let path = b"candidate";
        for length in [39, 65] {
            let oid = vec![b'a'; length];
            assert_eq!(
                parse_stage_zero(true, &index_record(b"100644", &oid, b"0", path), path),
                Err(IndexError::NotRegularStageZero)
            );
        }
        for oid in [
            b"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA".as_slice(),
            b"gggggggggggggggggggggggggggggggggggggggg".as_slice(),
        ] {
            assert_eq!(
                parse_stage_zero(true, &index_record(b"100644", oid, b"0", path), path),
                Err(IndexError::NotRegularStageZero)
            );
        }
    }

    #[test]
    fn index_requires_one_nul_terminated_record() {
        let path = b"candidate";
        let oid = vec![b'a'; 40];
        let valid = index_record(b"100644", &oid, b"0", path);
        assert_eq!(
            parse_stage_zero(false, &valid, path),
            Err(IndexError::Unavailable)
        );
        assert_eq!(
            parse_stage_zero(true, &valid[..valid.len() - 1], path),
            Err(IndexError::Unavailable)
        );
        let mut two = valid.clone();
        two.extend_from_slice(&valid);
        assert_eq!(
            parse_stage_zero(true, &two, path),
            Err(IndexError::Unavailable)
        );
        let mut extra_nul = valid;
        extra_nul.push(0);
        assert_eq!(
            parse_stage_zero(true, &extra_nul, path),
            Err(IndexError::Unavailable)
        );
        assert_eq!(
            parse_stage_zero(true, b"\0", path),
            Err(IndexError::NotUnique)
        );
    }

    #[test]
    fn index_requires_stage_zero_and_the_two_regular_modes() {
        let path = b"candidate";
        let oid = vec![b'a'; 40];
        for (raw, expected) in [
            (
                index_record(b"100644", &oid, b"0", path),
                Ok(FileMode::Regular),
            ),
            (
                index_record(b"100755", &oid, b"0", path),
                Ok(FileMode::Executable),
            ),
            (
                index_record(b"100644", &oid, b"1", path),
                Err(IndexError::NotRegularStageZero),
            ),
            (
                index_record(b"120000", &oid, b"0", path),
                Err(IndexError::NotRegularStageZero),
            ),
        ] {
            assert_eq!(
                parse_stage_zero(true, &raw, path).map(|entry| entry.mode()),
                expected
            );
        }
    }

    #[test]
    fn index_path_is_compared_as_os_bytes() {
        let path = b"raw-\xff\tline\nname";
        let oid = vec![b'a'; 40];
        let raw = index_record(b"100644", &oid, b"0", path);
        let parsed = parse_stage_zero(true, &raw, path).unwrap();
        assert_eq!(parsed.path(), path);
        assert_eq!(
            parse_stage_zero(true, &raw, b"raw-\xef\xbf\xbd\tline\nname"),
            Err(IndexError::NotRegularStageZero)
        );
    }

    #[test]
    fn malformed_index_fields_remain_distinct_from_invalid_values() {
        let path = b"candidate";
        for raw in [
            b"100644\tcandidate\0".as_slice(),
            b"100644 oid\t candidate\0".as_slice(),
            b"100644  oid 0\tcandidate\0".as_slice(),
        ] {
            assert_eq!(
                parse_stage_zero(true, raw, path),
                Err(IndexError::NotUnique)
            );
        }
    }

    #[test]
    fn partial_stage_keeps_worktree_body_and_index_blob_separate() {
        let current = b"staged body\nunstaged body\n";
        let digest = b"5173cebee79c5b89103b4c373e820cfca8c64fb37c6fe70fb9b37a0ec0b1f11a";
        let identity = FileIdentity { dev: 3, ino: 5 };
        let held_candidate = HeldCandidate::capture(current.to_vec(), identity, digest).unwrap();

        let source = b"candidate";
        let destination = b"renamed";
        let oid = vec![b'1'; 40];
        let held_index =
            parse_stage_zero(true, &index_record(b"100644", &oid, b"0", source), source).unwrap();
        let moved_index = parse_stage_zero(
            true,
            &index_record(b"100644", &oid, b"0", destination),
            destination,
        )
        .unwrap();

        assert_eq!(held_candidate.verify_after(current, identity), Ok(()));
        assert_eq!(verify_index_unchanged(&held_index, &moved_index), Ok(()));

        let changed_oid = vec![b'2'; 40];
        let changed = parse_stage_zero(
            true,
            &index_record(b"100644", &changed_oid, b"0", destination),
            destination,
        )
        .unwrap();
        assert_eq!(
            verify_index_unchanged(&held_index, &changed),
            Err(IndexError::Changed)
        );
        let changed_mode = parse_stage_zero(
            true,
            &index_record(b"100755", &oid, b"0", destination),
            destination,
        )
        .unwrap();
        assert_eq!(
            verify_index_unchanged(&held_index, &changed_mode),
            Err(IndexError::Changed)
        );
    }
}
