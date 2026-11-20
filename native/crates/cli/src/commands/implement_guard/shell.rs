//! 旧 bash の実行時の再現: 組み込みの `echo`・`printf` の書き出しと失敗、ERR の罠、EXIT の罠
//! (`cleanup_tmp`)、TERM・INT・HUP の罠(`on_signal`)、起動しない道具の文の書き出し。
//!
//! - 命令の区切り(組み込みの命令の後・道具に当たる処理の後)で `checkpoint` を呼び、罠の信号を
//!   受けていれば `on_signal` を行う(bash の `run_pending_traps` と同じ位置)。
//! - 失敗した命令は、罠の信号を先に確かめてから、errexit の働く所なら ERR の罠へ進む。
use super::sites::{self, Site};
use super::stdio::{Io, Stream, Writer};
use dev_workflow_platform::implement_guard as os;

/// bash の文の先頭に出す、旧版の呼出名(2026-11-11 17:58・21:35 の決定の固定名)。
pub const SCRIPT_NAME: &[u8] = b"implement-guard.sh";

/// 旧版のサブコマンド。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Sub {
    Take,
    ConfigCheck,
    Compare,
    TaskmdDiff,
    RestoreTaskmd,
    Cleanup,
}

impl Sub {
    pub fn name(self) -> &'static [u8] {
        match self {
            Self::Take => b"take",
            Self::ConfigCheck => b"config-check",
            Self::Compare => b"compare",
            Self::TaskmdDiff => b"taskmd-diff",
            Self::RestoreTaskmd => b"restore-taskmd",
            Self::Cleanup => b"cleanup",
        }
    }
}

/// 終わり方。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Exit {
    /// `exit <値>`。
    Code(u8),
    /// 信号で終わった(単体テストだけ。製品の経路では、その信号で本当に終わる)。
    Signal(i32),
}

/// 旧版の流れ。`Err` は旧版がそこで終わったこと。
pub type Flow<T> = Result<T, Exit>;

/// 差し込み点(`cfg(test)` のときだけ働く)。
#[cfg(test)]
pub type Hook = Box<dyn FnMut(&str) -> Option<i32>>;

/// 差し込み点が返す値のうち、罠の信号を 1 つ後の区切りで確かめさせる印(単体テストだけ)。
/// `-(DEFER + 信号)` を返すと、道具の直後の区切りでは確かめず、次の区切りで罠が動く(決定の表の
/// 「旧 bash の信号」の 2 通りのうち「次の組み込みの命令の後」を作る)。
#[cfg(test)]
pub const DEFER: i32 = 64;

/// 旧 bash の状態。
pub struct Shell {
    pub io: Box<dyn Io>,
    out: Stream,
    err: Stream,
    /// bash の `errno`(最後に失敗した書き込みの番号)。
    pub errno: i32,
    /// `SUB`(`on_signal` が restore-taskmd かどうかを見る)。
    pub sub: Option<Sub>,
    /// `WORK`(EXIT の罠と `on_signal` が消す)。
    pub work: Option<Vec<u8>>,
    /// EXIT の罠が残っているか(`trap - EXIT` で外れる)。
    exit_trap: bool,
    /// 罠を置いた信号(起動時に無視されていた信号には置けない)。
    pub trapped: [bool; 3],
    /// 終わりの信号のうち、処理を置いたもの(bash の `initialize_terminating_signals`)。
    pub terminating: Vec<i32>,
    /// 起動時に引き継いだ TERM・INT・HUP の扱い(`trap -` で戻す先)。
    pub inherited: [os::Disposition; 3],
    /// 起動時に引き継いだ `SIGPIPE`・`SIGXFSZ` の扱い(使い方の `sed` が受け継ぐ)。
    pub pipe: os::Disposition,
    pub file_size: os::Disposition,
    /// `restore-taskmd` の `TOUCHED_STATE`(旧版は常に none)。
    pub touched_state: Vec<u8>,
    /// 受け継いだ環境の `RESTORE_TEMP_STATE`(`restore_fail` の `TEMP=` に出る)。
    pub restore_temp_state: Option<Vec<u8>>,
    /// プロセスの信号の扱いを変えるか(単体テストでは変えない)。
    pub real_signals: bool,
    #[cfg(test)]
    pub hook: Option<Hook>,
    /// 罠の信号を確かめた区切りの種類(単体テストだけ。`builtin`・`tool`・`other`)。
    #[cfg(test)]
    pub signal_boundary: Option<&'static str>,
    #[cfg(test)]
    boundary: &'static str,
}

