//! 390・391 行の `dirname "$LOG_FILE"`・`basename "$LOG_FILE"` を、GNU coreutils 9.4 と glibc 2.39 の
//! `getopt_long` の規則で再現する(選択肢の読み方・短縮・`=値`・`--help`・`--version` とその出力・
//! 誤りの文と、その訳)。地域設定の文字コードが UTF-8 でも ASCII でもないとき、または訳が UTF-8 で
//! ない翻訳ファイルにあるときは、`NeedTool` を返す(呼び出し側が道具を起動する。決定の表の (2))。
//! R4X-2 の `mktemp` の文も、同じ `ToolLocale` で作る。
use super::locale::{NeedTool, ToolLocale};

/// coreutils の版(00:35 の決定の基準)。
pub const COREUTILS_VERSION: &[u8] = b"9.4";
const PACKAGE_NAME: &[u8] = b"GNU coreutils";
const PACKAGE_URL: &[u8] = b"https://www.gnu.org/software/coreutils/";
const COPYRIGHT_YEAR: &[u8] = b"2023";

/// 道具の 1 回の実行の結果。
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ToolRun {
    /// stdout へ出した物(コマンド置換が受ける)。
    pub stdout: Vec<u8>,
    /// stderr へ 1 回ずつ書く単位(glibc の stderr は溜めない。`fprintf` 1 回が 1 回の write)。
    pub stderr: Vec<Vec<u8>>,
    /// 終了値。
    pub status: i32,
}

/// どちらの道具か。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tool {
    Dirname,
    Basename,
}

impl Tool {
    pub fn name(self) -> &'static [u8] {
        match self {
            Self::Dirname => b"dirname",
            Self::Basename => b"basename",
        }
    }
}

/// `printf` の書式に値を入れる(`%s`・`%c`・`%d`・`%%`・`%<n>$s`)。
pub fn format(template: &[u8], args: &[&[u8]]) -> Vec<u8> {
    let mut out = Vec::new();
    let mut next = 0usize;
    let mut index = 0usize;
    while let Some(&byte) = template.get(index) {
        if byte != b'%' {
            out.push(byte);
            index += 1;
            continue;
        }
        index += 1;
        if template.get(index) == Some(&b'%') {
            out.push(b'%');
            index += 1;
            continue;
        }
        // 位置の指定(`%2$s`)。
        let digits = template
            .get(index..)
            .unwrap_or_default()
            .iter()
            .take_while(|b| b.is_ascii_digit())
            .count();
        let mut position = None;
        if digits > 0 && template.get(index + digits) == Some(&b'$') {
            let text = std::str::from_utf8(template.get(index..index + digits).unwrap_or_default())
                .unwrap_or("0");
            position = text.parse::<usize>().ok().map(|n| n.saturating_sub(1));
            index += digits + 1;
        }
        let conversion = template.get(index).copied().unwrap_or(b's');
        index += 1;
        let slot = match position {
            Some(p) => p,
            None => {
                let p = next;
                next += 1;
                p
            }
        };
        let value = args.get(slot).copied().unwrap_or_default();
        match conversion {
            b's' | b'c' | b'd' => out.extend_from_slice(value),
            other => {
                out.push(b'%');
                out.push(other);
            }
        }
    }
    out
}

/// 長い選択肢。
struct LongOption {
    name: &'static [u8],
    /// 0: 値なし、1: 値が要る。
    has_arg: u8,
    /// 返す値(短い選択肢の字か、`HELP`・`VERSION`)。
    val: i32,
}

const HELP: i32 = -130;
const VERSION: i32 = -131;

fn long_options(tool: Tool) -> &'static [LongOption] {
    match tool {
        Tool::Dirname => &[
            LongOption {
                name: b"zero",
                has_arg: 0,
                val: b'z' as i32,
            },
            LongOption {
                name: b"help",
                has_arg: 0,
                val: HELP,
            },
            LongOption {
                name: b"version",
                has_arg: 0,
                val: VERSION,
            },
        ],
        Tool::Basename => &[
            LongOption {
                name: b"multiple",
                has_arg: 0,
                val: b'a' as i32,
            },
            LongOption {
                name: b"suffix",
                has_arg: 1,
                val: b's' as i32,
            },
            LongOption {
                name: b"zero",
                has_arg: 0,
                val: b'z' as i32,
            },
            LongOption {
                name: b"help",
                has_arg: 0,
                val: HELP,
            },
            LongOption {
                name: b"version",
                has_arg: 0,
                val: VERSION,
            },
        ],
    }
}

