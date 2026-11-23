//! 保護領域の確かめ: `init_excl` :559-564、`check_state_dir` :644-671、`open_state` :2271-2280、
//! `verify_digests` :2264-2270、`snapshot_digest` :673-684、`state_fail` :2253-2262、`WORK` の作成
//! (`mktemp -d "$STATE/.work-XXXXXX"`)。
use super::paths;
use super::shell::{Exit, Flow, Part, Shell, Sub};
use super::sites;
use super::text::{path_field, quote};
use dev_workflow_platform::implement_guard as os;
use sha2::{Digest, Sha256};
use std::ffi::OsStr;
use std::fs;
use std::io::Read;
use std::os::unix::ffi::OsStrExt;
use std::path::Path;

/// 保護領域の候補を決める環境の値。
#[derive(Clone, Debug, Default)]
pub struct Places {
    pub xdg_state_home: Option<Vec<u8>>,
    pub home: Option<Vec<u8>>,
    pub tmpdir: Option<Vec<u8>>,
}

/// `init_excl` の結果。
#[derive(Clone, Debug, Default)]
pub struct Excluded {
    pub tmp: Vec<u8>,
    pub tmpdir: Vec<u8>,
}

/// `init_excl`。
pub fn init_excl(places: &Places) -> Excluded {
    let tmp = paths::phys_dir(b"/tmp").unwrap_or_default();
    let tmpdir = match places.tmpdir.as_deref() {
        Some(value) if !value.is_empty() => paths::phys_dir(value).unwrap_or_default(),
        _ => Vec::new(),
    };
    Excluded { tmp, tmpdir }
}

/// `under_dir`。
fn under(path: &[u8], parent: &[u8]) -> bool {
    if parent.is_empty() {
        return false;
    }
    path == parent || (path.starts_with(parent) && path.get(parent.len()) == Some(&b'/'))
}

/// `check_state_dir` の結果(`STATE` の実体パスと、子の環境の変数の値)。
#[derive(Clone, Debug, Default)]
pub struct Checked {
    pub state: Vec<u8>,
    /// 最後の `norm_rest` の結果(`NREST`)。
    pub nrest: Vec<u8>,
}

/// `check_state_dir`。
pub fn check_state_dir(
    value: &[u8],
    places: &Places,
    excluded: &Excluded,
) -> Result<Checked, Vec<u8>> {
    let mut nrest = Vec::new();
    let mut s = value.to_vec();
    while s != b"/" && s.last() == Some(&b'/') {
        s.pop();
    }
    if s.is_empty() {
        return Err(nrest);
    }
    if paths::is_link(&s) || !paths::is_dir(&s) {
        return Err(nrest);
    }
    let (dir, base) = paths::split_path(&s);
    if !(base.starts_with(b"guard-") && base.len() > b"guard-".len()) {
        return Err(nrest);
    }
    let Some(parent_real) = paths::phys_dir(&dir) else {
        return Err(nrest);
    };
    let home = places.home.clone().unwrap_or_default();
    let candidates = [
        places.xdg_state_home.clone().unwrap_or_default(),
        [home.as_slice(), b"/.local/state"].concat(),
        [home.as_slice(), b"/.cache"].concat(),
    ];
    for candidate in candidates {
        if candidate.is_empty() || candidate == b"/.local/state" || candidate == b"/.cache" {
            continue;
        }
        if candidate.first() != Some(&b'/') {
            continue;
        }
        let mut target = candidate.clone();
        target.extend_from_slice(b"/dev-workflow");
        let Some(real) = paths::resolve_future_path(&target, &mut nrest) else {
            continue;
        };
        if under(&real, &excluded.tmp) || under(&real, &excluded.tmpdir) {
            continue;
        }
        if parent_real == real {
            let mut state = real;
            state.push(b'/');
            state.extend_from_slice(&base);
            return Ok(Checked { state, nrest });
        }
    }
    Err(nrest)
}

/// `state_fail`: サブコマンドごとの失敗の出力。終了値 33。
pub fn state_fail(shell: &mut Shell, reason: &str, text: &[u8]) -> Exit {
    match state_fail_output(shell, reason, text) {
        Ok(()) => shell.finish(33),
        Err(exit) => exit,
    }
}

