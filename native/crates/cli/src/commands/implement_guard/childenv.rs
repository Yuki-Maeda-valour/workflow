//! 子の環境(旧版の bash が `exec` で旧 helper へ渡し、旧 helper がそのまま cp へ渡す値の集合)。
//! bash 5.2.21 の `initialize_shell_variables` の取り込みと、旧版の export・unset・代入の結果を、
//! 基準の容器で測った規則で再現する(並びは比べない。2026-11-11 22:31 の決定)。
//!
//! - 取り込み: `=` の無い項目と名前の無い項目は捨てる。同じ名前は後の値。識別子でない名前も
//!   そのまま渡る(`invalid_env`)。
//! - bash が渡さない名前・値を決め直す名前・旧版のスクリプトの変数(受け継いだ名前と同じ名前の
//!   変数は、exec の時点の値で渡る。配列にした名前は渡らない)は下の表。
use super::paths;

/// 旧版の exec の行(`LINENO`)。
pub const EXEC_LINE: &[u8] = b"3034";
/// 基準の bash の版(00:35 の決定)。
pub const BASH_VERSION: &[u8] = b"5.2.21(1)-release";

/// bash が子へ渡さない名前(受け継いでも消える)。
const NOT_PASSED: &[&str] = &[
    "_",
    "BASHPID",
    "BASH_ARGV0",
    "BASH_COMMAND",
    "BASH_REMATCH",
    "BASH_SUBSHELL",
    "BASH_VERSINFO",
    "COMP_WORDBREAKS",
    "HISTCMD",
    "PPID",
    "PS1",
    "PS2",
    "RANDOM",
    "SRANDOM",
    // 旧版の unset(:216)。
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
];

/// 旧版が配列・連想配列にする名前(bash は配列を渡さない)。
const ARRAYS: &[&str] = &[
    "CHANGES",
    "CONFIG_EDGE_FROM",
    "CONFIG_EDGE_KEY",
    "CONFIG_EDGE_TO",
    "CONFIG_NODE",
    "CONFIG_QUEUE",
    "CONFIG_QUEUE_KIND",
    "CONFIG_ROOTS",
    "CONFIG_SIZE",
    "CTX_ACTIVE",
    "CTX_BY_WT",
    "CTX_CHILDREN",
    "CTX_COMMON",
    "CTX_DEPTH",
    "CTX_FORMAT",
    "CTX_GD",
    "CTX_HEAD",
    "CTX_HELD_BLOB",
    "CTX_HELD_HEAD",
    "CTX_MAIN_COST",
    "CTX_MHASH",
    "CTX_MOID",
    "CTX_MSIZE",
    "CTX_MSOURCE",
    "CTX_PAIR_COST",
    "CTX_PARENT",
    "CTX_REFS",
    "CTX_ROOT_LIST",
    "CTX_ROOT_PATHS",
    "CTX_ROOT_SEEN",
    "CTX_WT",
    "DIFF_CFG",
    "GIT_PRE",
    "GIT_PRE_HOOKS",
    "MADE_DIR",
    "META",
    "MODULE_NAME",
    "NEWMETA",
    "REPORTED",
    "TM",
];