/// 短い選択肢の表(glibc は先頭の `+` を読み方の指定として外す)。
fn short_options(tool: Tool) -> &'static [u8] {
    match tool {
        Tool::Dirname => b"z",
        Tool::Basename => b"as:z",
    }
}

/// 道具の stderr と stdout を組み立てる。
struct Emit<'a> {
    locale: &'a mut ToolLocale,
    run: ToolRun,
    program: &'a [u8],
}

impl Emit<'_> {
    fn coreutils(&mut self, msgid: &[u8]) -> Result<Vec<u8>, NeedTool> {
        self.locale.gettext("coreutils", msgid)
    }

    fn libc(&mut self, msgid: &[u8]) -> Result<Vec<u8>, NeedTool> {
        self.locale.gettext("libc", msgid)
    }

    fn err(&mut self, piece: Vec<u8>) {
        self.run.stderr.push(piece);
    }

    fn out(&mut self, piece: &[u8]) {
        self.run.stdout.extend_from_slice(piece);
    }

    /// GNU の `error (0, 0, <文>)`: 名前と `: `、本文、改行を 1 回ずつ書く。
    fn error(&mut self, text: Vec<u8>) {
        let mut head = self.program.to_vec();
        head.extend_from_slice(b": ");
        self.err(head);
        self.err(text);
        self.err(b"\n".to_vec());
    }

    /// `usage (EXIT_FAILURE)` の `emit_try_help`。
    fn try_help(&mut self) -> Result<(), NeedTool> {
        let template = self.coreutils(b"Try '%s --help' for more information.\n")?;
        let program = self.program.to_vec();
        self.err(format(&template, &[&program]));
        self.run.status = 1;
        Ok(())
    }

    /// `emit_ancillary_info`。
    fn ancillary(&mut self, program: &[u8]) -> Result<(), NeedTool> {
        let template = self.coreutils(b"\n%s online help: <%s>\n")?;
        self.out(&format(&template, &[PACKAGE_NAME, PACKAGE_URL]));
        // `setlocale (LC_MESSAGES, nullptr)` が `en_` で始まるときは出さない。
        if !self.locale.messages.starts_with(b"en_") {
            let text = self.coreutils(
                b"Report any translation bugs to <https://translationproject.org/team/>\n",
            )?;
            self.out(&text);
        }
        let template = self.coreutils(b"Full documentation <%s%s>\n")?;
        self.out(&format(&template, &[PACKAGE_URL, program]));
        let template = self.coreutils(b"or available locally via: info '(coreutils) %s%s'\n")?;
        self.out(&format(&template, &[program, b" invocation"]));
        Ok(())
    }

    fn help_option(&mut self) -> Result<(), NeedTool> {
        let text = self.coreutils(b"      --help        display this help and exit\n")?;
        self.out(&text);
        let text = self.coreutils(b"      --version     output version information and exit\n")?;
        self.out(&text);
        Ok(())
    }

    /// `usage (EXIT_SUCCESS)`。
    fn usage(&mut self, tool: Tool) -> Result<(), NeedTool> {
        let program = self.program.to_vec();
        match tool {
            Tool::Dirname => {
                let template = self.coreutils(b"Usage: %s [OPTION] NAME...\n")?;
                self.out(&format(&template, &[&program]));
                let text = self.coreutils(b"Output each NAME with its last non-slash component and trailing slashes\nremoved; if NAME contains no /'s, output '.' (meaning the current directory).\n\n")?;
                self.out(&text);
                let text = self
                    .coreutils(b"  -z, --zero     end each output line with NUL, not newline\n")?;
                self.out(&text);
                self.help_option()?;
                let template = self.coreutils(b"\nExamples:\n  %s /usr/bin/          -> \"/usr\"\n  %s dir1/str dir2/str  -> \"dir1\" followed by \"dir2\"\n  %s stdio.h            -> \".\"\n")?;
                self.out(&format(&template, &[&program, &program, &program]));
                self.ancillary(b"dirname")?;
            }
            Tool::Basename => {
                let template =
                    self.coreutils(b"Usage: %s NAME [SUFFIX]\n  or:  %s OPTION... NAME...\n")?;
                self.out(&format(&template, &[&program, &program]));
                let text = self.coreutils(b"Print NAME with any leading directory components removed.\nIf specified, also remove a trailing SUFFIX.\n")?;
                self.out(&text);
                let text = self.coreutils(
                    b"\nMandatory arguments to long options are mandatory for short options too.\n",
                )?;
                self.out(&text);
                let text = self.coreutils(b"  -a, --multiple       support multiple arguments and treat each as a NAME\n  -s, --suffix=SUFFIX  remove a trailing SUFFIX; implies -a\n  -z, --zero           end each output line with NUL, not newline\n")?;
                self.out(&text);
                self.help_option()?;
                let template = self.coreutils(b"\nExamples:\n  %s /usr/bin/sort          -> \"sort\"\n  %s include/stdio.h .h     -> \"stdio\"\n  %s -s .h include/stdio.h  -> \"stdio\"\n  %s -a any/str1 any/str2   -> \"str1\" followed by \"str2\"\n")?;
                self.out(&format(
                    &template,
                    &[&program, &program, &program, &program],
                ));
                self.ancillary(b"basename")?;
            }
        }
        self.run.status = 0;
        Ok(())
    }

    /// `version_etc`。
    fn version(&mut self, tool: Tool) -> Result<(), NeedTool> {
        let mut head = tool.name().to_vec();
        head.extend_from_slice(b" (");
        head.extend_from_slice(PACKAGE_NAME);
        head.extend_from_slice(b") ");
        head.extend_from_slice(COREUTILS_VERSION);
        head.push(b'\n');
        self.out(&head);
        let symbol = self.coreutils(b"(C)")?;
        self.out(&format(
            b"Copyright %s %d Free Software Foundation, Inc.",
            &[&symbol, COPYRIGHT_YEAR],
        ));
        self.out(b"\n");
        let template = self.coreutils(b"License GPLv3+: GNU GPL version 3 or later <%s>.\nThis is free software: you are free to change and redistribute it.\nThere is NO WARRANTY, to the extent permitted by law.\n")?;
        self.out(&format(&template, &[b"https://gnu.org/licenses/gpl.html"]));
        self.out(b"\n");
        // proper_name_lite: 訳があれば訳。
        let first = self.coreutils(b"David MacKenzie")?;
        match tool {
            Tool::Dirname => {
                let second = self.coreutils(b"Jim Meyering")?;
                let template = self.coreutils(b"Written by %s and %s.\n")?;
                self.out(&format(&template, &[&first, &second]));
            }
            Tool::Basename => {
                let template = self.coreutils(b"Written by %s.\n")?;
                self.out(&format(&template, &[&first]));
            }
        }
        self.run.status = 0;
        Ok(())
    }
}

