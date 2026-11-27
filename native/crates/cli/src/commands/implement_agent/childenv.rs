//! bash が子へ渡す環境(352 行の `grep`・390 行の `dirname`・391 行の `basename`。R4X-3 のランナーの
//! 起動でも使う)。bash 5.2.21 の `initialize_shell_variables` の取り込みと、旧版の代入・`unset`・
//! `set +o posix` の結果を、S3I-A の `childenv` と同じ規則で作る。表は `implement-agent.sh` 用
//! (並びは比べない。2026-11-11 22:31 の決定)。
//!
//! - 取り込み: `=` の無い項目と名前の無い項目は捨てる。同じ名前は後の値。識別子でない名前も
//!   そのまま渡る。
//! - bash が渡さない名前: 下の `NOT_PASSED`(`set +o posix` が外す `POSIXLY_CORRECT` と、107 行で
//!   `unset` する `CDPATH` を含む)。
//! - 呼出元が旧スクリプトの変数と同じ名前を export していたとき: 子を起動する時点のその変数の値を
//!   渡す(`ScriptValues`)。配列にした名前(`CMD`・`WRITE_TOKENS`・`PROBE_TOKENS`)は渡さない。
//!   値が旧版の置き場所や python3 の場所で決まる `LOG_SWAP_PY` は、2026-11-12 14:24 の決定のとおり、
//!   受け継いだ値をそのまま渡す。
//! - bash は、子の環境に `_=<起動したパス>` を足す。一時的な代入(352 行の `LC_ALL=C`)は、その子にだけ渡す。
use super::paths;

/// 基準の bash の版(00:35 の決定)。
pub const BASH_VERSION: &[u8] = b"5.2.21(1)-release";

/// bash が子へ渡さない名前(受け継いでも消える)。
const NOT_PASSED: &[&str] = &[
    "_",
    "BASHPID",
    "BASH_ARGV0",
    "BASH_COMMAND",
    "BASH_SUBSHELL",
    "BASH_VERSINFO",
    "COMP_WORDBREAKS",
    "HISTCMD",
    "PPID",
    "PS1",
    "PS2",
    "RANDOM",
    "SRANDOM",
    // 103 行の `set +o posix` と、107 行の `unset CDPATH`。
    "POSIXLY_CORRECT",
    "CDPATH",
];

/// 旧版が配列にする名前(bash は配列を渡さない)。
const ARRAYS: &[&str] = &["CMD", "WRITE_TOKENS", "PROBE_TOKENS"];

/// 値が旧版の置き場所か python3 の場所で決まる名前(2026-11-12 14:24 の決定)。受け継いだ値を
/// そのまま子へ渡す(受け継いでいなければ渡さない)。
pub const NOT_REPRODUCED: &[&str] = &["LOG_SWAP_PY"];

/// 子を起動する時点のスクリプトの変数の値。
#[derive(Clone, Debug, Default)]
pub struct ScriptValues {
    pub values: Vec<(&'static str, Vec<u8>)>,
}

impl ScriptValues {
    pub fn set(&mut self, name: &'static str, value: &[u8]) {
        match self.values.iter_mut().find(|(n, _)| *n == name) {
            Some(slot) => slot.1 = value.to_vec(),
            None => self.values.push((name, value.to_vec())),
        }
    }

    pub fn get(&self, name: &str) -> Option<&[u8]> {
        self.values
            .iter()
            .find(|(n, _)| *n == name)
            .map(|(_, v)| v.as_slice())
    }
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

/// 環境の最初の値(libc の `getenv`)。
pub fn first_value(raw: &[Vec<u8>], name: &[u8]) -> Option<Vec<u8>> {
    raw.iter().find_map(|entry| {
        let rest = entry.strip_prefix(name)?;
        rest.strip_prefix(b"=").map(<[u8]>::to_vec)
    })
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
    if !tail
        .iter()
        .all(|b| matches!(b, b' ' | b'\t' | b'\n' | 11 | 12 | b'\r'))
        || text.is_empty()
    {
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

/// `PWD` の取り込み(`set_pwd`)。受け継いだ値が絶対パスで、現在のフォルダと同じ実体ならその字面
/// (論理の cwd は `sh_canonpath` した値)、そうでなければ getcwd の物理パス。getcwd が失敗したら、
/// 受け継いだ値があればそのまま残り、論理の cwd は無し(3 つ目が `Some(errno)`)。
pub fn pwd_value(inherited: Option<&[u8]>) -> (Option<Vec<u8>>, Option<Vec<u8>>, Option<i32>) {
    if let Some(value) = inherited
        && value.first() == Some(&b'/')
        && same_file(value, b".")
    {
        let logical = paths::canonpath(value).or_else(|| paths::getcwd().ok());
        return (Some(value.to_vec()), logical, None);
    }
    match paths::getcwd() {
        Ok(cwd) => (Some(cwd.clone()), Some(cwd), None),
        Err(errno) => (inherited.map(<[u8]>::to_vec), None, Some(errno)),
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
        && let Some(found) = paths::search_for_command(b"bash", path_list, None)
        && found != b"bash"
    {
        if found.first() == Some(&b'/') {
            return found;
        }
        return make_absolute(&found, cwd);
    }
    if paths::is_file(b"bash") && paths::executable(b"bash") {
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

/// 子を起動する時点の環境(`名前=値` の列。並びは比べないので名前の順)。`line` はその命令の行
/// (`LINENO`)、`command` は起動するパス(`_`)、`temporary` は一時的な代入。
pub fn child_environment(
    startup: &Startup,
    script: &ScriptValues,
    line: u32,
    command: &[u8],
    temporary: &[(&str, &[u8])],
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
        if valid && temporary.iter().any(|(n, _)| *n == text) {
            continue;
        }
        let mut value = value.clone();
        if valid {
            match text {
                "BASH" => value.clone_from(&startup.bash),
                "BASH_VERSION" => value = BASH_VERSION.to_vec(),
                "IFS" => value = b" \t\n".to_vec(),
                "LINENO" => value = line.to_string().into_bytes(),
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
                "PWD" | "SHLVL" => continue,
                _ if NOT_REPRODUCED.contains(&text) => {}
                _ => {
                    if let Some(own) = script.get(text) {
                        value = own.to_vec();
                    }
                }
            }
        }
        out.push((name.clone(), value));
    }
    out.push((
        b"SHLVL".to_vec(),
        startup.shell_level.to_string().into_bytes(),
    ));
    if let Some(pwd) = &startup.pwd {
        out.push((b"PWD".to_vec(), pwd.clone()));
    }
    for (name, value) in temporary {
        out.push((name.as_bytes().to_vec(), value.to_vec()));
    }
    out.push((b"_".to_vec(), command.to_vec()));
    out.sort();
    out.into_iter()
        .map(|(mut name, value)| {
            name.push(b'=');
            name.extend_from_slice(&value);
            name
        })
        .collect()
}
