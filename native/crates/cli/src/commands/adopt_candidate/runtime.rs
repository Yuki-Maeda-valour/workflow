//! 旧 Python の実行環境の振る舞い。起動の失敗(Fatal Python error)・環境変数の効果・標準出力の故障の扱い。
//! 判断の出典: 2026-11-11 22:24(文字コード固定)・22:26(標準ライブラリの行番号)・17:45(Current thread)・
//! 22:31 と 2026-11-12 00:28(起動を変える環境変数)の利用者決定。
use super::bytes::concat;
use super::diagnostics::{os_error, strerror};
use super::plan::Uncaught;

/// 環境変数の読み取り(テストでは固定の値を渡す)。空文字は「設定あり・空」。
pub(crate) trait Env {
    fn get(&self, name: &str) -> Option<Vec<u8>>;
}

/// 非空の環境変数だけを「設定あり」とする(`config_get_env` と同じ)。
fn setting(env: &dyn Env, name: &str) -> Option<Vec<u8>> {
    env.get(name).filter(|value| !value.is_empty())
}

fn c_space(byte: u8) -> bool {
    matches!(byte, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r')
}

/// `strtol` の読み方(先頭の空白・符号・10 進の数字だけ・末尾は余りなし)。
fn whole_integer(value: &[u8]) -> Option<i128> {
    let value = &value[value.iter().take_while(|byte| c_space(**byte)).count()..];
    let (negative, digits) = match value.first() {
        Some(b'-') => (true, &value[1..]),
        Some(b'+') => (false, &value[1..]),
        _ => (false, value),
    };
    if digits.is_empty() || !digits.iter().all(u8::is_ascii_digit) || digits.len() > 30 {
        return None;
    }
    let number: i128 = std::str::from_utf8(digits).ok()?.parse().ok()?;
    Some(if negative { -number } else { number })
}

/// `PYTHONOPTIMIZE` が 2 以上か(旧版の `__doc__` が消える)。数でない・範囲外は 1 と同じ扱い。
pub(crate) fn docstrings_stripped(env: &dyn Env) -> bool {
    setting(env, "PYTHONOPTIMIZE")
        .and_then(|value| whole_integer(&value))
        .is_some_and(|level| (2..=i128::from(i32::MAX)).contains(&level))
}

/// `PYTHONUNBUFFERED`(非空ならどの値でも)。
pub(crate) fn unbuffered(env: &dyn Env) -> bool {
    setting(env, "PYTHONUNBUFFERED").is_some()
}

fn hash_seed_valid(env: &dyn Env) -> bool {
    let Some(value) = setting(env, "PYTHONHASHSEED") else {
        return true;
    };
    if value == b"random" {
        return true;
    }
    let digits = &value[value.iter().take_while(|byte| c_space(**byte)).count()..];
    // `strtoul` は符号を受け付ける。`-0` は 0 として有効で、`-1` などは巨大な値になり範囲外。
    let (negative, digits) = match digits.first() {
        Some(b'-') => (true, &digits[1..]),
        Some(b'+') => (false, &digits[1..]),
        _ => (false, digits),
    };
    !digits.is_empty()
        && digits.iter().all(u8::is_ascii_digit)
        && (!negative || digits.iter().all(|byte| *byte == b'0'))
        && digits.len() <= 12
        && std::str::from_utf8(digits)
            .ok()
            .and_then(|text| text.parse::<u64>().ok())
            .is_some_and(|number| number <= u64::from(u32::MAX))
}

fn int_max_digits_valid(env: &dyn Env) -> bool {
    let Some(value) = setting(env, "PYTHONINTMAXSTRDIGITS") else {
        return true;
    };
    match whole_integer(&value) {
        Some(0) => true,
        Some(limit) => limit >= 640 && limit <= i128::from(i32::MAX),
        None => false,
    }
}

/// 旧 Python が起動の時点で落ちる条件。落ちるなら stderr へ書く診断と終了値 1。
pub(crate) struct Fatal {
    pub(crate) stderr: Vec<u8>,
}

/// 起動前の失敗。順序は旧 Python と同じ: PYTHONHASHSEED → PYTHONINTMAXSTRDIGITS → 標準入力 → 標準出力 → 標準エラー。
/// `directory[i]` は fd i がディレクトリか。`thread` は `Current thread` に出す値(位置と形式だけを検査する)。
pub(crate) fn startup_failure(env: &dyn Env, directory: [bool; 3], thread: u64) -> Option<Fatal> {
    if !hash_seed_valid(env) {
        return Some(Fatal {
            stderr: b"Fatal Python error: config_init_hash_seed: PYTHONHASHSEED must be \"random\" or an integer in range [0; 4294967295]\nPython runtime state: preinitialized\n\n".to_vec(),
        });
    }
    if !int_max_digits_valid(env) {
        return Some(Fatal {
            stderr: b"Fatal Python error: config_init_int_max_str_digits: PYTHONINTMAXSTRDIGITS: invalid limit; must be >= 640 or 0 for unlimited.\nPython runtime state: preinitialized\n\n".to_vec(),
        });
    }
    let current = format!(
        "\nCurrent thread 0x{thread:016x} (most recent call first):\n  <no Python frame>\n"
    );
    if directory[0] {
        return Some(Fatal {
            stderr: format!(
                "Fatal Python error: init_sys_streams: <stdin> is a directory, cannot continue\nPython runtime state: core initialized\n{current}"
            )
            .into_bytes(),
        });
    }
    if directory[1] {
        return Some(Fatal {
            stderr: format!(
                "Fatal Python error: init_sys_streams: can't initialize sys standard streams\nPython runtime state: core initialized\nIsADirectoryError: [Errno 21] Is a directory: 1\n{current}"
            )
            .into_bytes(),
        });
    }
    // 標準エラーがディレクトリのときは、診断を書く先が無いので何も出ない。
    directory[2].then(|| Fatal { stderr: Vec::new() })
}

/// 旧 Python の LC_CTYPE の強制(環境変数だけで決まる場合)。子の環境へ足す値と、`warn` のときの警告。
pub(crate) struct Coercion {
    pub(crate) warning: Option<&'static str>,
}

pub(crate) fn locale_coercion(env: &dyn Env) -> Option<Coercion> {
    let coerce = setting(env, "PYTHONCOERCECLOCALE");
    if coerce.as_deref() == Some(b"0") {
        return None;
    }
    if setting(env, "LC_ALL").is_some() {
        return None;
    }
    let effective = setting(env, "LC_CTYPE")
        .or_else(|| setting(env, "LANG"))
        .unwrap_or_else(|| b"C".to_vec());
    if effective != b"C" && effective != b"POSIX" {
        return None;
    }
    Some(Coercion {
        warning: (coerce.as_deref() == Some(b"warn")).then_some(
            "Python detected LC_CTYPE=C: LC_CTYPE coerced to C.UTF-8 (set another locale or PYTHONCOERCECLOCALE=0 to disable this locale coercion behavior).\n",
        ),
    })
}

/// 旧 Python の `os.environ` に見える環境(子へ渡す元)。LC_CTYPE の強制が起きるなら `C.UTF-8` にする。
pub(crate) fn python_environment(
    vars: Vec<(Vec<u8>, Vec<u8>)>,
    env: &dyn Env,
) -> Vec<(Vec<u8>, Vec<u8>)> {
    if locale_coercion(env).is_none() {
        return vars;
    }
    let mut vars = vars;
    match vars.iter_mut().find(|(key, _)| key == b"LC_CTYPE") {
        Some((_, value)) => *value = b"C.UTF-8".to_vec(),
        None => vars.push((b"LC_CTYPE".to_vec(), b"C.UTF-8".to_vec())),
    }
    vars
}

/// 書き込みの境界(標準出力・標準エラー)。部分書込みは `Ok(n)`、失敗は errno。
pub(crate) trait Sink {
    fn write(&mut self, bytes: &[u8]) -> Result<usize, i32>;
}

/// 実際の標準の fd(1 か 2)。
pub(crate) struct Standard(pub(crate) i32);

impl Sink for Standard {
    fn write(&mut self, bytes: &[u8]) -> Result<usize, i32> {
        dev_workflow_platform::adopt_candidate::write_standard(self.0, bytes)
    }
}

/// 全部書けるまで繰り返す。途中で失敗したら、それまでに書けたバイトは残る。
pub(crate) fn write_all(sink: &mut dyn Sink, bytes: &[u8]) -> Result<(), i32> {
    let mut done = 0;
    while done < bytes.len() {
        match sink.write(&bytes[done..]) {
            Ok(0) => return Err(libc_eio()),
            Ok(count) => done += count,
            Err(errno) => return Err(errno),
        }
    }
    Ok(())
}

fn libc_eio() -> i32 {
    5
}

/// errno に対応する Python の例外の型の名前。
fn exception_name(errno: i32) -> &'static str {
    match errno {
        32 => "BrokenPipeError",
        11 => "BlockingIOError",
        _ => "OSError",
    }
}