/// getopt の結果。
enum Got {
    /// 選択肢の値(短い字か `HELP`・`VERSION`)と、値の要る選択肢の値。
    Option(i32, Option<Vec<u8>>),
    /// 誤り(文は書いた)。
    Error,
    /// 選択肢の終わり(残りの位置)。
    End,
}

/// glibc 2.39 の `getopt_long`(引数は 1 つ)の 1 回分。`state` は (optind, 次に読む短い字の位置)。
fn getopt(
    emit: &mut Emit<'_>,
    tool: Tool,
    args: &[&[u8]],
    state: &mut (usize, usize),
) -> Result<Got, NeedTool> {
    let program = emit.program.to_vec();
    let (optind, nextchar) = *state;
    if nextchar == 0 {
        let Some(&arg) = args.get(optind) else {
            return Ok(Got::End);
        };
        if arg == b"--" {
            state.0 += 1;
            return Ok(Got::End);
        }
        if arg.first() != Some(&b'-') || arg.len() == 1 {
            // 選択肢でない(1 つだけなので、並べ替えても同じ)。
            return Ok(Got::End);
        }
        if arg.get(1) == Some(&b'-') {
            // 長い選択肢。
            let rest = arg.get(2..).unwrap_or_default();
            let name_len = rest.iter().position(|b| *b == b'=').unwrap_or(rest.len());
            let name = rest.get(..name_len).unwrap_or_default();
            let options = long_options(tool);
            state.0 += 1;
            let mut found: Option<&LongOption> = options.iter().find(|option| option.name == name);
            if found.is_none() {
                let matches: Vec<&LongOption> = options
                    .iter()
                    .filter(|option| option.name.starts_with(name))
                    .collect();
                if let Some(first) = matches.first() {
                    let ambiguous = matches
                        .iter()
                        .any(|option| option.has_arg != first.has_arg || option.val != first.val);
                    if ambiguous {
                        let template =
                            emit.libc(b"%s: option '%s%s' is ambiguous; possibilities:")?;
                        emit.err(format(&template, &[&program, b"--", rest]));
                        for option in &matches {
                            emit.err(format(b" '%s%s'", &[b"--", option.name]));
                        }
                        emit.err(b"\n".to_vec());
                        return Ok(Got::Error);
                    }
                    found = Some(first);
                }
            }
            let Some(option) = found else {
                let template = emit.libc(b"%s: unrecognized option '%s%s'\n")?;
                emit.err(format(&template, &[&program, b"--", rest]));
                return Ok(Got::Error);
            };
            let value = if name_len < rest.len() {
                let value = rest.get(name_len + 1..).unwrap_or_default().to_vec();
                if option.has_arg == 0 {
                    let template = emit.libc(b"%s: option '%s%s' doesn't allow an argument\n")?;
                    emit.err(format(&template, &[&program, b"--", option.name]));
                    return Ok(Got::Error);
                }
                Some(value)
            } else if option.has_arg == 1 {
                match args.get(state.0) {
                    Some(value) => {
                        state.0 += 1;
                        Some(value.to_vec())
                    }
                    None => {
                        let template = emit.libc(b"%s: option '%s%s' requires an argument\n")?;
                        emit.err(format(&template, &[&program, b"--", option.name]));
                        return Ok(Got::Error);
                    }
                }
            } else {
                None
            };
            return Ok(Got::Option(option.val, value));
        }
        state.1 = 1;
    }
    // 短い選択肢。
    let Some(&arg) = args.get(state.0) else {
        return Ok(Got::End);
    };
    let Some(&ch) = arg.get(state.1) else {
        state.0 += 1;
        state.1 = 0;
        return getopt(emit, tool, args, state);
    };
    state.1 += 1;
    let at_end = state.1 >= arg.len();
    if at_end {
        state.0 += 1;
        state.1 = 0;
    }
    let table = short_options(tool);
    let position = table.iter().position(|b| *b == ch);
    let Some(position) = position.filter(|_| ch != b':' && ch != b';') else {
        let template = emit.libc(b"%s: invalid option -- '%c'\n")?;
        emit.err(format(&template, &[&program, &[ch]]));
        return Ok(Got::Error);
    };
    if table.get(position + 1) == Some(&b':') {
        // 値の要る選択肢。
        if !at_end {
            let value = arg.get(state.1..).unwrap_or_default().to_vec();
            state.0 += 1;
            state.1 = 0;
            return Ok(Got::Option(i32::from(ch), Some(value)));
        }
        match args.get(state.0) {
            Some(value) => {
                state.0 += 1;
                return Ok(Got::Option(i32::from(ch), Some(value.to_vec())));
            }
            None => {
                let template = emit.libc(b"%s: option requires an argument -- '%c'\n")?;
                emit.err(format(&template, &[&program, &[ch]]));
                return Ok(Got::Error);
            }
        }
    }
    Ok(Got::Option(i32::from(ch), None))
}