impl Shell {
    pub fn new(io: Box<dyn Io>) -> Self {
        Self {
            io,
            out: Stream::default(),
            err: Stream::default(),
            errno: 0,
            sub: None,
            work: None,
            exit_trap: true,
            trapped: [false; 3],
            terminating: Vec::new(),
            inherited: [os::Disposition::Default; 3],
            pipe: os::Disposition::Default,
            file_size: os::Disposition::Default,
            touched_state: b"none".to_vec(),
            restore_temp_state: None,
            real_signals: true,
            #[cfg(test)]
            hook: None,
            #[cfg(test)]
            signal_boundary: None,
            #[cfg(test)]
            boundary: "other",
        }
    }

    // ---- 差し込み点 ----

    /// 差し込み点。検査では、その場所で信号を受けたことにするか、OS の失敗の番号を返す。
    pub fn point(&mut self, name: &str) -> Option<i32> {
        #[cfg(test)]
        if let Some(hook) = self.hook.as_mut() {
            return hook(name);
        }
        let _ = name;
        None
    }

    // ---- 出力 ----

    fn writer_parts(&mut self) -> (&mut Stream, &mut Stream, Writer<'_>) {
        let Self {
            io,
            out,
            err,
            errno,
            ..
        } = self;
        (
            out,
            err,
            Writer {
                io: io.as_mut(),
                errno,
            },
        )
    }

    /// bash の組み込みの誤りの文(`builtin_error`): `implement-guard.sh: line <行>: <命令>: <文>`。
    fn builtin_error(&mut self, line: u32, command: &[u8], text: &[u8]) {
        let mut message = SCRIPT_NAME.to_vec();
        message.extend_from_slice(format!(": line {line}: ").as_bytes());
        if !command.is_empty() {
            message.extend_from_slice(command);
            message.extend_from_slice(b": ");
        }
        message.extend_from_slice(text);
        message.push(b'\n');
        let (_, err, mut writer) = self.writer_parts();
        err.vfprintf(&mut writer, 2, &message);
    }

    /// `sh_wrerror`: `write error: <strerror(errno)>`。
    fn write_error(&mut self, line: u32, command: &[u8]) {
        let mut text = b"write error: ".to_vec();
        text.extend_from_slice(super::strerror(self.errno).as_bytes());
        self.builtin_error(line, command, &text);
    }

    /// bash のリダイレクトの失敗の文: `implement-guard.sh: line <行>: <パス>: <strerror>`。
    pub fn redirect_error(&mut self, line: u32, path: &[u8], errno: i32) {
        let mut text = path.to_vec();
        text.extend_from_slice(b": ");
        text.extend_from_slice(super::strerror(errno).as_bytes());
        self.builtin_error(line, b"", &text);
    }

    /// 組み込みの命令の誤りの文(`read` の読み取りの誤りなど)。
    pub fn redirect_error_text(&mut self, line: u32, command: &[u8], text: &[u8]) {
        self.builtin_error(line, command, text);
    }

    /// `printf -v` の診断(`implement-guard.sh: line <行>: printf: <文>`。文は改行で終わる)。
    pub fn printf_note(&mut self, line: u32, text: &[u8]) {
        let body = text.strip_suffix(b"\n").unwrap_or(text);
        self.builtin_error(line, b"printf", body);
    }

    /// bash の起動時の警告と誤り(`bash: …`。決定の固定名)。
    pub fn startup_message(&mut self, text: &[u8]) {
        let mut message = b"bash: ".to_vec();
        message.extend_from_slice(text);
        message.push(b'\n');
        let (_, err, mut writer) = self.writer_parts();
        err.vfprintf(&mut writer, 2, &message);
    }

    /// bash が stderr へ直接出す 1 行(`shell-init: …` など。改行を足す)。
    pub fn raw_stderr_line(&mut self, text: &[u8]) {
        let mut message = text.to_vec();
        message.push(b'\n');
        let (_, err, mut writer) = self.writer_parts();
        err.vfprintf(&mut writer, 2, &message);
    }

    /// 境界の印(検査専用入口だけ)。
    pub fn boundary_message(&mut self, text: &[u8]) {
        self.raw_stderr_line(text);
    }