fn os_exception(errno: i32) -> Vec<u8> {
    os_error(errno, &strerror(errno), None)
}

/// `print_help` の書込みで起きた `OSError` の traceback(固定名と本家 3.10.11 の行番号)。
fn help_write_traceback(errno: i32) -> Vec<u8> {
    const FRAMES: [(&str, u32, &str, &str); 10] = [
        ("adopt-candidate.py", 273, "<module>", "sys.exit(main())"),
        (
            "adopt-candidate.py",
            260,
            "main",
            "args = parser.parse_args()",
        ),
        (
            "argparse.py",
            1833,
            "parse_args",
            "args, argv = self.parse_known_args(args, namespace)",
        ),
        (
            "argparse.py",
            1866,
            "parse_known_args",
            "namespace, args = self._parse_known_args(args, namespace)",
        ),
        (
            "argparse.py",
            2079,
            "_parse_known_args",
            "start_index = consume_optional(start_index)",
        ),
        (
            "argparse.py",
            2019,
            "consume_optional",
            "take_action(action, args, option_string)",
        ),
        (
            "argparse.py",
            1943,
            "take_action",
            "action(self, namespace, argument_values, option_string)",
        ),
        ("argparse.py", 1106, "__call__", "parser.print_help()"),
        (
            "argparse.py",
            2567,
            "print_help",
            "self._print_message(self.format_help(), file)",
        ),
        ("argparse.py", 2573, "_print_message", "file.write(message)"),
    ];
    traceback(&FRAMES, exception_name(errno), &os_exception(errno))
}

fn traceback(frames: &[(&str, u32, &str, &str)], class: &str, message: &[u8]) -> Vec<u8> {
    let mut text = String::from("Traceback (most recent call last):\n");
    for (file, line, function, source) in frames {
        text.push_str(&format!(
            "  File \"{file}\", line {line}, in {function}\n    {source}\n"
        ));
    }
    concat(&[text.as_bytes(), class.as_bytes(), b": ", message, b"\n"])
}