/// gnulib の `last_component`。
fn last_component(name: &[u8]) -> usize {
    let mut base = name.iter().position(|b| *b != b'/').unwrap_or(name.len());
    let mut last_was_slash = false;
    let mut index = base;
    while let Some(&byte) = name.get(index) {
        if byte == b'/' {
            last_was_slash = true;
        } else if last_was_slash {
            base = index;
            last_was_slash = false;
        }
        index += 1;
    }
    base
}

/// gnulib の `dir_len`(`//` を別の根としない)。
pub fn dir_len(name: &[u8]) -> usize {
    let prefix = usize::from(name.first() == Some(&b'/'));
    let mut length = last_component(name);
    while prefix < length {
        if name.get(length - 1) != Some(&b'/') {
            break;
        }
        length -= 1;
    }
    length
}

/// gnulib の `base_len`。
fn base_len(name: &[u8]) -> usize {
    let mut length = name.len();
    while 1 < length && name.get(length - 1) == Some(&b'/') {
        length -= 1;
    }
    length
}

/// coreutils の `perform_basename`(接尾辞なし): `base_name` と `strip_trailing_slashes`。
pub fn base_name(name: &[u8]) -> Vec<u8> {
    let start = last_component(name);
    let base = name.get(start..).unwrap_or_default();
    let mut out = if !base.is_empty() {
        let mut length = base_len(base);
        if base.get(length) == Some(&b'/') {
            length += 1;
        }
        base.get(..length).unwrap_or_default().to_vec()
    } else {
        name.get(..base_len(name)).unwrap_or_default().to_vec()
    };
    // strip_trailing_slashes。
    let component = last_component(&out);
    let base_start = if component < out.len() { component } else { 0 };
    let limit = base_start + base_len(out.get(base_start..).unwrap_or_default());
    out.truncate(limit);
    out
}