fn state_fail_output(shell: &mut Shell, reason: &str, text: &[u8]) -> Flow<()> {
    let sub = shell.sub;
    let mut line = Vec::new();
    match sub {
        Some(Sub::ConfigCheck) | Some(Sub::Compare) => {
            line.extend_from_slice(format!("ERROR [{reason}] ").as_bytes());
        }
        _ => line.extend_from_slice(b"ERROR [digest] "),
    }
    line.extend_from_slice(text);
    match sub {
        Some(Sub::ConfigCheck) => {
            shell.echo_err(sites::STATE_FAIL_CONFIG, &line)?;
            shell.printf(
                sites::STATE_FAIL_CONFIG,
                &[
                    Part::Text(b"RESULT=incomparable\nREASON="),
                    Part::Arg(reason.as_bytes()),
                    Part::Text(b"\nGIT_SKIPPED=yes\n"),
                ],
            )?;
        }
        Some(Sub::Compare) => {
            shell.echo_err(sites::STATE_FAIL_COMPARE, &line)?;
            emit_incomparable(shell, reason)?;
        }
        Some(Sub::TaskmdDiff) => {
            shell.echo_err(sites::STATE_FAIL_TASKMD, &line)?;
            shell.printf(sites::STATE_FAIL_TASKMD, &[Part::Text(b"REASON=digest\n")])?;
        }
        _ => {
            shell.echo_err(sites::STATE_FAIL_RESTORE, &line)?;
            shell.printf(
                sites::STATE_FAIL_RESTORE,
                &[Part::Text(
                    b"RESTORED=no\nTOUCHED=none\nTEMP=none\nREASON=digest\n",
                )],
            )?;
        }
    }
    Ok(())
}

/// `emit_incomparable` :2677-2686(S3I-A の経路では `GIT_USED=0`・`CHANGES` は空)。
fn emit_incomparable(shell: &mut Shell, reason: &str) -> Flow<()> {
    let gm: &[u8] = match reason {
        "hooks-changed" | "config-changed" => b"yes",
        _ => b"unknown",
    };
    shell.printf(
        sites::EMIT_INCOMPARABLE_HEAD,
        &[
            Part::Text(b"RESULT=incomparable\nWORKTREE_CHANGED=unknown\nGITMETA_CHANGED="),
            Part::Arg(gm),
            Part::Text(b"\n"),
        ],
    )?;
    shell.printf(
        sites::EMIT_INCOMPARABLE_REASON,
        &[
            Part::Text(b"REASON="),
            Part::Arg(reason.as_bytes()),
            Part::Text(b"\n"),
        ],
    )?;
    shell.printf(
        sites::EMIT_INCOMPARABLE_SKIPPED,
        &[Part::Text(b"GIT_SKIPPED=yes\n")],
    )?;
    Ok(())
}

/// ファイルの sha256(`sha256sum <file`。開けない・読めない・リンクを辿った先が読めなければ `None`)。
pub fn file_sha256(path: &[u8]) -> Option<String> {
    let mut file = fs::File::open(Path::new(OsStr::from_bytes(path))).ok()?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0u8; 65536];
    loop {
        match file.read(&mut buffer) {
            Ok(0) => break,
            Ok(n) => hasher.update(buffer.get(..n).unwrap_or_default()),
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => return None,
        }
    }
    Some(hex(&hasher.finalize()))
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// `snapshot_digest`。`.` で始まる名前を除いた `snapshot/*` を、C の地域設定の順に読む。
pub fn snapshot_digest(dir: &[u8]) -> Option<String> {
    let entries = fs::read_dir(Path::new(OsStr::from_bytes(dir))).ok()?;
    let mut names: Vec<Vec<u8>> = Vec::new();
    for entry in entries {
        let entry = entry.ok()?;
        let name = entry.file_name().as_bytes().to_vec();
        if name.first() == Some(&b'.') {
            continue;
        }
        names.push(name);
    }
    names.sort();
    let mut listing: Vec<u8> = Vec::new();
    for name in names {
        let mut full = dir.to_vec();
        full.push(b'/');
        full.extend_from_slice(&name);
        if !paths::exists(&full) && !paths::is_link(&full) {
            continue;
        }
        if paths::is_link(&full) || !paths::is_file(&full) {
            return None;
        }
        let digest = file_sha256(&full)?;
        listing.extend_from_slice(digest.as_bytes());
        listing.extend_from_slice(b"  ");
        listing.extend_from_slice(&name);
        listing.push(b'\n');
    }
    Some(hex(&Sha256::digest(&listing)))
}