    /// 起動しない道具の文(道具の stderr は bash の fd 2。GNU の `error` と同じく、名前・本文・
    /// `: <strerror>`・改行の 4 回に分けて書く)。
    /// 道具が書き込みで信号を受けて終わったら、その番号を返す。
    pub fn tool_error(&mut self, tool: &[u8], text: &[u8], errno: Option<i32>) -> Option<i32> {
        let mut pieces: Vec<Vec<u8>> = Vec::new();
        let mut head = tool.to_vec();
        head.extend_from_slice(b": ");
        pieces.push(head);
        pieces.push(text.to_vec());
        if let Some(errno) = errno {
            let mut tail = b": ".to_vec();
            tail.extend_from_slice(super::strerror(errno).as_bytes());
            pieces.push(tail);
        }
        pieces.push(b"\n".to_vec());
        // 道具は別のプロセスなので、bash の errno は変えない。
        let mut ignored = 0;
        let mut writer = Writer {
            io: self.io.as_mut(),
            errno: &mut ignored,
        };
        for piece in pieces {
            let (_, ok) = write_piece(&mut writer, 2, &piece);
            if !ok {
                break;
            }
        }
        self.child_died()
    }

    /// 組み込みの `echo "<文>"`(`fd` は 1 か、`>&2` なら 2)。成否を返す(罠と ERR は呼ばない)。
    /// 終わりの信号は bash の `QUIT` と同じ所で確かめる。
    fn echo_raw(&mut self, site: Site, fd: i32, text: &[u8]) -> Flow<bool> {
        {
            let (out, _, mut writer) = self.writer_parts();
            out.clear_error();
            out.vfprintf(&mut writer, fd, text);
        }
        self.quit()?;
        {
            let (out, _, mut writer) = self.writer_parts();
            out.putc(&mut writer, fd, b'\n');
        }
        self.quit()?;
        // sh_chkwrite: QUIT; fflush; QUIT。
        let failed = {
            let (out, _, mut writer) = self.writer_parts();
            out.flush(&mut writer, fd);
            out.error
        };
        self.quit()?;
        if failed {
            self.write_error(site.line, b"echo");
            self.out.purge();
            self.out.clear_error();
            return Ok(false);
        }
        Ok(true)
    }