/// `remove_suffix`(basename の接尾辞。名前と同じなら消さない)。
fn remove_suffix(name: &mut Vec<u8>, suffix: &[u8]) {
    if suffix.is_empty() || suffix.len() >= name.len() {
        return;
    }
    if name.ends_with(suffix) {
        name.truncate(name.len() - suffix.len());
    }
}

/// 道具の 1 回の実行(引数は `argv[1..]`)。
pub fn run(
    tool: Tool,
    locale: &mut ToolLocale,
    program: &[u8],
    args: &[&[u8]],
) -> Result<ToolRun, NeedTool> {
    let mut emit = Emit {
        locale,
        run: ToolRun::default(),
        program,
    };
    let mut state = (0usize, 0usize);
    let mut use_nuls = false;
    let mut multiple = false;
    let mut suffix: Option<Vec<u8>> = None;
    loop {
        match getopt(&mut emit, tool, args, &mut state)? {
            Got::End => break,
            Got::Error => {
                emit.try_help()?;
                return Ok(emit.run);
            }
            Got::Option(HELP, _) => {
                emit.usage(tool)?;
                return Ok(emit.run);
            }
            Got::Option(VERSION, _) => {
                emit.version(tool)?;
                return Ok(emit.run);
            }
            Got::Option(value, argument) => match u8::try_from(value).unwrap_or(0) {
                b'z' => use_nuls = true,
                b'a' => multiple = true,
                b's' => {
                    multiple = true;
                    suffix = argument;
                }
                _ => {}
            },
        }
    }
    let operands: Vec<&[u8]> = args.get(state.0..).unwrap_or_default().to_vec();
    if operands.is_empty() {
        let text = emit.coreutils(b"missing operand")?;
        emit.error(text);
        emit.try_help()?;
        return Ok(emit.run);
    }
    let end = if use_nuls { 0u8 } else { b'\n' };
    match tool {
        Tool::Dirname => {
            for operand in operands {
                let length = dir_len(operand);
                if length == 0 {
                    emit.out(b".");
                } else {
                    let part = operand.get(..length).unwrap_or_default().to_vec();
                    emit.out(&part);
                }
                emit.out(&[end]);
            }
        }
        Tool::Basename => {
            if !multiple && operands.len() > 2 {
                let template = emit.coreutils(b"extra operand %s")?;
                let quoted = quote(operands.get(2).copied().unwrap_or_default());
                emit.error(format(&template, &[&quoted]));
                emit.try_help()?;
                return Ok(emit.run);
            }
            let (names, suffix): (Vec<&[u8]>, Option<Vec<u8>>) = if multiple {
                (operands, suffix)
            } else {
                (
                    operands.first().copied().into_iter().collect(),
                    operands.get(1).map(|s| s.to_vec()),
                )
            };
            for name in names {
                let mut base = base_name(name);
                if let Some(suffix) = &suffix
                    && base.first() != Some(&b'/')
                {
                    remove_suffix(&mut base, suffix);
                }
                emit.out(&base);
                emit.out(&[end]);
            }
        }
    }
    emit.run.status = 0;
    Ok(emit.run)
}

/// gnulib の `quote`(この区間では使わない形。印字できる ASCII だけの値を `'…'` で囲む)。
fn quote(value: &[u8]) -> Vec<u8> {
    let mut out = b"'".to_vec();
    out.extend_from_slice(value);
    out.push(b'\'');
    out
}

#[cfg(test)]
mod tests {
    use super::super::locale::Codeset;
    use super::*;

    fn c_locale() -> ToolLocale {
        ToolLocale::fixed(b"C", &[], Codeset::Ascii)
    }

