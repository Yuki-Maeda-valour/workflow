use super::*;
use dev_workflow_core::loop_state::{CodepointString, LegacyIntLimit};
use dev_workflow_core::s3d_profile::codec::strict_utf8;
use dev_workflow_core::s3d_profile::{SnapshotYamlPolicy, snapshot_patterns};
/// The reason the current profile could not be read, with the fixed `strerror`
/// table (Python's `exc.strerror` was glibc's, not the static musl text).
pub fn current_profile_error(error: &sfs::ProfileError) -> Failure {
    profile_error(&error.text(super::tools::strerror))
}
fn profile_error(message: &str) -> Failure {
    Failure::raw(
        20,
        joined(&[
            &legacy_text::diagnostic(&format!("ERROR [secret-profile] {message}\n")),
            "ERROR [internal] secret profile の和集合を作れない\n".as_bytes(),
        ]),
    )
}
fn git(s: &Snapshot, parts: Vec<Vec<u8>>) -> Result<Vec<u8>> {
    s.git.profile(&parts).map_err(|e| profile_error(&e))
}
fn trim(value: Vec<u8>) -> Vec<u8> {
    let start = value
        .iter()
        .position(|c| !c.is_ascii_whitespace())
        .unwrap_or(value.len());
    let end = value
        .iter()
        .rposition(|c| !c.is_ascii_whitespace())
        .map_or(start, |i| i + 1);
    value[start..end].to_vec()
}
fn patterns(raw: &[u8], source: &str) -> Result<Vec<CodepointString>> {
    let configured = legacy_text::integer_limit();
    let limit = LegacyIntLimit::from_python_value(configured)
        .unwrap_or_else(|_| LegacyIntLimit::from_python_value(4300).unwrap());
    snapshot_patterns(raw, SnapshotYamlPolicy::python_3_10(limit))
        .map_err(|e| profile_error(&format!("{source} {}", e.message())))
}
pub fn union(s: &mut Snapshot) -> Result<()> {
    if s.args.refs.is_empty() {
        return Ok(());
    }
    signal_checkpoint()?;
    // `python3 "$PROFILE_HELPER" …` (1226): its Git children see the shell's
    // export environment as it was when python3 was forked.
    process::fork(1226);
    let result = helper(s);
    // The old Bash parent waits for the whole Python helper, including all of
    // its Git children. A signal to that parent does not interrupt the helper,
    // and the trap runs before the helper's saved stderr would be copied.
    if dev_workflow_platform::diff_snapshot::signals::pending() {
        return Err(Failure::silent(20));
    }
    let (globs, union) = result?;
    if s.args.union_given && sha(&union) != s.args.union {
        return Err(Failure::internal(
            "secret profile の和集合が開始時から変わった",
        ));
    }
    s.args.globs.extend(globs);
    // `while IFS= read … profile_glob; …; done <secret-profile-globs.z` (1237).
    process::mark();
    s.ere = ere::build(&s.args.globs, &s.args.eres)?;
    Ok(())
}
fn helper(s: &Snapshot) -> Result<(Vec<Vec<u8>>, Vec<u8>)> {
    legacy_text::startup()?;
    if let Some(stderr) = helper_args::check(raw(s.root.as_os_str()), &s.args.refs) {
        return Err(Failure::raw(
            20,
            joined(&[
                &stderr,
                "ERROR [internal] secret profile の和集合を作れない\n".as_bytes(),
            ]),
        ));
    }
    let fmt = trim(git(s, words(&["rev-parse", "--show-object-format"]))?);
    let length = match fmt.as_slice() {
        b"sha1" => 40,
        b"sha256" => 64,
        _ => return Err(profile_error("object format を確認できない")),
    };
    let mut refs = vec![];
    for value in &s.args.refs {
        if value.len() != length
            || !value
                .iter()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
        {
            return Err(profile_error(
                "--secret-profile-ref は完全な小文字の OID でなければならない",
            ));
        }
        let empty = trim(git(s, words(&["hash-object", "-t", "tree", "/dev/null"]))?);
        if value == &empty {
            refs.push(None);
            continue;
        }
        let resolved = s
            .git
            .profile(
                &[
                    words(&["rev-parse", "--verify", "--quiet"]),
                    vec![joined(&[value, b"^{commit}"])],
                ]
                .concat(),
            )
            .map_err(|_| profile_error("--secret-profile-ref が commit に解決できない"))?;
        if trim(resolved) != *value {
            return Err(profile_error("--secret-profile-ref の OID が一致しない"));
        }
        refs.push(Some(value.clone()));
    }
    let mut values = [".env", ".env.*", ".dev.vars"]
        .into_iter()
        .map(CodepointString::from)
        .collect::<Vec<_>>();
    if let Some(raw) = sfs::current_profile(&s.root, &mut NativeOperations)
        .map_err(|e| current_profile_error(&e))?
    {
        values.extend(patterns(&raw, "現在")?);
    }
    for reference in refs.into_iter().flatten() {
        let listed = git(
            s,
            [
                words(&["ls-tree", "-z"]),
                vec![
                    reference.clone(),
                    b"--".to_vec(),
                    b".claude/project-profile.yml".to_vec(),
                ],
            ]
            .concat(),
        )?;
        if listed.is_empty() {
            continue;
        }
        if !listed.ends_with(&[0]) || listed.iter().filter(|b| **b == 0).count() != 1 {
            return Err(profile_error("基準 profile の形式を確認できない"));
        }
        let record = &listed[..listed.len() - 1];
        let Some(n) = record.iter().position(|b| *b == b'\t') else {
            return Err(profile_error("基準 profile が通常ファイルでない"));
        };
        let fields = record[..n]
            .split(|b| b.is_ascii_whitespace())
            .filter(|f| !f.is_empty())
            .collect::<Vec<_>>();
        if record[n + 1..] != *b".claude/project-profile.yml"
            || fields.len() != 3
            || !(fields[0] == b"100644" || fields[0] == b"100755")
            || fields[1] != b"blob"
            || ![40, 64].contains(&fields[2].len())
            || !fields[2]
                .iter()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b))
        {
            return Err(profile_error("基準 profile が通常ファイルでない"));
        }
        let oid = fields[2].to_vec();
        let size = trim(git(
            s,
            [words(&["cat-file", "-s"]), vec![oid.clone()]].concat(),
        )?);
        if size.is_empty() || size.len() > 10 || !size.iter().all(u8::is_ascii_digit) {
            return Err(profile_error("基準 profile の大きさを確認できない"));
        }
        let size = std::str::from_utf8(&size)
            .unwrap()
            .parse::<usize>()
            .map_err(|_| profile_error("基準 profile の大きさを確認できない"))?;
        if size > 1048576 {
            return Err(profile_error("基準 profile の大きさを確認できない"));
        }
        let raw = git(s, [words(&["cat-file", "blob"]), vec![oid]].concat())?;
        if raw.len() != size || raw.len() > 1048576 {
            return Err(profile_error("基準 profile の大きさが一致しない"));
        }
        let source = format!(
            "参照 {}",
            std::str::from_utf8(&reference).expect("checked OID")
        );
        values.extend(patterns(&raw, &source)?);
    }
    let mut seen = HashSet::new();
    let mut union = vec![];
    let mut globs = vec![];
    for value in values {
        if !seen.insert(value.clone()) {
            continue;
        }
        let bytes = strict_utf8(&value).map_err(|e| legacy_text::unicode_failure(&e.message()))?;
        union.extend(&bytes);
        union.push(0);
        globs.push(bytes);
    }
    Ok((globs, union))
}