/// 捕捉されない例外の traceback。
fn uncaught_traceback(kind: Uncaught) -> Vec<u8> {
    const MAIN: [(&str, u32, &str, &str); 2] = [
        ("adopt-candidate.py", 273, "<module>", "sys.exit(main())"),
        (
            "adopt-candidate.py",
            265,
            "main",
            "print(json.dumps(adopt(args.root, args.candidate, args.name, args.expect_sha256, args.accept), ensure_ascii=False))",
        ),
    ];
    match kind {
        Uncaught::RootNotPath => {
            let mut frames = MAIN.to_vec();
            frames.extend([
                (
                    "adopt-candidate.py",
                    190,
                    "adopt",
                    "root, source = Path(root).absolute(), Path(source).absolute()",
                ),
                ("pathlib.py", 960, "__new__", "self = cls._from_parts(args)"),
                (
                    "pathlib.py",
                    594,
                    "_from_parts",
                    "drv, root, parts = self._parse_args(args)",
                ),
                ("pathlib.py", 578, "_parse_args", "a = os.fspath(a)"),
            ]);
            traceback(
                &frames,
                "TypeError",
                b"expected str, bytes or os.PathLike object, not list",
            )
        }
        Uncaught::DigestNotString => {
            let mut frames = MAIN.to_vec();
            frames.extend([
                ("adopt-candidate.py", 200, "adopt", "warnings = validate_candidate(data, digest)"),
                (
                    "adopt-candidate.py",
                    74,
                    "validate_candidate",
                    "if not re.fullmatch('[0-9a-f]{64}', digest) or hashlib.sha256(data).hexdigest() != digest:",
                ),
                ("re.py", 195, "fullmatch", "return _compile(pattern, flags).fullmatch(string)"),
            ]);
            traceback(
                &frames,
                "TypeError",
                b"expected string or bytes-like object",
            )
        }
    }
}

/// プログラムが決めた最終の出力。書込みの故障の扱いは `emit` が旧 Python に合わせる。
pub(crate) enum Program {
    /// help(標準出力へ。終了 0)。
    Help(Vec<u8>),
    /// 引数の誤り(標準エラーへ。終了 2)。
    UsageError(Vec<u8>),
    /// 正常(標準出力の JSON と LF。終了 0)。
    Success(Vec<u8>),
    /// 捕捉された停止(`候補の採用を停止: ` に続く表示。標準エラーへ。終了 2)。
    Stop(Vec<u8>),
    /// 捕捉されない例外の traceback(標準エラーへ。終了 1)。
    Uncaught(Uncaught),
}

const STOP_PREFIX: &str = "候補の採用を停止: ";

/// 標準出力・標準エラーの種類(旧 Python の `create_stdio` の分け方)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum StreamKind {
    /// `PYTHONUNBUFFERED`: 生の `FileIO`。`write` ごとに `write(2)` を 1 回だけ呼び、短く書けても気づかない。
    Unbuffered,
    /// `BufferedWriter`。flush は終了時か、塊が溢れたとき。標準出力が端末なら、改行を含む書込みの度に流す
    /// (`line_buffering=True`。`PyStream` の `line_buffering`)。
    Block,
}

/// 旧 `sys.stdout`・`sys.stderr` の `TextIOWrapper` + `BufferedWriter` の動き(CPython 3.10 の C 実装)。
/// 書込みの途中で失敗したときの、どこまで出るか・どこで例外になるかを合わせる。
pub(crate) struct PyStream<'a> {
    sink: &'a mut dyn Sink,
    kind: StreamKind,
    /// `BufferedWriter` の大きさ(`st_blksize`。1 以下なら 8192)。
    buffer_size: usize,
    buffer: Vec<u8>,
    /// `TextIOWrapper` の溜め(`_CHUNK_SIZE` は 8192)。
    pending: Vec<u8>,
    /// `line_buffering`(標準出力が端末のとき真)。改行か復帰を含む書込みは、溜めと `BufferedWriter` を流す。
    line_buffering: bool,
}

const CHUNK_SIZE: usize = 8192;

impl<'a> PyStream<'a> {
    pub(crate) fn new(
        sink: &'a mut dyn Sink,
        kind: StreamKind,
        blksize: i64,
        line_buffering: bool,
    ) -> Self {
        let buffer_size = if blksize > 1 { blksize as usize } else { 8192 };
        Self {
            sink,
            kind,
            buffer_size,
            buffer: Vec::new(),
            pending: Vec::new(),
            line_buffering,
        }
    }

    /// `raw.write` を 1 回(`_bufferedwriter_raw_write`)。
    fn raw_write(&mut self, from_buffer: bool, data: &[u8]) -> Result<usize, i32> {
        let _ = from_buffer;
        self.sink.write(data)
    }

    /// `_bufferedwriter_flush_unlocked`: 全部書けるまで繰り返す。失敗しても、書けた分は溜めから除く。
    fn flush_buffer(&mut self) -> Result<(), i32> {
        while !self.buffer.is_empty() {
            let data = std::mem::take(&mut self.buffer);
            match self.sink.write(&data) {
                Ok(0) => {
                    self.buffer = data;
                    return Err(5);
                }
                Ok(count) => self.buffer = data[count..].to_vec(),
                Err(errno) => {
                    self.buffer = data;
                    return Err(errno);
                }
            }
        }
        Ok(())
    }

    /// `BufferedWriter.write`。
    fn buffered_write(&mut self, data: &[u8]) -> Result<(), i32> {
        if data.len() <= self.buffer_size - self.buffer.len().min(self.buffer_size) {
            self.buffer.extend_from_slice(data);
            return Ok(());
        }
        self.flush_buffer()?;
        let mut written = 0;
        let mut remaining = data.len();
        while remaining > self.buffer_size {
            let count = self.raw_write(false, &data[written..])?;
            written += count;
            remaining -= count;
        }
        self.buffer = data[written..].to_vec();
        Ok(())
    }

    /// `TextIOWrapper` の溜めを `buffer.write` へ渡す。溜めは、成功・失敗にかかわらず空になる。
    fn write_pending(&mut self) -> Result<(), i32> {
        let data = std::mem::take(&mut self.pending);
        if data.is_empty() {
            return Ok(());
        }
        if self.kind == StreamKind::Unbuffered {
            return self.sink.write(&data).map(|_| ());
        }
        self.buffered_write(&data)
    }