/// 旧版のスクリプトの変数のうち、exec の時点で固定の値を持つもの。
const FIXED: &[(&str, &str)] = &[
    ("BOUNDED_SIZE", ""),
    ("CAND_MISSING_ACCEPTED", "0"),
    ("CAND_TARGET", ""),
    ("COLLECT_ERR", ""),
    ("COLLECT_UNMERGED", "0"),
    ("CONFIG_FILE_LIMIT", "16777216"),
    ("CONFIG_GIT_COST", "0"),
    ("CONFIG_LIMIT", "209715200"),
    ("CONFIG_ORIGIN_ERR", ""),
    ("CONFIG_READ_BYTES", "0"),
    ("CONFIG_UNKNOWN_ORIGIN", ""),
    ("CONFIG_XCODE_SELECTED", ""),
    ("FS_CONFIG_CHANGED", "0"),
    ("FS_HOOKS_CHANGED", "0"),
    ("GIT_USED", "0"),
    ("MAX_BYTES", "209715200"),
    ("MAX_ENTRIES", "2000"),
    ("META_CHANGED", "0"),
    ("NEW_MARK", ""),
    ("NEW_MARK_ID", ""),
    ("NEW_STATE", ""),
    (
        "NORM_SED",
        "s/^([[:space:]]*[-*+][[:space:]]+\\[)[xX](\\])/\\1 \\2/",
    ),
    ("ORIGIN_CHANGED", ""),
    ("ORIGIN_MODE", ""),
    ("OWNER_COMMON", ""),
    ("OWNER_COMMON_ID", ""),
    ("OWNER_MARK", "no"),
    ("OWNER_SKIP", ""),
    ("OWNER_TASKDIR", ""),
    ("OWNER_TASKDIR_ID", ""),
    ("OWNER_TMPDIR", ""),
    ("OWNER_TOP_ID", ""),
    ("OWN_MARK_STATE", ""),
    ("REVIEWS_DIR", ".claude/reviews"),
    ("ROW_KIND", ""),
    ("ROW_MODE", ""),
    ("ROW_VAL", ""),
    ("RUN_RC", ""),
    ("SELECT_COPY_BYTES", "0"),
    ("SELECT_CURRENT", "0"),
    ("SELECT_GENERATED", "0"),
    ("SELECT_MODE", "take"),
    ("SELECT_OBJECTS", ""),
    ("SELECT_OUTPUT_BYTES", "0"),
    ("SELECT_PATH_COUNT", "0"),
    ("SELECT_PRIVATE", ""),
    ("SELECT_SEQ", "0"),
    // 旧スクリプトのファイル名(`split_path "${BASH_SOURCE[0]}"` の末尾。追加の決定 4 で固定の値)。
    ("SP_BASE", "implement-guard.sh"),
    ("STATE_BASE", ""),
    ("STATE_TRIED", ""),
    ("SUB", "restore-taskmd"),
    ("TAKE_DONE", "0"),
    ("TASK_MD", ""),
    ("TOUCHED_STATE", "none"),
    ("WORK", ""),
    ("WT_CHANGED", "0"),
    ("extra", ""),
    ("kind", "T"),
    ("opt", "--snapshot-sha256"),
    #[cfg(not(target_os = "macos"))]
    ("SHA_CMD", "sha256sum"),
    #[cfg(not(target_os = "macos"))]
    ("STAT_FLAVOR", "gnu"),
    #[cfg(target_os = "macos")]
    ("SHA_CMD", "shasum"),
    #[cfg(target_os = "macos")]
    ("STAT_FLAVOR", "bsd"),
];

/// 旧スクリプトの置き場所か、旧版が探す `python3` の場所で値が決まる変数(2026-11-12 14:24 の
/// 追加の決定 4)。再現せず、受け継いだ値をそのまま子へ渡す(受け継いでいなければ渡さない)。
pub const NOT_REPRODUCED: &[&str] = &["CAP", "RP", "SP_DIR", "helper", "helper_dir", "python_path"];