    /// 組み込みの `printf`。書式は、そのまま出す部分と `%s` の引数の並び。
    fn printf_raw(&mut self, site: Site, parts: &[Part<'_>]) -> Flow<bool> {
        for part in parts {
            match part {
                Part::Text(text) => {
                    for &ch in text.iter() {
                        {
                            let (out, _, mut writer) = self.writer_parts();
                            out.putc(&mut writer, 1, ch);
                        }
                        // PC: putchar の後の QUIT。
                        self.quit()?;
                    }
                }
                Part::Arg(value) => {
                    let failed = {
                        let (out, _, mut writer) = self.writer_parts();
                        out.clear_error();
                        out.vfprintf(&mut writer, 1, value);
                        out.error
                    };
                    self.quit()?;
                    if failed {
                        self.write_error(site.line, b"printf");
                        self.out.clear_error();
                        return Ok(false);
                    }
                }
            }
        }
        // PRETURN: QUIT; ferror でなければ fflush; QUIT; ferror。
        self.quit()?;
        let failed = {
            let (out, _, mut writer) = self.writer_parts();
            if !out.error {
                out.flush(&mut writer, 1);
            }
            out.error
        };
        self.quit()?;
        if failed {
            self.write_error(site.line, b"printf");
            self.out.clear_error();
            return Ok(false);
        }
        Ok(true)
    }

    /// `echo "<文>" >&2` の 1 命令。errexit の働く所で失敗すれば ERR の罠へ進む。
    pub fn echo_err(&mut self, site: Site, text: &[u8]) -> Flow<bool> {
        let ok = self.echo_raw(site, 2, text)?;
        self.after(site, ok, 1)
    }

    /// `printf …` の 1 命令(stdout)。
    pub fn printf(&mut self, site: Site, parts: &[Part<'_>]) -> Flow<bool> {
        let ok = self.printf_raw(site, parts)?;
        self.after(site, ok, 1)
    }

    /// 命令の後: 罠の信号を確かめ、失敗なら ERR の罠へ進む(働かない所では失敗を返す)。
    pub fn after(&mut self, site: Site, ok: bool, status: i32) -> Flow<bool> {
        #[cfg(test)]
        {
            self.boundary = "builtin";
        }
        self.checkpoint()?;
        if !ok && site.errexit {
            return Err(self.err_trap(status, site.err_line));
        }
        Ok(ok)
    }

    /// bash の `QUIT`(終わりの信号を受けていれば、EXIT の罠を走らせてその信号で終わる)。
    pub fn quit(&mut self) -> Flow<()> {
        let pending = os::pending_traps();
        let Some(&signal) = self
            .terminating
            .iter()
            .find(|&&signal| pending & (1u32 << signal) != 0)
        else {
            return Ok(());
        };
        // termsig_handler: EXIT の罠を走らせてから、その信号で終わる。
        os::clear_trap(signal);
        if self.exit_trap {
            self.exit_trap = false;
            self.cleanup_tmp();
        }
        self.flush_all();
        if self.real_signals {
            os::die_by_signal(signal);
        }
        Err(Exit::Signal(signal))
    }

    /// 道具(子のプロセス)の書き込みで終わりの信号を受けたか。道具は起動時の扱いを受け継ぐので、
    /// 既定なら道具がその信号で終わる(bash は終わらない)。記録を消して、道具が終わったかを返す。
    pub fn child_died(&mut self) -> Option<i32> {
        let pending = os::pending_traps();
        let mut died = None;
        for &signal in &[os::SIGPIPE, os::SIGXFSZ] {
            if pending & (1u32 << signal) != 0 {
                os::clear_trap(signal);
                if self.terminating.contains(&signal) && died.is_none() {
                    died = Some(signal);
                }
            }
        }
        died
    }

    /// 命令の区切り(`run_pending_traps`)。
    pub fn checkpoint(&mut self) -> Flow<()> {
        self.quit()?;
        if self.trap_pending() {
            #[cfg(test)]
            {
                self.signal_boundary = Some(self.boundary);
            }
            return Err(self.on_signal());
        }
        #[cfg(test)]
        {
            self.boundary = "other";
        }
        Ok(())
    }

    /// 道具に当たる処理の後の区切り。検査では、その場所で信号を受けたことにできる。
    pub fn tool_point(&mut self, name: &str) -> Flow<()> {
        if let Some(signal) = self.point(name)
            && signal < 0
        {
            #[cfg(test)]
            if -signal > DEFER {
                // 道具の直後の区切りでは確かめない(罠は次の区切りで動く)。
                os::simulate_trap(-signal - DEFER);
                return Ok(());
            }
            os::simulate_trap(-signal);
        }
        #[cfg(test)]
        {
            self.boundary = "tool";
        }
        self.checkpoint()
    }

    fn trap_pending(&self) -> bool {
        let pending = os::pending_traps();
        os::TRAP_SIGNALS
            .iter()
            .zip(self.trapped.iter())
            .any(|(&signal, &trapped)| trapped && pending & (1u32 << signal) != 0)
    }

    // ---- 罠 ----

    /// ERR の罠: `echo "ERROR [internal] 予期しない失敗(終了コード $ec・行 $LINENO)" >&2; exit 20`。
    /// 罠の中の echo が失敗すれば、罠の中の errexit で終了値 1。
    pub fn err_trap(&mut self, status: i32, line: u32) -> Exit {
        let text = format!("ERROR [internal] 予期しない失敗(終了コード {status}・行 {line})");
        match self.echo_raw(sites::ERR_TRAP_ECHO, 2, text.as_bytes()) {
            Ok(true) => self.exit(20),
            Ok(false) => self.exit(1),
            Err(exit) => exit,
        }
    }

    /// `exit <値>`。EXIT の罠(`cleanup_tmp`)が残っていれば走らせる。
    pub fn exit(&mut self, code: u8) -> Exit {
        if self.exit_trap {
            self.exit_trap = false;
            let has_work = self.work.as_ref().is_some_and(|work| !work.is_empty());
            self.cleanup_tmp();
            // EXIT の罠の `rm -rf -- "$WORK"` は前面の道具なので、bash は終わった後に罠の信号を確かめる
            // (受けていれば on_signal で終了値 20)。
            if has_work && let Err(exit) = self.tool_point("rm-work") {
                return exit;
            }
        }
        Exit::Code(code)
    }

    /// `trap - ERR; exit <値>`(errexit の後始末だけ。EXIT の罠は走る)。
    pub fn finish(&mut self, code: u8) -> Exit {
        self.exit(code)
    }

    /// `cleanup_tmp` :299-313(S3I-A の経路では `WORK` だけを持つ)。
    pub fn cleanup_tmp(&mut self) {
        if let Some(work) = self.work.clone()
            && !work.is_empty()
        {
            super::cleanup::remove_tree(self, &work);
        }
    }

    /// `on_signal` :315-321。
    pub fn on_signal(&mut self) -> Exit {
        let restore = self.sub == Some(Sub::RestoreTaskmd);
        if restore {
            if self.real_signals {
                for signal in os::TRAP_SIGNALS {
                    let _ = os::set_disposition(signal, os::Disposition::Ignore);
                }
            }
            self.trapped = [false; 3];
        }
        // 罠の中は、ほかの罠の信号を確かめない(bash は走っている罠の中で同じ罠を走らせない)。
        os::clear_traps();
        self.cleanup_tmp();
        if restore {
            let touched = self.touched_state.clone();
            let ok = self.printf_raw(
                sites::ON_SIGNAL_PRINTF,
                &[
                    Part::Text(b"RESTORED=no\nTOUCHED="),
                    Part::Arg(&touched),
                    Part::Text(b"\nTEMP=none\nREASON=interrupted\n"),
                ],
            );
            match ok {
                Ok(true) => {}
                Ok(false) => return self.err_trap(1, sites::ON_SIGNAL_PRINTF.err_line),
                Err(exit) => return exit,
            }
        }
        self.exit_trap = false;
        Exit::Code(20)
    }

    /// TERM・INT・HUP の罠を置く(起動時に無視されていた信号には置けない)。その前に、EXIT の罠を
    /// 置いた bash と同じく、起動時に無視されていない終わりの信号に処理を置く。
    pub fn install_traps(&mut self) {
        for signal in os::TERMINATING_SIGNALS {
            let inherited = os::disposition(signal).unwrap_or(os::Disposition::Default);
            if inherited != os::Disposition::Ignore && os::install_trap(signal).is_ok() {
                self.terminating.push(signal);
            }
        }
        for (index, signal) in os::TRAP_SIGNALS.iter().enumerate() {
            let inherited = os::disposition(*signal).unwrap_or(os::Disposition::Default);
            if let Some(slot) = self.inherited.get_mut(index) {
                *slot = inherited;
            }
            let installed =
                inherited != os::Disposition::Ignore && os::install_trap(*signal).is_ok();
            if let Some(slot) = self.trapped.get_mut(index) {
                *slot = installed;
            }
        }
    }

    /// `trap - EXIT ERR TERM INT HUP`(exec の前)。TERM・INT・HUP を起動時の扱いへ戻す。
    pub fn reset_traps(&mut self) {
        self.exit_trap = false;
        for signal in std::mem::take(&mut self.terminating) {
            let _ = os::set_disposition(signal, os::Disposition::Default);
            os::clear_trap(signal);
        }
        for (index, signal) in os::TRAP_SIGNALS.iter().enumerate() {
            let inherited = self
                .inherited
                .get(index)
                .copied()
                .unwrap_or(os::Disposition::Default);
            let _ = os::set_disposition(*signal, inherited);
        }
        self.trapped = [false; 3];
    }

    /// 終わる時の stdio の書き出し(glibc の `exit` は stderr、stdout の順に溜めた物を書く)。
    pub fn flush_all(&mut self) {
        let (out, err, mut writer) = self.writer_parts();
        err.flush(&mut writer, 2);
        out.flush(&mut writer, 1);
    }
}

/// 道具の 1 回の書き込み(全部書けるか失敗するまで)。
pub fn write_piece(writer: &mut Writer<'_>, fd: i32, data: &[u8]) -> (usize, bool) {
    let mut done = 0usize;
    while done < data.len() {
        match writer.io.write(fd, data.get(done..).unwrap_or_default()) {
            Ok(0) => continue,
            Ok(n) => done += n,
            Err(error) => {
                *writer.errno = error.raw_os_error().unwrap_or(os::errno::EIO);
                return (done, false);
            }
        }
    }
    (done, true)
}

/// `printf` の書式の部品。
pub enum Part<'a> {
    /// 書式の中の字面(1 文字ずつ `putchar`)。
    Text(&'a [u8]),
    /// `%s` の引数。
    Arg(&'a [u8]),
}
