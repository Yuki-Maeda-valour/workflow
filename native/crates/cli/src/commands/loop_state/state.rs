use super::{
    budget::{Budget, Limits},
    error::{LegacyException, Result, fs_reason},
    json::{self, PyValue},
    runtime,
};
use dev_workflow_platform::{
    fs::{FileSnapshot, HeldDir, HeldFile},
    loop_state as platform,
};
use sha2::{Digest, Sha256};
use std::{
    cmp::Ordering,
    ffi::{OsStr, OsString},
    path::{Path, PathBuf},
};
pub fn normalized(path: &Path) -> PathBuf {
    use std::os::unix::ffi::{OsStrExt, OsStringExt};
    let raw = path.as_os_str().as_bytes();
    let absolute = raw.starts_with(b"/");
    let double = raw.starts_with(b"//") && !raw.starts_with(b"///");
    let mut parts: Vec<&[u8]> = Vec::new();
    for part in raw.split(|c| *c == b'/') {
        if part.is_empty() || part == b"." {
            continue;
        }
        if part == b".." {
            if parts.last().is_some_and(|v| *v != b"..") {
                parts.pop();
            } else if !absolute {
                parts.push(part);
            }
        } else {
            parts.push(part);
        }
    }
    let mut value = if absolute {
        if double {
            b"//".to_vec()
        } else {
            b"/".to_vec()
        }
    } else {
        Vec::new()
    };
    for part in parts {
        if !value.is_empty() && !value.ends_with(b"/") {
            value.push(b'/');
        }
        value.extend(part);
    }
    if value.is_empty() {
        value.push(b'.');
    }
    PathBuf::from(OsString::from_vec(value))
}
pub fn absolute(path: &Path) -> Result<PathBuf> {
    Ok(normalized(&if path.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir()?.join(path)
    }))
}
/// posixpath.split(bytes)。head は最後の "/" まで(すべてが "/" でなければ末尾の "/" を除く)。
pub fn split_raw(raw: &[u8]) -> (&[u8], &[u8]) {
    let at = raw.iter().rposition(|c| *c == 47).map_or(0, |i| i + 1);
    let (mut head, tail) = raw.split_at(at);
    if !head.is_empty() && !head.iter().all(|c| *c == 47) {
        while head.ends_with(b"/") {
            head = &head[..head.len() - 1];
        }
    }
    (head, tail)
}
/// posixpath.dirname(bytes)。Path::parent と違い、末尾の "." や ".." の部分を落とさない。
pub fn dirname(path: &Path) -> PathBuf {
    use std::os::unix::ffi::{OsStrExt, OsStringExt};
    PathBuf::from(OsString::from_vec(
        split_raw(path.as_os_str().as_bytes()).0.to_vec(),
    ))
}
/// posixpath.join(a, b)(b が "/" で始まれば b)。
pub fn join_raw(head: &Path, tail: &Path) -> PathBuf {
    use std::os::unix::ffi::{OsStrExt, OsStringExt};
    let (a, b) = (head.as_os_str().as_bytes(), tail.as_os_str().as_bytes());
    if b.starts_with(b"/") {
        return tail.to_owned();
    }
    let mut out = a.to_vec();
    if !out.is_empty() && !out.ends_with(b"/") {
        out.push(b'/');
    }
    out.extend_from_slice(b);
    PathBuf::from(OsString::from_vec(out))
}
/// 旧 `parent_path, name = os.path.split(path); parent = open_dir_path(parent_path)`。
/// 現在のディレクトリへはつながない(相対なら open_dir_path と同じ Stop)。
pub fn raw_parent(path: &Path) -> Result<(HeldDir, OsString)> {
    use std::os::unix::ffi::{OsStrExt, OsStringExt};
    let (head, tail) = split_raw(path.as_os_str().as_bytes());
    Ok((
        directory(Path::new(OsStr::from_bytes(head)))?,
        OsString::from_vec(tail.to_vec()),
    ))
}
/// 保存状態の str のパスの文字番号(旧 os.path.abspath と同じく、相対なら現在のディレクトリへつなぐ)。
pub fn abspath_text(value: &super::json::Text) -> super::json::Text {
    let codes = value.as_codepoints();
    let mut joined = if codes.starts_with(&[47]) {
        Vec::new()
    } else {
        let mut cwd = std::env::current_dir()
            .map(|p| runtime::os_text(p.as_os_str()).as_codepoints().to_vec())
            .unwrap_or_else(|_| vec![46]);
        if !cwd.ends_with(&[47]) {
            cwd.push(47);
        }
        cwd
    };
    joined.extend_from_slice(codes);
    super::json::text(normpath_codes(&joined))
}
/// posixpath.dirname(str) を文字番号の上で行う(末尾の "." の部分を落とさない)。
pub fn dirname_codes(path: &[u32]) -> Vec<u32> {
    let at = path.iter().rposition(|c| *c == 47).map_or(0, |i| i + 1);
    let mut head = path[..at].to_vec();
    if !head.is_empty() && !head.iter().all(|c| *c == 47) {
        while head.ends_with(&[47]) {
            head.pop();
        }
    }
    head
}
/// posixpath.join(a, b) を文字番号の上で行う(b が "/" で始まれば b)。
pub fn join_codes(head: &[u32], tail: &[u32]) -> Vec<u32> {
    if tail.starts_with(&[47]) {
        return tail.to_vec();
    }
    let mut out = head.to_vec();
    if !out.is_empty() && !out.ends_with(&[47]) {
        out.push(47);
    }
    out.extend_from_slice(tail);
    out
}
/// posixpath.normpath を文字番号の上で行う(normalized と同じ規則)。
pub fn normpath_codes(path: &[u32]) -> Vec<u32> {
    if path.is_empty() {
        return vec![46];
    }
    let absolute = path.starts_with(&[47]);
    let double = path.starts_with(&[47, 47]) && !path.starts_with(&[47, 47, 47]);
    let mut parts: Vec<&[u32]> = Vec::new();
    for part in path.split(|c| *c == 47) {
        if part.is_empty() || part == [46] {
            continue;
        }
        if part == [46, 46] {
            if parts.last().is_some_and(|v| *v != [46, 46]) {
                parts.pop();
            } else if !absolute {
                parts.push(part);
            }
        } else {
            parts.push(part);
        }
    }
    let mut value = if absolute {
        if double { vec![47, 47] } else { vec![47] }
    } else {
        Vec::new()
    };
    for part in parts {
        if !value.is_empty() && !value.ends_with(&[47]) {
            value.push(47);
        }
        value.extend_from_slice(part);
    }
    if value.is_empty() {
        value.push(46);
    }
    value
}
fn encode_part(part: &[u32]) -> Result<Vec<u8>> {
    runtime::bytes(&super::json::text(part.to_vec())).map_err(|e| match e {
        LegacyException::Python { kind, reason, .. } => LegacyException::Python {
            kind,
            reason,
            location: "os.stat",
        },
        other => other,
    })
}
/// 保存状態の str のディレクトリのパスを、旧 open_dir_path と同じ所で OS のバイトへ変える。
/// 符号化できない部分があれば、その前までを実際に開いてから(旧版はそこまで stat・open する)、
/// その部分の os.stat の UnicodeEncodeError(214 行)にする。
pub fn encode_dir_path(path: &super::json::Text) -> Result<PathBuf> {
    use std::os::unix::ffi::OsStringExt;
    if let Ok(raw) = runtime::bytes(path) {
        return Ok(PathBuf::from(OsString::from_vec(raw)));
    }
    let codes = path.as_codepoints();
    if !codes.starts_with(&[47]) {
        return Err(LegacyException::stop(
            "絶対パスでないディレクトリは観察できない",
        ));
    }
    let mut prefix = b"/".to_vec();
    for part in codes.split(|c| *c == 47).filter(|p| !p.is_empty()) {
        match encode_part(part) {
            Ok(raw) => {
                if !prefix.ends_with(b"/") {
                    prefix.push(b'/');
                }
                prefix.extend(raw);
            }
            Err(error) => {
                drop(directory(Path::new(&OsString::from_vec(prefix)))?);
                return Err(error.trace(
                    "open_dir_path",
                    214,
                    "before = os.stat(name, dir_fd=fd, follow_symlinks=False)",
                ));
            }
        }
    }
    unreachable!("runtime::bytes が失敗したなら、どれかの部分も失敗する")
}
/// 旧 safe_config_file の `os.path.split(path)` と open_dir_path・葉の os.stat の順で、
/// 保存状態の str の config のパスを OS のバイトへ変える(721・725 行)。
pub fn encode_config_path(path: &super::json::Text) -> Result<PathBuf> {
    use std::os::unix::ffi::OsStringExt;
    if let Ok(raw) = runtime::bytes(path) {
        return Ok(PathBuf::from(OsString::from_vec(raw)));
    }
    let codes = path.as_codepoints();
    let at = codes.iter().rposition(|c| *c == 47).map_or(0, |i| i + 1);
    let mut head = codes[..at].to_vec();
    if !head.is_empty() && !head.iter().all(|c| *c == 47) {
        while head.ends_with(&[47]) {
            head.pop();
        }
    }
    let parent = encode_dir_path(&super::json::text(head)).map_err(|e| {
        e.at(
            "safe_config_file",
            721,
            "parent = open_dir_path(parent_path)",
        )
    })?;
    drop(directory(&parent).map_err(|e| {
        e.at(
            "safe_config_file",
            721,
            "parent = open_dir_path(parent_path)",
        )
    })?);
    Err(encode_part(&codes[at..]).unwrap_err().trace(
        "safe_config_file",
        725,
        "before = os.stat(name, dir_fd=parent, follow_symlinks=False)",
    ))
}
pub fn parent(path: &Path) -> Result<(HeldDir, OsString)> {
    raw_parent(&absolute(path)?)
}
/// 旧 open_dir_path(path)。相対(空を含む)は 209 行の Stop。NUL を含む部分は、その前までを
/// 開いてから os.stat の ValueError(214 行)にする。
pub fn directory(path: &Path) -> Result<HeldDir> {
    use std::os::unix::ffi::{OsStrExt, OsStringExt};
    let raw = path.as_os_str().as_bytes();
    if !raw.starts_with(b"/") {
        return Err(LegacyException::stop(
            "絶対パスでないディレクトリは観察できない",
        ));
    }
    if raw.contains(&0) {
        let mut prefix = b"/".to_vec();
        for part in raw.split(|c| *c == 47).filter(|p| !p.is_empty()) {
            if part.contains(&0) {
                drop(open_directory(Path::new(&OsString::from_vec(prefix)))?);
                return Err(LegacyException::Python {
                    kind: "ValueError",
                    reason: "embedded null byte".into(),
                    location: "os.stat",
                }
                .trace(
                    "open_dir_path",
                    214,
                    "before = os.stat(name, dir_fd=fd, follow_symlinks=False)",
                ));
            }
            if !prefix.ends_with(b"/") {
                prefix.push(b'/');
            }
            prefix.extend_from_slice(part);
        }
    }
    open_directory(path)
}
fn open_directory(path: &Path) -> Result<HeldDir> {
    use dev_workflow_platform::fs::FsError;
    use platform::DirectoryOperation as Operation;
    use std::os::unix::ffi::OsStrExt;
    platform::open_directory_detailed(path, super::observe::directory).map_err(|failure| {
        let platform::DirectoryError {
            error,
            name,
            operation,
        } = failure;
        if let FsError::Io(error) = error {
            let reason = super::error::reason(&error);
            let cause = LegacyException::Os {
                error,
                path: Some(name.as_bytes().to_vec()),
                location: "open_dir_path",
            };
            if matches!(operation, Operation::Root) {
                return cause.trace("open_dir_path", 210, r#"fd = os.open("/", DIR_FLAGS)"#);
            }
            let (line, source) = if matches!(operation, Operation::Stat) {
                (
                    214,
                    "before = os.stat(name, dir_fd=fd, follow_symlinks=False)",
                )
            } else {
                (215, "nxt = os.open(name, DIR_FLAGS, dir_fd=fd)")
            };
            LegacyException::stop(format!("親ディレクトリを安全に開けない: {reason}"))
                .trace(
                    "open_dir_path",
                    217,
                    r#"raise Stop(f"親ディレクトリを安全に開けない: {exc.strerror}") from exc"#,
                )
                .contextual(cause.trace("open_dir_path", line, source), true)
        } else if matches!(error, FsError::Changed | FsError::NotDirectory) {
            // 旧 `not S_ISDIR(before) or not same_directory_path(...)` はどちらも 220 行の Stop。
            LegacyException::stop("親ディレクトリが観察中に差し替わった").trace(
                "open_dir_path",
                220,
                r#"raise Stop("親ディレクトリが観察中に差し替わった")"#,
            )
        } else {
            LegacyException::stop(format!(
                "親ディレクトリを安全に開けない: {}",
                fs_reason(&error)
            ))
        }
    })
}

pub fn read_at(
    parent: &HeldDir,
    name: &OsStr,
    before: &FileSnapshot,
    budget: &mut Budget,
) -> Result<(HeldFile, Vec<u8>)> {
    budget.one(before.size.max(0) as u128)?;
    budget.tick(0)?;
    let mut file = super::observe::open(parent, name, before).map_err(|e|match e{dev_workflow_platform::fs::FsError::Io(error)=>LegacyException::Os{error,path:Some(std::os::unix::ffi::OsStrExt::as_bytes(name).to_vec()),location:"read_at.open"}.at("read_at",257,"fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, \"O_NOFOLLOW\", 0), dir_fd=parent)"),e=>e.into()})?;
    let raw = read_held(&mut file, budget)?;
    Ok((file, raw))
}
pub fn read_held(file: &mut HeldFile, budget: &mut Budget) -> Result<Vec<u8>> {
    read_held_with(file, budget, false)
}
pub fn read_held_with(file: &mut HeldFile, budget: &mut Budget, hash: bool) -> Result<Vec<u8>> {
    super::observe::budget("bytes", budget)?;
    let mut session = file.begin_read()?;
    let mut raw = Vec::new();
    let mut buffer = [0; 65536];
    loop {
        let n = session.read_chunk(&mut buffer).map_err(|e| {
            LegacyException::from(e).at(
                if hash { "hash_at" } else { "read_at" },
                if hash { 242 } else { 260 },
                "block = os.read(fd, 65536)",
            )
        })?;
        super::observe::read(session.opened_snapshot(), n).map_err(|e| {
            e.at(
                if hash { "hash_at" } else { "read_at" },
                if hash { 242 } else { 260 },
                "block = os.read(fd, 65536)",
            )
        })?;
        if n == 0 {
            break;
        }
        budget.read(n)?;
        budget.one((raw.len() + n) as u128)?;
        raw.extend_from_slice(&buffer[..n]);
    }
    session
        .verify_unchanged()
        .map_err(|_| LegacyException::stop("通常ファイルが読取中に変わった"))?;
    Ok(raw)
}
pub fn read_bytes(path: &Path, limits: &Limits) -> Result<Vec<u8>> {
    let (parent, name) = parent(path).map_err(|e| {
        e.at(
            "read_state_bytes",
            1714,
            "parent = open_dir_path(parent_path)",
        )
    })?;
    let before = parent.metadata(&name).map_err(|error| {
        let cause = LegacyException::from(error);
        let cause = match cause {
            LegacyException::Os { error, .. } => LegacyException::Os {
                error,
                path: Some(std::os::unix::ffi::OsStrExt::as_bytes(name.as_os_str()).to_vec()),
                location: "read_state_bytes.stat",
            },
            e => e,
        };
        LegacyException::stop("状態を読めない")
            .trace(
                "read_state_bytes",
                1719,
                "raise Stop(\"状態を読めない\") from exc",
            )
            .contextual(
                cause.trace(
                    "read_state_bytes",
                    1717,
                    "before = os.stat(name, dir_fd=parent, follow_symlinks=False)",
                ),
                true,
            )
    })?;
    if !before.is_regular() {
        return Err(LegacyException::stop("状態が通常ファイルでない").trace(
            "read_state_bytes",
            1721,
            r#"raise Stop("状態が通常ファイルでない")"#,
        ));
    }
    if limits.total.cmp_u128(before.size.max(0) as u128) == Ordering::Less {
        return Err(LegacyException::stop("状態の読取量が上限を超えた").trace(
            "read_state_bytes",
            1728,
            r#"raise Stop("状態の読取量が上限を超えた")"#,
        ));
    }
    let mut limits = limits.clone();
    if limits.total.compare(&limits.one) == Ordering::Greater {
        limits.one = limits.total.clone()
    }
    let mut budget = Budget::new(limits).map_err(|e| {
        e.at(
            "read_state_bytes",
            1732,
            "raw = read_at(parent, name, before, Budget(items, total, max(total, one), seconds))",
        )
    })?;
    Ok(read_at(&parent, &name, &before, &mut budget).map_err(|e|e.at("read_state_bytes",1732,"raw = read_at(parent, name, before, Budget(items, total, max(total, one), seconds))"))?.1)
}
pub fn read(path: &Path, limits: &Limits, int_limit: usize) -> Result<PyValue> {
    read_at_depth(path, limits, int_limit, 4)
}
pub fn read_at_depth(
    path: &Path,
    limits: &Limits,
    int_limit: usize,
    depth: usize,
) -> Result<PyValue> {
    let raw = read_bytes(path, limits).map_err(|e| {
        e.at(
            "read_state",
            1745,
            "raw = read_state_bytes(path, items, total, one, seconds)",
        )
    })?;
    let value = (if depth == 4 {
        json::load(&raw, int_limit)
    } else {
        json::load_at_depth(&raw, int_limit, depth)
    })
    .map_err(|e| match e {
        json::LoadError::Invalid(cause) => LegacyException::stop("状態を読めない")
            .trace(
                "read_state",
                1749,
                r#"raise Stop("状態を読めない") from exc"#,
            )
            .contextual(
                cause.trace("read_state", 1747, "value = json.loads(raw)"),
                true,
            ),
        json::LoadError::Recursion(reason, init) => {
            let error = LegacyException::Python {
                kind: "RecursionError",
                reason: reason.into(),
                location: "json.loads",
            };
            let error = if init {
                error.external(
                    "json/decoder.py",
                    "__init__",
                    32,
                    r"lineno = doc.count('\n', 0, pos) + 1",
                )
            } else {
                error
            };
            error
                .external(
                    "json/decoder.py",
                    "raw_decode",
                    353,
                    "obj, end = self.scan_once(s, idx)",
                )
                .external(
                    "json/decoder.py",
                    "decode",
                    337,
                    "obj, end = self.raw_decode(s, idx=_w(s, 0).end())",
                )
                .external(
                    "json/__init__.py",
                    "loads",
                    346,
                    "return _default_decoder.decode(s)",
                )
                .trace("read_state", 1747, "value = json.loads(raw)")
        }
    })?;
    if !value.is_dict() {
        return Err(LegacyException::stop("状態がオブジェクトでない").trace(
            "read_state",
            1751,
            r#"raise Stop("状態がオブジェクトでない")"#,
        ));
    }
    Ok(value)
}
pub fn hash(raw: &[u8]) -> String {
    format!("{:x}", Sha256::digest(raw))
}
pub fn digest(path: &Path, limits: &Limits) -> Result<String> {
    Ok(hash(&read_bytes(path, limits).map_err(|e| {
        e.at(
            "state_digest",
            1740,
            "return hashlib.sha256(read_state_bytes(path, items, total, one, seconds)).hexdigest()",
        )
    })?))
}
const WITH_LINE: &str = r#"with os.fdopen(fd, "w", encoding="utf-8") as fh:"#;
const DUMP_LINE: &str =
    r#"json.dump(value, fh, ensure_ascii=True, sort_keys=True, separators=(",", ":"))"#;
fn output_error(error: std::io::Error) -> LegacyException {
    LegacyException::os(error, "write_state")
}
/// write_state の with 文。本文の例外 E0 があれば、旧 close の 2 回の flush の
/// 失敗を「During handling」の連なりで E0 の後へつなぐ(textio.c・bufferedio.c の順)。
fn close_with(file: super::legacy_file::LegacyTextFile, body: Result<()>) -> Result<()> {
    let (text, buffer) = file.close();
    let raised = |error: std::io::Error| output_error(error).trace("write_state", 1766, WITH_LINE);
    match (body, text, buffer) {
        (body, None, None) => body,
        (Ok(()), Some(text), None) => Err(raised(text)),
        (Ok(()), None, Some(buffer)) => Err(raised(buffer)),
        (Ok(()), Some(text), Some(buffer)) => {
            Err(raised(buffer).contextual(output_error(text), false))
        }
        (Err(first), Some(text), None) => Err(raised(text).contextual(first, false)),
        (Err(first), None, Some(buffer)) => Err(raised(buffer).contextual(first, false)),
        (Err(first), Some(text), Some(buffer)) => {
            Err(raised(buffer).contextual(output_error(text).contextual(first, false), false))
        }
    }
}
pub fn write(path: &Path, value: &PyValue) -> Result<()> {
    let (parent, name) = parent(path)?;
    let mut temp = OsString::from(".");
    temp.push(&name);
    temp.push(format!(".tmp.{}", std::process::id()));
    let writer = platform::StateWriter::create(&parent, &temp)
        .map_err(|_| LegacyException::stop("状態の一時ファイルを安全に作れない"))?;
    let mut file = super::legacy_file::LegacyTextFile::new(writer);
    let body = (|| -> Result<()> {
        json::dump_stream(value, &mut |chunk| {
            // 保存の途中で受けた SIGINT も、旧版と同じく次の命令で KeyboardInterrupt にする。
            super::interrupt::check()?;
            file.write(chunk.as_bytes()).map_err(output_error)
        })
        .map_err(|e| e.trace("write_state", 1767, DUMP_LINE))?;
        file.flush()
            .map_err(|e| output_error(e).trace("write_state", 1768, "fh.flush()"))?;
        file.sync()
            .map_err(|e| output_error(e).trace("write_state", 1769, "os.fsync(fh.fileno())"))
    })();
    let result = close_with(file, body);
    if let Err(error) = result {
        // except Exception: KeyboardInterrupt は当たらないので、一時ファイルを残す。
        if !super::interrupt::is_interrupt(&error) {
            let _ = platform::unlink(&parent, &temp);
        }
        return Err(error);
    }
    platform::replace(&parent, &temp, &name)
        .map_err(|_| LegacyException::stop("状態を安全に置き換えられない"))?;
    Ok(())
}
pub fn schema(value: &PyValue) -> bool {
    value.get("schema") == &PyValue::from(7i128)
}
pub fn string_path(value: &PyValue) -> Result<PathBuf> {
    let s = value
        .string()
        .ok_or_else(|| LegacyException::stop("前の worktree の状態を読めない"))?;
    Ok(PathBuf::from(runtime::os(s)?))
}