    /// `TextIOWrapper.write`。
    pub(crate) fn write(&mut self, text: &[u8]) -> Result<(), i32> {
        let need_flush = self.kind == StreamKind::Unbuffered;
        let line_flush =
            self.line_buffering && text.iter().any(|byte| *byte == b'\n' || *byte == b'\r');
        if self.pending.len() + text.len() > CHUNK_SIZE {
            self.write_pending()?;
        }
        self.pending.extend_from_slice(text);
        if self.pending.len() >= CHUNK_SIZE || need_flush || line_flush {
            self.write_pending()?;
        }
        if line_flush && self.kind != StreamKind::Unbuffered {
            self.flush_buffer()?;
        }
        Ok(())
    }

    /// `TextIOWrapper.flush`(終了時の `flush_std_files` と `PyErr_Display` の後の flush)。
    pub(crate) fn flush(&mut self) -> Result<(), i32> {
        self.write_pending()?;
        if self.kind == StreamKind::Unbuffered {
            return Ok(());
        }
        self.flush_buffer()
    }
}

fn unraisable(errno: i32) -> Vec<u8> {
    concat(&[
        b"Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>\n",
        exception_name(errno).as_bytes(),
        b": ",
        &os_exception(errno),
        b"\n",
    ])
}

/// 引数の誤りの出力を、usage と `prog: error: …` の 2 回の `write()` に分ける。
fn usage_parts(text: &[u8]) -> (&[u8], &[u8]) {
    let marker = b"adopt-candidate.py: error: ";
    let at = text
        .windows(marker.len())
        .position(|window| window == marker)
        .unwrap_or(text.len());
    text.split_at(at)
}

/// 標準出力・標準エラーの種類と `BufferedWriter` の大きさ。
#[derive(Clone, Copy)]
pub(crate) struct Streams {
    pub(crate) out: (StreamKind, i64),
    pub(crate) err: (StreamKind, i64),
    /// 標準出力が端末か(`line_buffering`)。起動したときに決まる。
    pub(crate) out_terminal: bool,
}

impl Streams {
    /// 端末でない・`PYTHONUNBUFFERED` なしの既定(`BufferedWriter` は 8192)。
    #[cfg(test)]
    pub(crate) fn block() -> Self {
        Self {
            out: (StreamKind::Block, 8192),
            err: (StreamKind::Block, 8192),
            out_terminal: false,
        }
    }

    pub(crate) fn unbuffered() -> Self {
        Self {
            out: (StreamKind::Unbuffered, 0),
            err: (StreamKind::Unbuffered, 0),
            out_terminal: false,
        }
    }
}

/// 標準エラーへの書込みが失敗して例外が外へ出たとき、旧 Python は、その例外の呼出履歴を標準エラーへ書こうとする。
/// 標準エラーはもう書けないので、中身は出ないが、行を流す途中で失敗した呼出履歴は `BufferedWriter` に残り、終了時の flush も
/// 失敗する(終了値 120)。中身ではなく、溜めに残ることだけを、短い代わりの文で模す。終了値は 1。
/// 旧版の実際の呼出履歴(固定名・本家 3.10.11 の行番号・例外の連鎖)は、書けたときにだけ見える。書けないままなら出力先には
/// 何も出ないので、比較できない。書けるようになる場合(待たずに書く pipe の EAGAIN・空きが戻る ENOSPC など)は、作れる入力が
/// 無く、比較器の未実施の表に載せてある(`標準エラーの書込みが失敗した後に、空きが戻って呼出履歴が出る場合`)。
fn stderr_failure(stderr: &mut PyStream<'_>) -> i32 {
    let _ = stderr.write(&b"Traceback (most recent call last):\n  File \"adopt-candidate.py\", line 0, in f\n    x\n".repeat(8));
    1
}