/// exec の時点のスクリプトの変数の値(実行ごとに決まるもの)。
#[derive(Clone, Debug, Default)]
pub struct ScriptValues {
    pub values: Vec<(&'static str, Vec<u8>)>,
}

/// 起動時に決めた値(bash の起動の再現)。
#[derive(Clone, Debug, Default)]
pub struct Startup {
    /// 受け継いだ環境の項目(並びのまま)。
    pub raw: Vec<Vec<u8>>,
    /// 起動で決めた `SHLVL` の数(警告の後)。
    pub shell_level: i32,
    /// `PWD` の値(受け継いだ字面か、getcwd の物理パス。決められなければ無し)。
    pub pwd: Option<Vec<u8>>,
    /// bash の現在のフォルダ(`command -v` の `MP_DOCWD`)。
    pub cwd: Option<Vec<u8>>,
    /// `BASH` の値。
    pub bash: Vec<u8>,
    /// 実効の uid が 0 か(`PS4`)。
    pub root: bool,
}

/// 識別子か(`legal_identifier`)。
pub fn identifier(name: &[u8]) -> bool {
    match name.split_first() {
        Some((first, rest)) => {
            (first.is_ascii_alphabetic() || *first == b'_')
                && rest.iter().all(|b| b.is_ascii_alphanumeric() || *b == b'_')
        }
        None => false,
    }
}

/// 受け継いだ環境を、bash の取り込みの規則で (名前, 値) にする(同じ名前は後の値・最初の位置)。
pub fn imported(raw: &[Vec<u8>]) -> Vec<(Vec<u8>, Vec<u8>)> {
    let mut out: Vec<(Vec<u8>, Vec<u8>)> = Vec::new();
    for entry in raw {
        let Some(eq) = entry.iter().position(|b| *b == b'=') else {
            continue;
        };
        if eq == 0 {
            continue;
        }
        let name = entry.get(..eq).unwrap_or_default().to_vec();
        let value = entry.get(eq + 1..).unwrap_or_default().to_vec();
        match out.iter_mut().find(|(n, _)| *n == name) {
            Some(slot) => slot.1 = value,
            None => out.push((name, value)),
        }
    }
    out
}

/// 受け継いだ値(取り込みの後)。
pub fn lookup<'a>(imported: &'a [(Vec<u8>, Vec<u8>)], name: &str) -> Option<&'a [u8]> {
    imported
        .iter()
        .find(|(n, _)| n.as_slice() == name.as_bytes())
        .map(|(_, v)| v.as_slice())
}

/// bash の `legal_number`(前後の空白を許す十進。あふれは偽)。
pub fn legal_number(text: &[u8]) -> Option<i64> {
    let trimmed_start = text
        .iter()
        .position(|b| !matches!(b, b' ' | b'\t' | b'\n' | 11 | 12 | b'\r'))
        .unwrap_or(text.len());
    let mut rest = text.get(trimmed_start..).unwrap_or_default();
    let mut negative = false;
    if let Some((sign, tail)) = rest.split_first()
        && (*sign == b'+' || *sign == b'-')
    {
        negative = *sign == b'-';
        rest = tail;
    }
    let digits = rest.iter().take_while(|b| b.is_ascii_digit()).count();
    if digits == 0 {
        return None;
    }
    let mut value: i64 = 0;
    for byte in rest.get(..digits).unwrap_or_default() {
        let digit = i64::from(byte - b'0');
        value = if negative {
            value.checked_mul(10)?.checked_sub(digit)?
        } else {
            value.checked_mul(10)?.checked_add(digit)?
        };
    }
    let tail = rest.get(digits..).unwrap_or_default();
    if !tail.iter().all(|b| *b == b' ' || *b == b'\t') || text.is_empty() {
        return None;
    }
    Some(value)
}

/// `adjust_shell_level`: 受け継いだ値に `change` を足す(int へ切り詰め、負なら 0)。1000 以上なら
/// 警告の値と 1 を返す。
pub fn adjust_shell_level(old: Option<&[u8]>, change: i64) -> (i32, Option<i32>) {
    let old = old
        .and_then(|v| if v.is_empty() { None } else { legal_number(v) })
        .unwrap_or(0);
    let level = old.wrapping_add(change) as i32;
    if level < 0 {
        (0, None)
    } else if level >= 1000 {
        (1, Some(level))
    } else {
        (level, None)
    }
}

/// `PWD` の取り込み(`set_pwd`)。受け継いだ値が絶対パスで、現在のフォルダと同じ実体ならその字面、
/// そうでなければ getcwd の物理パス。getcwd が失敗したら、受け継いだ値があればそのまま残り、
/// bash は `shell-init: error retrieving current directory: …` を出す(2 つ目が `Some(errno)`)。
pub fn pwd_value(inherited: Option<&[u8]>) -> (Option<Vec<u8>>, Option<i32>) {
    if let Some(value) = inherited
        && value.first() == Some(&b'/')
        && same_file(value, b".")
    {
        return (Some(value.to_vec()), None);
    }
    match std::env::current_dir() {
        Ok(cwd) => {
            use std::os::unix::ffi::OsStrExt;
            (Some(cwd.as_os_str().as_bytes().to_vec()), None)
        }
        Err(error) => (
            inherited.map(<[u8]>::to_vec),
            Some(error.raw_os_error().unwrap_or(2)),
        ),
    }
}

