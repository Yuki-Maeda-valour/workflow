use super::bytes::joined;
use super::{Failure, Result};
#[derive(Clone, Debug)]
pub struct Args {
    pub cwd: Vec<u8>,
    pub base: Vec<u8>,
    pub out: Vec<u8>,
    pub globs: Vec<Vec<u8>>,
    pub eres: Vec<Vec<u8>>,
    pub refs: Vec<Vec<u8>>,
    pub union: Vec<u8>,
    pub union_given: bool,
    pub pre: Vec<u8>,
    pub pre_given: bool,
    pub pre_sha: Vec<u8>,
    pub pre_sha_given: bool,
    pub includes: Vec<Vec<u8>>,
    pub max_bytes: Vec<u8>,
    pub patch: Vec<u8>,
    pub patch_base: Vec<u8>,
    pub patch_base_given: bool,
    pub print: bool,
    pub precheck: bool,
    pub accept: Vec<u8>,
    pub accept_given: bool,
}
impl Default for Args {
    fn default() -> Self {
        Self {
            cwd: vec![],
            base: vec![],
            out: vec![],
            globs: vec![],
            eres: vec![],
            refs: vec![],
            union: vec![],
            union_given: false,
            pre: vec![],
            pre_given: false,
            pre_sha: vec![],
            pre_sha_given: false,
            includes: vec![],
            max_bytes: b"8388608".to_vec(),
            patch: vec![],
            patch_base: b"HEAD".to_vec(),
            patch_base_given: false,
            print: false,
            precheck: false,
            accept: vec![],
            accept_given: false,
        }
    }
}
pub const USAGE: &[u8] = include_bytes!("usage.txt");
impl Args {
    pub fn parse(input: Vec<Vec<u8>>) -> Result<Self> {
        let mut a = Self::default();
        let mut i = 0;
        while i < input.len() {
            let key = &input[i];
            match key.as_slice() {
                b"-h" | b"--help" => return Err(Failure::raw(0, USAGE.to_vec())),
                b"--print-exclude-ere" => {
                    a.print = true;
                    i += 1;
                    continue;
                }
                b"--precheck" => {
                    a.precheck = true;
                    i += 1;
                    continue;
                }
                b"--cwd"
                | b"--base"
                | b"--out"
                | b"--exclude-glob"
                | b"--exclude"
                | b"--secret-profile-ref"
                | b"--secret-profile-union-sha256"
                | b"--pre-untracked"
                | b"--pre-untracked-sha256"
                | b"--include-untracked"
                | b"--max-untracked-bytes"
                | b"--patch-out"
                | b"--patch-base"
                | b"--accept" => (),
                _ => {
                    return Err(Failure::raw(
                        2,
                        joined(&[
                            b"ERROR [usage] ",
                            "不明な引数: ".as_bytes(),
                            key,
                            b"\n",
                            USAGE,
                        ]),
                    ));
                }
            }
            if i + 1 == input.len() {
                return Err(Failure::raw(
                    2,
                    joined(&[
                        b"ERROR [usage] ",
                        key,
                        " に値が必要です\n".as_bytes(),
                        USAGE,
                    ]),
                ));
            }
            let v = input[i + 1].clone();
            i += 2;
            match key.as_slice() {
                b"--cwd" => a.cwd = v,
                b"--base" => a.base = v,
                b"--out" => a.out = v,
                b"--exclude-glob" => a.globs.push(v),
                b"--exclude" => a.eres.push(v),
                b"--secret-profile-ref" => a.refs.push(v),
                b"--secret-profile-union-sha256" => {
                    if a.union_given {
                        return Err(Failure::usage(
                            "--secret-profile-union-sha256 は 1 回だけ指定できる",
                        ));
                    }
                    a.union_given = true;
                    a.union = v;
                }
                b"--pre-untracked" => {
                    a.pre = v;
                    a.pre_given = true;
                }
                b"--pre-untracked-sha256" => {
                    a.pre_sha = v;
                    a.pre_sha_given = true;
                }
                b"--include-untracked" => a.includes.push(v),
                b"--max-untracked-bytes" => a.max_bytes = v,
                b"--patch-out" => a.patch = v,
                b"--patch-base" => {
                    a.patch_base = v;
                    a.patch_base_given = true;
                }
                b"--accept" => {
                    if a.accept_given {
                        return Err(Failure::usage("--accept は 1 回だけ指定できる"));
                    }
                    a.accept = v;
                    a.accept_given = true;
                }
                _ => unreachable!(),
            }
        }
        if a.print && a.precheck {
            return Err(Failure::usage(
                "--print-exclude-ere と --precheck は併用できない",
            ));
        }
        let forbidden = !a.base.is_empty()
            || !a.out.is_empty()
            || !a.patch.is_empty()
            || !a.pre.is_empty()
            || a.pre_sha_given
            || a.patch_base_given
            || !a.includes.is_empty()
            || !a.refs.is_empty()
            || !a.union.is_empty()
            || a.max_bytes != b"8388608";
        if a.print {
            if forbidden || !a.cwd.is_empty() || a.accept_given {
                return Err(Failure::usage(
                    "--print-exclude-ere は除外指定以外の引数と併用できない",
                ));
            }
        } else if a.precheck {
            if forbidden || !a.globs.is_empty() || !a.eres.is_empty() {
                return Err(Failure::usage(
                    "--precheck は --cwd と --accept 以外の引数と併用できない",
                ));
            }
            if a.cwd.is_empty() {
                return Err(Failure::usage("--cwd が必要です"));
            }
        } else {
            for (value, msg) in [
                (&a.cwd, "--cwd が必要です"),
                (&a.base, "--base が必要です"),
                (&a.out, "--out が必要です"),
            ] {
                if value.is_empty() {
                    return Err(Failure::usage(msg));
                }
            }
            if a.patch_base_given && a.patch.is_empty() {
                return Err(Failure::usage(
                    "--patch-base は --patch-out と対でのみ指定できる",
                ));
            }
            if a.pre_given != a.pre_sha_given {
                return Err(Failure::usage(
                    "--pre-untracked と --pre-untracked-sha256 は対で指定する",
                ));
            }
            if a.max_bytes.is_empty() || !a.max_bytes.iter().all(u8::is_ascii_digit) {
                return Err(Failure::usage("--max-untracked-bytes は非負の十進整数"));
            }
        }
        if a.union_given && a.refs.is_empty() {
            return Err(Failure::usage(
                "--secret-profile-union-sha256 には --secret-profile-ref が必要です",
            ));
        }
        if a.union_given
            && (a.union.len() != 64
                || !a
                    .union
                    .iter()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(b)))
        {
            return Err(Failure::usage(
                "--secret-profile-union-sha256 は完全な小文字 SHA-256 でなければならない",
            ));
        }
        if !a.precheck && a.globs.is_empty() && a.eres.is_empty() && a.refs.is_empty() {
            return Err(Failure::usage(
                "--exclude-glob か --exclude のどちらか 1 つ以上が必要です",
            ));
        }
        Ok(a)
    }
}