/// 出力して終了値を返す。旧 Python の流れ(書込み → 例外の捕捉 → 終了時の `flush_std_files`)をそのまま追う:
/// 終了時の flush が失敗したら、標準エラーへ `Exception ignored …` を書き、終了値は 120 になる。
pub(crate) fn emit(
    program: Program,
    streams: Streams,
    out: &mut dyn Sink,
    err: &mut dyn Sink,
) -> i32 {
    let mut stdout = PyStream::new(out, streams.out.0, streams.out.1, streams.out_terminal);
    let mut stderr = PyStream::new(err, streams.err.0, streams.err.1, true);
    let unbuffered = streams.out.0 == StreamKind::Unbuffered;
    let mut code = match program {
        Program::Help(text) => match stdout.write(&text) {
            Ok(()) => 0,
            Err(errno) => {
                let _ = stderr.write(&help_write_traceback(errno));
                let _ = stderr.flush();
                1
            }
        },
        Program::UsageError(text) => {
            let (usage, message) = usage_parts(&text);
            match stderr.write(usage).and_then(|()| stderr.write(message)) {
                Ok(()) => 2,
                Err(_) => stderr_failure(&mut stderr),
            }
        }
        Program::Success(text) => {
            let body = text.strip_suffix(b"\n").unwrap_or(&text);
            match stdout.write(body).and_then(|()| stdout.write(b"\n")) {
                Ok(()) => 0,
                Err(errno) => {
                    let message = super::bytes::stderr(&concat(&[
                        STOP_PREFIX.as_bytes(),
                        &os_exception(errno),
                    ]));
                    match stderr.write(&message).and_then(|()| stderr.write(b"\n")) {
                        Ok(()) => 2,
                        Err(_) => stderr_failure(&mut stderr),
                    }
                }
            }
        }
        Program::Stop(message) => {
            let line = super::bytes::stderr(&concat(&[STOP_PREFIX.as_bytes(), &message]));
            match stderr.write(&line).and_then(|()| stderr.write(b"\n")) {
                Ok(()) => 2,
                Err(_) => stderr_failure(&mut stderr),
            }
        }
        Program::Uncaught(kind) => {
            let _ = stderr.write(&uncaught_traceback(kind));
            let _ = stderr.flush();
            1
        }
    };
    let _ = unbuffered;
    // スクリプトの実行が終わった直後に、旧 Python は `flush_io()`(標準エラー → 標準出力)を呼び、失敗を捨てる
    // (CPython 3.10.11 の `PyRun_SimpleFileExFlags`)。`TextIOWrapper` の溜めは書込みの前に空になるので、
    // `BufferedWriter` の大きさを超える溜めの書込みがここで失敗すると、データは失われ、終了時の flush は成功する。
    let _ = stderr.flush();
    let _ = stdout.flush();
    if let Err(errno) = stdout.flush() {
        let _ = stderr.write(&unraisable(errno));
        code = 120;
    }
    if stderr.flush().is_err() {
        code = 120;
    }
    code
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    pub(crate) struct Fixed(pub(crate) HashMap<String, Vec<u8>>);
    impl Env for Fixed {
        fn get(&self, name: &str) -> Option<Vec<u8>> {
            self.0.get(name).cloned()
        }
    }
    pub(crate) fn env(pairs: &[(&str, &str)]) -> Fixed {
        Fixed(
            pairs
                .iter()
                .map(|(k, v)| (k.to_string(), v.as_bytes().to_vec()))
                .collect(),
        )
    }

    /// 旧 Python 3.10.11 で実際に確かめた値。
    #[test]
    fn optimize_levels_follow_the_old_interpreter() {
        for (value, stripped) in [
            ("", false),
            ("x", false),
            ("1", false),
            ("2", true),
            ("3", true),
            ("-1", false),
            ("+2", true),
            (" 2", true),
            ("2x", false),
            ("0", false),
            ("2 ", false),
            ("02", true),
            ("0x2", false),
            ("10", true),
            ("  +3", true),
            ("2.0", false),
            ("-5", false),
            ("1 2", false),
            ("99999999999999999999", false),
        ] {
            assert_eq!(
                docstrings_stripped(&env(&[("PYTHONOPTIMIZE", value)])),
                stripped,
                "{value:?}"
            );
        }
        assert!(!docstrings_stripped(&env(&[])));
    }

    #[test]
    fn invalid_start_up_variables_stop_before_any_stream_check() {
        for (value, valid) in [
            ("", true),
            ("random", true),
            ("0", true),
            ("4294967295", true),
            ("4294967296", false),
            ("-1", false),
            ("x", false),
            (" 1", true),
            ("1 ", false),
            ("+1", true),
            ("00", true),
            ("-0", true),
            ("-00", true),
            ("+-1", false),
            ("--0", false),
            ("-4294967295", false),
            ("0x1", false),
            ("1x", false),
        ] {
            assert_eq!(
                hash_seed_valid(&env(&[("PYTHONHASHSEED", value)])),
                valid,
                "{value:?}"
            );
        }
        for (value, valid) in [
            ("", true),
            ("0", true),
            ("640", true),
            ("639", false),
            ("abc", false),
            (" 700", true),
            ("+700", true),
            ("700x", false),
            ("-1", false),
            ("4301", true),
        ] {
            assert_eq!(
                int_max_digits_valid(&env(&[("PYTHONINTMAXSTRDIGITS", value)])),
                valid,
                "{value:?}"
            );
        }
        let both = env(&[("PYTHONHASHSEED", "x"), ("PYTHONINTMAXSTRDIGITS", "5")]);
        let fatal = startup_failure(&both, [true, false, false], 1).unwrap();
        assert!(
            fatal
                .stderr
                .starts_with(b"Fatal Python error: config_init_hash_seed")
        );
        let digits = env(&[("PYTHONINTMAXSTRDIGITS", "5")]);
        let fatal = startup_failure(&digits, [true, true, true], 1).unwrap();
        assert!(
            fatal
                .stderr
                .starts_with(b"Fatal Python error: config_init_int_max_str_digits")
        );
        assert!(fatal.stderr.ends_with(b"preinitialized\n\n"));
    }

    #[test]
    fn standard_stream_directories_are_fatal_in_stdin_stdout_stderr_order() {
        let none = env(&[]);
        assert!(startup_failure(&none, [false; 3], 1).is_none());
        let stdin = startup_failure(&none, [true, true, false], 0x7f12_3456_789a).unwrap();
        assert_eq!(
            String::from_utf8(stdin.stderr).unwrap(),
            "Fatal Python error: init_sys_streams: <stdin> is a directory, cannot continue\nPython runtime state: core initialized\n\nCurrent thread 0x00007f123456789a (most recent call first):\n  <no Python frame>\n"
        );
        let stdout = startup_failure(&none, [false, true, false], 1).unwrap();
        assert!(String::from_utf8(stdout.stderr).unwrap().contains(
            "can't initialize sys standard streams\nPython runtime state: core initialized\nIsADirectoryError: [Errno 21] Is a directory: 1\n\nCurrent thread 0x"
        ));
        assert_eq!(
            startup_failure(&none, [false, false, true], 1)
                .unwrap()
                .stderr,
            b""
        );
        assert_eq!(
            startup_failure(&none, [false, true, true], 1)
                .unwrap()
                .stderr
                .is_empty(),
            false
        );
    }

    #[test]
    fn locale_is_coerced_only_when_the_environment_alone_says_c() {
        let coerced = |pairs: &[(&str, &str)]| locale_coercion(&env(pairs)).is_some();
        assert!(coerced(&[]));
        for pairs in [
            &[("LANG", "C")][..],
            &[("LANG", "POSIX")],
            &[("LC_CTYPE", "C")],
            &[("LC_CTYPE", "C"), ("LANG", "en_US.UTF-8")],
            &[("LC_ALL", "")],
            &[("PYTHONUTF8", "1")],
            &[("PYTHONCOERCECLOCALE", "1")],
        ] {
            assert!(coerced(pairs), "{pairs:?}");
        }
        for pairs in [
            &[("LANG", "ja_JP.UTF-8")][..],
            &[("LANG", "en_US.UTF-8")],
            &[("LC_ALL", "C")],
            &[("PYTHONCOERCECLOCALE", "0")],
            &[("LC_CTYPE", "en_US.UTF-8"), ("LANG", "C")],
        ] {
            assert!(!coerced(pairs), "{pairs:?}");
        }
        assert!(
            locale_coercion(&env(&[("PYTHONCOERCECLOCALE", "warn")]))
                .unwrap()
                .warning
                .unwrap()
                .starts_with("Python detected LC_CTYPE=C")
        );
        assert!(locale_coercion(&env(&[])).unwrap().warning.is_none());
    }

    #[test]
    fn coerced_locale_reaches_the_child_environment_like_the_old_os_environ() {
        let vars = vec![
            (b"PATH".to_vec(), b"/bin".to_vec()),
            (b"LC_CTYPE".to_vec(), b"C".to_vec()),
        ];
        let coerced = python_environment(vars.clone(), &env(&[("LC_CTYPE", "C")]));
        assert!(
            coerced.contains(&(b"LC_CTYPE".to_vec(), b"C.UTF-8".to_vec())) && coerced.len() == 2
        );
        let added = python_environment(vec![(b"PATH".to_vec(), b"/bin".to_vec())], &env(&[]));
        assert_eq!(
            added.last().unwrap(),
            &(b"LC_CTYPE".to_vec(), b"C.UTF-8".to_vec())
        );
        assert_eq!(
            python_environment(vars.clone(), &env(&[("LC_ALL", "C")])),
            vars
        );
        assert_eq!(
            python_environment(vars.clone(), &env(&[("PYTHONCOERCECLOCALE", "0")])),
            vars
        );
    }

    #[test]
    fn unbuffered_is_any_non_empty_value() {
        assert!(unbuffered(&env(&[("PYTHONUNBUFFERED", "1")])));
        assert!(unbuffered(&env(&[("PYTHONUNBUFFERED", "0")])));
        assert!(!unbuffered(&env(&[("PYTHONUNBUFFERED", "")])));
        assert!(!unbuffered(&env(&[])));
    }

    /// 書ける量の上限を持つ代役。上限を超えると errno で失敗する(`RLIMIT_FSIZE` の見え方)。
    pub(crate) struct Limited {
        pub(crate) bytes: Vec<u8>,
        pub(crate) room: usize,
        pub(crate) errno: i32,
    }
    impl Sink for Limited {
        fn write(&mut self, data: &[u8]) -> Result<usize, i32> {
            if self.room == 0 {
                return Err(self.errno);
            }
            let count = data.len().min(self.room);
            self.bytes.extend_from_slice(&data[..count]);
            self.room -= count;
            Ok(count)
        }
    }
    fn sink(room: usize, errno: i32) -> Limited {
        Limited {
            bytes: Vec::new(),
            room,
            errno,
        }
    }

    #[test]
    fn help_failures_split_on_buffering_like_the_old_interpreter() {
        for room in [0usize, 10] {
            let (mut out, mut err) = (sink(room, 28), sink(usize::MAX, 28));
            assert_eq!(
                emit(
                    Program::Help(b"x".repeat(477)),
                    Streams::block(),
                    &mut out,
                    &mut err
                ),
                120
            );
            assert_eq!(out.bytes.len(), room);
            assert_eq!(
                String::from_utf8(err.bytes).unwrap(),
                "Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>\nOSError: [Errno 28] No space left on device\n"
            );
            let (mut out, mut err) = (sink(room, 28), sink(usize::MAX, 28));
            let code = emit(
                Program::Help(b"x".repeat(477)),
                Streams::unbuffered(),
                &mut out,
                &mut err,
            );
            if room == 0 {
                assert_eq!(code, 1);
                let text = String::from_utf8(err.bytes).unwrap();
                assert!(text.starts_with("Traceback (most recent call last):\n  File \"adopt-candidate.py\", line 273, in <module>\n"));
                assert!(text.ends_with(
                    "    file.write(message)\nOSError: [Errno 28] No space left on device\n"
                ));
                assert_eq!(text.matches("  File \"argparse.py\"").count(), 8);
            } else {
                // 待たずに書く側は、短く書けても気づかない(旧 Python の実測)。
                assert_eq!((code, out.bytes.len(), err.bytes.len()), (0, 10, 0));
            }
        }
        let (mut out, mut err) = (sink(0, 28), sink(0, 28));
        assert_eq!(
            emit(
                Program::Help(b"x".to_vec()),
                Streams::block(),
                &mut out,
                &mut err
            ),
            120
        );
        assert_eq!(
            emit(
                Program::Help(b"x".to_vec()),
                Streams::unbuffered(),
                &mut out,
                &mut err
            ),
            1
        );
        let (mut out, mut err) = (sink(usize::MAX, 28), sink(0, 28));
        assert_eq!(
            emit(
                Program::Help(b"x".to_vec()),
                Streams::block(),
                &mut out,
                &mut err
            ),
            0
        );
    }

    /// 標準出力が端末なら `line_buffering=True`: 改行を含む書込みの中で失敗し(呼出履歴)、`BufferedWriter` に残った分が
    /// 終了時の flush でも失敗する(旧 Python の実測: 終了値 120・呼出履歴と `Exception ignored` の両方)。
    #[test]
    fn terminal_stdout_fails_inside_the_write_and_again_at_exit() {
        let streams = Streams {
            out_terminal: true,
            ..Streams::block()
        };
        let (mut out, mut err) = (sink(0, 5), sink(usize::MAX, 5));
        assert_eq!(
            emit(
                Program::Help(b"x\n".repeat(200)),
                streams,
                &mut out,
                &mut err
            ),
            120
        );
        let text = String::from_utf8(err.bytes).unwrap();
        assert!(
            text.starts_with("Traceback (most recent call last):\n"),
            "{text}"
        );
        assert!(text.contains("    file.write(message)\nOSError: [Errno 5] Input/output error\n"));
        assert!(text.ends_with("Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>\nOSError: [Errno 5] Input/output error\n"));
        // 改行を含まない書込みは流さないので、端末でも途中で失敗しない(終了時に失敗する)。
        let (mut out, mut err) = (sink(0, 5), sink(usize::MAX, 5));
        assert_eq!(
            emit(Program::Help(b"x".repeat(200)), streams, &mut out, &mut err),
            120
        );
        assert!(!String::from_utf8(err.bytes).unwrap().contains("Traceback"));
    }

    /// 標準エラーは `line_buffering=True`。長い usage と停止の文が書けないときの終了値・出る量(旧 Python の実測。
    /// `st_blksize` 4096・usage 約 230 バイト + 文)。
    #[test]
    fn long_stderr_messages_follow_the_measured_old_behaviour() {
        let usage = format!("usage: {}\n", "u".repeat(220));
        let text = |length: usize| {
            format!(
                "{usage}adopt-candidate.py: error: unrecognized arguments: {}\n",
                "y".repeat(length)
            )
            .into_bytes()
        };
        let blk = Streams {
            out: (StreamKind::Block, 4096),
            err: (StreamKind::Block, 4096),
            out_terminal: false,
        };
        let unbuffered = Streams {
            out: (StreamKind::Unbuffered, 0),
            err: (StreamKind::Unbuffered, 0),
            out_terminal: false,
        };
        // (長さ, 上限, 溜める側の終了値, 待たずに書く側の終了値)
        for (length, room, buffered, direct) in [
            (3000, 0, 120, 1),
            (3000, 5000, 2, 2),
            (5000, 5000, 120, 2),
            (9000, 5000, 120, 2),
            (9000, 100, 120, 1),
        ] {
            for (streams, code) in [(blk, buffered), (unbuffered, direct)] {
                let (mut out, mut err) = (sink(usize::MAX, 27), sink(room, 27));
                assert_eq!(
                    emit(
                        Program::UsageError(text(length)),
                        streams,
                        &mut out,
                        &mut err
                    ),
                    code,
                    "{length}・{room}"
                );
                assert!(err.bytes.len() <= room);
            }
        }
    }

    #[test]
    fn stderr_failures_are_120_when_buffered_and_1_when_unbuffered() {
        for program in [
            || Program::UsageError(b"u\n".to_vec()),
            || Program::Stop(b"s".to_vec()),
        ] {
            let (mut out, mut err) = (sink(usize::MAX, 32), sink(0, 28));
            assert_eq!(emit(program(), Streams::block(), &mut out, &mut err), 120);
            assert_eq!(
                emit(program(), Streams::unbuffered(), &mut out, &mut err),
                1
            );
            let (mut out, mut err) = (sink(usize::MAX, 32), sink(usize::MAX, 28));
            assert_eq!(emit(program(), Streams::block(), &mut out, &mut err), 2);
        }
    }

    /// 旧 Python で実測した、`RLIMIT_FSIZE` を使った途中書込み。
    #[test]
    fn partial_writes_follow_the_measured_old_behaviour() {
        // 溜める側: 上限までは書け、flush の失敗で 120(標準エラーは全バイトの先頭を残す)。
        let (mut out, mut err) = (sink(usize::MAX, 27), sink(20, 27));
        assert_eq!(
            emit(
                Program::Stop(b"x".repeat(60)),
                Streams::block(),
                &mut out,
                &mut err
            ),
            120
        );
        assert_eq!(err.bytes.len(), 20);
        // 待たずに書く側: 最初の write は短く書けても気づかず、LF の write が失敗して例外(1)。
        let (mut out, mut err) = (sink(usize::MAX, 27), sink(20, 27));
        assert_eq!(
            emit(
                Program::Stop(b"x".repeat(60)),
                Streams::unbuffered(),
                &mut out,
                &mut err
            ),
            1
        );
        assert_eq!(err.bytes.len(), 20);
        let usage = b"usage: adopt-candidate.py [-h]\nadopt-candidate.py: error: bad\n".to_vec();
        let (mut out, mut err) = (sink(usize::MAX, 27), sink(10, 27));
        assert_eq!(
            emit(
                Program::UsageError(usage.clone()),
                Streams::unbuffered(),
                &mut out,
                &mut err
            ),
            1
        );
        assert_eq!(err.bytes.len(), 10);
        let (mut out, mut err) = (sink(usize::MAX, 27), sink(10, 27));
        assert_eq!(
            emit(
                Program::UsageError(usage),
                Streams::block(),
                &mut out,
                &mut err
            ),
            120
        );
        // 正常の JSON: 溜める側は 120、待たずに書く側は LF の write の失敗で 2。
        for (unbuffered, code) in [(false, 120), (true, 2)] {
            let (mut out, mut err) = (sink(10, 27), sink(usize::MAX, 27));
            assert_eq!(
                emit(
                    Program::Success(b"{\"path\": \"/r\"}\n".to_vec()),
                    if unbuffered {
                        Streams::unbuffered()
                    } else {
                        Streams::block()
                    },
                    &mut out,
                    &mut err
                ),
                code
            );
            assert_eq!(out.bytes.len(), 10);
        }
        // 捕捉されない例外の traceback が書けないとき。
        for (unbuffered, code) in [(false, 120), (true, 1)] {
            let (mut out, mut err) = (sink(usize::MAX, 27), sink(100, 27));
            assert_eq!(
                emit(
                    Program::Uncaught(Uncaught::RootNotPath),
                    if unbuffered {
                        Streams::unbuffered()
                    } else {
                        Streams::block()
                    },
                    &mut out,
                    &mut err
                ),
                code
            );
        }
    }

    /// `TextIOWrapper` の溜め(8192)と `BufferedWriter`(`st_blksize` = 4096)の境目。旧 Python の実測と同じ。
    #[test]
    fn large_json_fails_inside_print_from_the_chunk_size_on() {
        let blk = Streams {
            out: (StreamKind::Block, 4096),
            err: (StreamKind::Block, 4096),
            out_terminal: false,
        };
        let json = |total: usize| {
            let mut text = vec![b'x'; total - 1];
            text.push(b'\n');
            text
        };
        // 本家 3.10.11 の実測(標準出力が通常ファイル・`st_blksize` 4096・上限 `room`)。
        // (合計, 上限, 終了値, 出た量, 終了時に `Exception ignored` を書くか)。
        // 4096 を超え 8192 未満の溜めは、`flush_io()` の中の失敗で捨てられ、終了時の flush は成功する(終了 0・出力なし)。
        for (total, room, code, written, unraisable) in [
            (4096, 0, 120, 0, true),
            (4097, 0, 0, 0, false),
            (5000, 0, 0, 0, false),
            (8191, 0, 0, 0, false),
            (8192, 0, 2, 0, false),
            (8193, 0, 2, 0, false),
            (9808, 0, 2, 0, false),
            (4096, 5000, 0, 4096, false),
            (4097, 5000, 0, 4097, false),
            (6000, 5000, 120, 5000, true),
            (8190, 5000, 120, 5000, true),
            (8191, 5000, 120, 5000, true),
            (8192, 5000, 120, 5000, true),
            (8193, 5000, 120, 5000, true),
            (9808, 5000, 2, 5000, false),
            (8191, 8192, 0, 8191, false),
            (8192, 8192, 0, 8192, false),
            (8193, 8192, 120, 8192, true),
            (9808, 8192, 120, 8192, true),
            (20000, 8192, 2, 8192, false),
        ] {
            let (mut out, mut err) = (sink(room, 27), sink(usize::MAX, 27));
            assert_eq!(
                emit(Program::Success(json(total)), blk, &mut out, &mut err),
                code,
                "{total}・{room}"
            );
            assert_eq!(out.bytes.len(), written, "{total}・{room}");
            assert_eq!(
                String::from_utf8(err.bytes)
                    .unwrap()
                    .starts_with("Exception ignored"),
                unraisable,
                "{total}・{room}"
            );
        }
        // 途中まで書けて、残りが BufferedWriter の大きさ(4096)以下なら溜めに残り、print は成功し、終了時に失敗する(120)。
        // 残りがまだ大きければ、続けて書こうとして print の中で失敗する(2)。
        for (room, code) in [(6000, 120), (5000, 120), (4000, 2), (0, 2)] {
            let (mut out, mut err) = (sink(room, 27), sink(usize::MAX, 27));
            assert_eq!(
                emit(Program::Success(json(9000)), blk, &mut out, &mut err),
                code,
                "{room}"
            );
            assert_eq!(out.bytes.len(), room);
        }
    }

    #[test]
    fn success_output_failure_is_caught_only_when_unbuffered() {
        let (mut out, mut err) = (sink(0, 32), sink(usize::MAX, 28));
        assert_eq!(
            emit(
                Program::Success(b"{}\n".to_vec()),
                Streams::block(),
                &mut out,
                &mut err
            ),
            120
        );
        assert!(
            String::from_utf8(err.bytes.clone())
                .unwrap()
                .ends_with("BrokenPipeError: [Errno 32] Broken pipe\n")
        );
        let (mut out, mut err) = (sink(1, 28), sink(usize::MAX, 28));
        assert_eq!(
            emit(
                Program::Success(b"{}\n".to_vec()),
                Streams::unbuffered(),
                &mut out,
                &mut err
            ),
            2
        );
        assert_eq!(out.bytes, b"{");
        assert_eq!(
            String::from_utf8(err.bytes).unwrap(),
            "候補の採用を停止: [Errno 28] No space left on device\n"
        );
        let (mut out, mut err) = (sink(0, 28), sink(0, 28));
        assert_eq!(
            emit(
                Program::Success(b"{}\n".to_vec()),
                Streams::unbuffered(),
                &mut out,
                &mut err
            ),
            1
        );
    }

    #[test]
    fn uncaught_tracebacks_name_the_old_frames() {
        let (mut out, mut err) = (sink(usize::MAX, 28), sink(usize::MAX, 28));
        assert_eq!(
            emit(
                Program::Uncaught(Uncaught::RootNotPath),
                Streams::block(),
                &mut out,
                &mut err
            ),
            1
        );
        let text = String::from_utf8(err.bytes).unwrap();
        assert!(text.contains("  File \"pathlib.py\", line 578, in _parse_args\n    a = os.fspath(a)\nTypeError: expected str, bytes or os.PathLike object, not list\n"));
        let mut err = sink(usize::MAX, 28);
        emit(
            Program::Uncaught(Uncaught::DigestNotString),
            Streams::block(),
            &mut out,
            &mut err,
        );
        let text = String::from_utf8(err.bytes).unwrap();
        assert!(text.ends_with("  File \"re.py\", line 195, in fullmatch\n    return _compile(pattern, flags).fullmatch(string)\nTypeError: expected string or bytes-like object\n"));
    }
}