fn same_file(a: &[u8], b: &[u8]) -> bool {
    match (paths::identity_follow(a), paths::identity_follow(b)) {
        (Some(x), Some(y)) => x == y,
        _ => false,
    }
}

/// `BASH`(`get_bash_name`。起動名は `bash`)。
pub fn bash_value(path_list: &[u8], cwd: Option<&[u8]>, login_shell: Vec<u8>) -> Vec<u8> {
    if !path_list.is_empty()
        && let Some(found) = paths::find_command(b"bash", path_list)
    {
        if found.first() == Some(&b'/') {
            return found;
        }
        return make_absolute(&found, cwd);
    }
    if paths::executable_file(b"bash") {
        return make_absolute(b"bash", cwd);
    }
    login_shell
}

/// `sh_makepath(NULL, file, MP_DOCWD|MP_RMDOT)`。
fn make_absolute(file: &[u8], cwd: Option<&[u8]>) -> Vec<u8> {
    let mut out = cwd.map(<[u8]>::to_vec).unwrap_or_else(|| b".".to_vec());
    if out.last() != Some(&b'/') {
        out.push(b'/');
    }
    out.extend_from_slice(file.strip_prefix(b"./").unwrap_or(file));
    out
}

/// exec の時点の子の環境(`名前=値` の列。並びは比べないので名前の順)。
pub fn child_environment(
    startup: &Startup,
    script: &ScriptValues,
    epoch: (u64, u32),
) -> Vec<Vec<u8>> {
    let imported = imported(&startup.raw);
    let mut out: Vec<(Vec<u8>, Vec<u8>)> = Vec::new();
    for (name, value) in &imported {
        let text = std::str::from_utf8(name).unwrap_or("");
        let valid = identifier(name);
        if valid && (NOT_PASSED.contains(&text) || ARRAYS.contains(&text)) {
            continue;
        }
        let mut value = value.clone();
        if valid {
            match text {
                "BASH" => value.clone_from(&startup.bash),
                "BASH_VERSION" => value = BASH_VERSION.to_vec(),
                "IFS" => value = b" \t\n".to_vec(),
                "LINENO" => value = EXEC_LINE.to_vec(),
                "OPTIND" | "OPTERR" => value = b"1".to_vec(),
                "EPOCHSECONDS" => value = epoch.0.to_string().into_bytes(),
                "EPOCHREALTIME" => {
                    value = format!("{}.{:06}", epoch.0, epoch.1).into_bytes();
                }
                "PS4" if startup.root => value = b"+ ".to_vec(),
                "OLDPWD" => {
                    if !paths::is_dir(&value) {
                        continue;
                    }
                }
                "PWD" | "SHLVL" | "LC_ALL" | "GIT_NO_LAZY_FETCH" | "GIT_OPTIONAL_LOCKS" => continue,
                // 追加の決定 4 の名前は、受け継いだ値のまま渡す。
                _ if NOT_REPRODUCED.contains(&text) => {}
                _ => {
                    if let Some((_, fixed)) = FIXED.iter().find(|(n, _)| *n == text) {
                        value = fixed.as_bytes().to_vec();
                    } else if let Some((_, own)) = script.values.iter().find(|(n, _)| *n == text) {
                        value.clone_from(own);
                    }
                }
            }
        }
        out.push((name.clone(), value));
    }
    // 旧版が export した値と、bash が必ず渡す値。
    let (level, _) = adjust_shell_level(Some(startup.shell_level.to_string().as_bytes()), -1);
    out.push((b"SHLVL".to_vec(), level.to_string().into_bytes()));
    if let Some(pwd) = &startup.pwd {
        out.push((b"PWD".to_vec(), pwd.clone()));
    }
    out.push((b"LC_ALL".to_vec(), b"C".to_vec()));
    out.push((b"GIT_NO_LAZY_FETCH".to_vec(), b"1".to_vec()));
    out.push((b"GIT_OPTIONAL_LOCKS".to_vec(), b"0".to_vec()));
    out.sort();
    out.into_iter()
        .map(|(mut name, value)| {
            name.push(b'=');
            name.extend_from_slice(&value);
            name
        })
        .collect()
}