/// `open_state` と `verify_digests`。合格なら `STATE` の実体パス。
pub fn open_state(
    shell: &mut Shell,
    value: &[u8],
    places: &Places,
    excluded: &Excluded,
    manifest_sha256: &[u8],
    snapshot_sha256: &[u8],
) -> Flow<Checked> {
    let checked = match check_state_dir(value, places, excluded) {
        Ok(checked) => checked,
        Err(_) => {
            let mut text = "--state が保護領域の guard-* でない・無い・symlink: "
                .as_bytes()
                .to_vec();
            text.extend_from_slice(&path_field(value));
            return Err(state_fail(shell, "state-missing", &text));
        }
    };
    let state = checked.state.clone();
    let mode = paths::mode_text(&state).unwrap_or_default();
    shell.tool_point("stat-mode-state")?;
    if mode != b"700" {
        let mut text = "--state のモードが 0700 でない: ".as_bytes().to_vec();
        if mode.is_empty() {
            text.extend_from_slice("不明".as_bytes());
        } else {
            text.extend_from_slice(&mode);
        }
        return Err(state_fail(shell, "state-missing", &text));
    }
    let manifest = [state.as_slice(), b"/manifest.tsv"].concat();
    let snapshot = [state.as_slice(), b"/snapshot"].concat();
    if paths::is_link(&manifest)
        || !paths::is_file(&manifest)
        || paths::is_link(&snapshot)
        || !paths::is_dir(&snapshot)
    {
        return Err(state_fail(
            shell,
            "state-missing",
            "--state に manifest.tsv / snapshot が無い".as_bytes(),
        ));
    }
    let digest = file_sha256(&manifest).unwrap_or_default();
    shell.tool_point("sha256-manifest")?;
    if digest.is_empty() || digest.as_bytes() != manifest_sha256.to_ascii_lowercase() {
        return Err(state_fail(
            shell,
            "manifest-digest",
            "マニフェストのダイジェストが一致しない(保護領域の記録を信頼できない)".as_bytes(),
        ));
    }
    let digest = snapshot_digest(&snapshot).unwrap_or_default();
    shell.tool_point("snapshot-digest")?;
    if digest.is_empty() || digest.as_bytes() != snapshot_sha256.to_ascii_lowercase() {
        return Err(state_fail(
            shell,
            "snapshot-digest",
            "スナップショット本体のダイジェストが一致しない(保護領域の記録を信頼できない)"
                .as_bytes(),
        ));
    }
    Ok(checked)
}

/// mktemp の名前の文字(gnulib の `tempname` と同じ 62 文字)。
const LETTERS: &[u8] = b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";

/// `WORK="$(mktemp -d "$STATE/.work-XXXXXX")" || fail_internal "保護領域に一時ディレクトリを作れない"`。
pub fn make_work(shell: &mut Shell, state: &[u8]) -> Flow<()> {
    let mut template = state.to_vec();
    template.extend_from_slice(b"/.work-XXXXXX");
    let injected = shell.point("fail:mktemp");
    let mut result: Result<Vec<u8>, i32> = Err(os::errno::EEXIST);
    if let Some(errno) = injected.filter(|e| *e > 0) {
        result = Err(errno);
    } else {
        for _ in 0..100 {
            let mut random = [0u8; 6];
            let _ = dev_workflow_platform::restore_taskmd::random_bytes(&mut random);
            let mut name = state.to_vec();
            name.extend_from_slice(b"/.work-");
            for byte in random {
                name.push(
                    *LETTERS
                        .get(usize::from(byte) % LETTERS.len())
                        .unwrap_or(&b'a'),
                );
            }
            let mut builder = fs::DirBuilder::new();
            std::os::unix::fs::DirBuilderExt::mode(&mut builder, 0o700);
            match builder.create(Path::new(OsStr::from_bytes(&name))) {
                Ok(()) => {
                    result = Ok(name);
                    break;
                }
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => {
                    result = Err(error.raw_os_error().unwrap_or(os::errno::EIO));
                    break;
                }
            }
        }
    }
    match result {
        Ok(name) => {
            shell.work = Some(name);
            shell.tool_point("mktemp")?;
            Ok(())
        }
        Err(errno) => {
            let mut text = b"failed to create directory via template ".to_vec();
            text.extend_from_slice(&quote(&template));
            shell.tool_error(b"mktemp", &text, Some(errno));
            shell.tool_point("mktemp")?;
            Err(fail_internal(
                shell,
                "保護領域に一時ディレクトリを作れない".as_bytes(),
            ))
        }
    }
}

/// `fail_internal` :330(`echo …; trap - ERR; exit 20`)。
pub fn fail_internal(shell: &mut Shell, text: &[u8]) -> Exit {
    let mut line = b"ERROR [internal] ".to_vec();
    line.extend_from_slice(text);
    match shell.echo_err(sites::FAIL_INTERNAL_ECHO, &line) {
        Ok(_) => shell.finish(20),
        Err(exit) => exit,
    }
}

/// `fail_usage` :329(`echo …; trap - ERR; exit 2`)。
pub fn fail_usage(shell: &mut Shell, text: &[u8]) -> Exit {
    let mut line = b"ERROR [usage] ".to_vec();
    line.extend_from_slice(text);
    match shell.echo_err(sites::FAIL_USAGE_ECHO, &line) {
        Ok(_) => shell.finish(2),
        Err(exit) => exit,
    }
}