    fn one(tool: Tool, arg: &[u8]) -> ToolRun {
        run(tool, &mut c_locale(), tool.name(), &[arg]).unwrap()
    }

    fn joined(run: &ToolRun) -> Vec<u8> {
        run.stderr.concat()
    }

    #[test]
    fn dirname_and_basename_follow_gnulib() {
        for (input, dir, base) in [
            (&b"a/b"[..], &b"a"[..], &b"b"[..]),
            (b"/", b"/", b"/"),
            (b"//", b"/", b"/"),
            (b"///a//", b"/", b"a"),
            (b"a", b".", b"a"),
            (b"a/", b".", b"a"),
            (b"a/b/", b"a", b"b"),
            (b"x\n", b".", b"x\n"),
            (b"-", b".", b"-"),
            (b"/a", b"/", b"a"),
            (b"a//b//", b"a", b"b"),
        ] {
            let d = one(Tool::Dirname, input);
            let mut want = dir.to_vec();
            want.push(b'\n');
            assert_eq!(
                d.stdout,
                want,
                "dirname {:?}",
                String::from_utf8_lossy(input)
            );
            let b = one(Tool::Basename, input);
            let mut want = base.to_vec();
            want.push(b'\n');
            assert_eq!(
                b.stdout,
                want,
                "basename {:?}",
                String::from_utf8_lossy(input)
            );
        }
    }

    #[test]
    fn getopt_errors_match_glibc_in_c() {
        let r = one(Tool::Dirname, b"-x");
        assert_eq!(r.status, 1);
        assert_eq!(
            joined(&r),
            b"dirname: invalid option -- 'x'\nTry 'dirname --help' for more information.\n"
        );
        let r = one(Tool::Dirname, b"-zf");
        assert_eq!(
            joined(&r),
            b"dirname: invalid option -- 'f'\nTry 'dirname --help' for more information.\n"
        );
        let r = one(Tool::Dirname, b"-z");
        assert_eq!(
            joined(&r),
            b"dirname: missing operand\nTry 'dirname --help' for more information.\n"
        );
        assert_eq!(r.stderr.len(), 4);
        let r = one(Tool::Dirname, b"--help=x");
        assert_eq!(
            joined(&r),
            b"dirname: option '--help' doesn't allow an argument\nTry 'dirname --help' for more information.\n"
        );
        let r = one(Tool::Dirname, b"--zz");
        assert_eq!(
            joined(&r),
            b"dirname: unrecognized option '--zz'\nTry 'dirname --help' for more information.\n"
        );
        let r = one(Tool::Dirname, b"--=x");
        assert_eq!(
            joined(&r),
            b"dirname: option '--=x' is ambiguous; possibilities: '--zero' '--help' '--version'\nTry 'dirname --help' for more information.\n"
        );
        let r = one(Tool::Basename, b"-s");
        assert_eq!(
            joined(&r),
            b"basename: option requires an argument -- 's'\nTry 'basename --help' for more information.\n"
        );
        let r = one(Tool::Dirname, b"--");
        assert_eq!(
            joined(&r),
            b"dirname: missing operand\nTry 'dirname --help' for more information.\n"
        );
    }

    #[test]
    fn help_and_version_in_c() {
        for arg in [&b"--help"[..], b"--he", b"--h"] {
            let r = one(Tool::Dirname, arg);
            assert_eq!(r.status, 0);
            assert!(r.stdout.starts_with(b"Usage: dirname [OPTION] NAME...\n"));
            assert!(
                r.stdout.ends_with(
                    b"or available locally via: info '(coreutils) dirname invocation'\n"
                )
            );
        }
        let r = one(Tool::Basename, b"--v");
        assert_eq!(
            r.stdout,
            b"basename (GNU coreutils) 9.4\nCopyright (C) 2023 Free Software Foundation, Inc.\nLicense GPLv3+: GNU GPL version 3 or later <https://gnu.org/licenses/gpl.html>.\nThis is free software: you are free to change and redistribute it.\nThere is NO WARRANTY, to the extent permitted by law.\n\nWritten by David MacKenzie.\n"
        );
    }

    #[test]
    fn format_handles_positions() {
        assert_eq!(format(b"%2$s-%1$s", &[b"a", b"b"]), b"b-a");
        assert_eq!(format(b"%s %% %c", &[b"x", b"y"]), b"x % y");
    }
}
