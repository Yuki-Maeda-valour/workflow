//! 旧 bash の実行時の再現: 組み込みの `echo` の書き出しと失敗、ERR の罠(`on_err`)、EXIT の罠
//! (`cleanup`)、終わりの信号、TERM・HUP・INT の罠(`on_signal`)、`die`、検査専用の止め所。
//!
//! - 罠の時点は 3 つ: 起動(152 行の前。bash は信号に処理を置かない)・EXIT の罠の後(152 行。bash は
//!   終わりの信号に `termsig_sighandler` を置く)・TERM・HUP・INT の罠の後(517〜519 行)。
//! - 命令の区切り(組み込みの命令の後・道具に当たる処理の後)で `checkpoint` を呼び、受けた信号を
//!   bash と同じに扱う(`QUIT` と `run_pending_traps` の位置)。
//! - 失敗した命令は、受けた信号を先に確かめてから、errexit の働く所なら ERR の罠へ進む。
//! - `die`・`on_err`・`on_signal` はログの有無(`LOG_FD_OPEN`)を値で持つ。この区間ではログを
//!   開かないので stderr だけに書く。R4X-2・R4X-3 がログの書き込みと生出力の節を足す。
use super::sites::{self, Site};
use super::stdio::{Io, Stream, Writer};
use dev_workflow_platform::implement_agent as os;

/// bash の文の先頭に出す、旧版の呼出名(2026-11-11 17:58・21:35 の決定の固定名)。
pub const SCRIPT_NAME: &[u8] = b"implement-agent.sh";

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

/// 罠の時点。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    /// 152 行より前(終わりの信号は既定の扱い)。
    Start,
    /// 152 行の EXIT の罠の後(終わりの信号は、EXIT の罠を走らせてからその信号で終わる)。
    ExitTrap,
    /// 517〜519 行の TERM・HUP・INT の罠の後。
    Traps,
}

/// 止め所の地点の種類(旧版の bash がしていること)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Wait {
    /// 前面の子を待つ(352 行の `grep`・715 行の `mkdir`)。
    Foreground,
    /// コマンド置換の中の子を待つ(390・391 行)。
    Comsub,
}

/// 検査専用の止め所。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Point {
    Grep,
    Dirname,
    Basename,
    Boundary,
}

impl Point {
    pub fn name(self) -> &'static str {
        match self {
            Self::Grep => "grep",
            Self::Dirname => "dirname",
            Self::Basename => "basename",
            Self::Boundary => "boundary",
        }
    }

    pub fn from_name(name: &[u8]) -> Option<Self> {
        [Self::Grep, Self::Dirname, Self::Basename, Self::Boundary]
            .into_iter()
            .find(|point| point.name().as_bytes() == name)
    }

    fn wait(self) -> Wait {
        match self {
            Self::Grep | Self::Boundary => Wait::Foreground,
            Self::Dirname | Self::Basename => Wait::Comsub,
        }
    }
}

/// 止め所の呼び返し。2 つ目の引数は、待つのを打ち切るか(その時点で bash が子を待たずに終わる
/// 信号を受けたか)を返す。製品の入口は何もしない呼び返しを渡す。
pub type StopHook = Box<dyn FnMut(Point, &dyn Fn() -> bool)>;

/// 差し込み点(`cfg(test)` のときだけ働く)。名前を受け、その場所で受けたことにする信号を返す。
#[cfg(test)]
pub type Hook = Box<dyn FnMut(&str) -> Option<i32>>;

/// ログの状態(`LOG_FD_OPEN` など)。この区間では開かない。
#[derive(Clone, Debug, Default)]
#[allow(dead_code)]
pub struct LogState {
    /// `LOG_FD_OPEN`(874 行で 1 にする。R4X-2)。
    pub open: bool,
}

/// 旧 bash の状態。
pub struct Shell {
    pub io: Box<dyn Io>,
    out: Stream,
    err: Stream,
    /// bash の `errno`(最後に失敗した書き込みの番号)。
    pub errno: i32,
    /// 罠の時点。
    pub phase: Phase,
    /// EXIT の罠が残っているか。
    exit_trap: bool,
    /// ERR の罠が残っているか(305 行で置き、`trap - ERR` で外れる)。
    pub err_trap: bool,
    /// `STATE_DIR`(EXIT の罠の `cleanup` が消す。この区間では空)。
    pub state_dir: Vec<u8>,
    /// TERM・HUP・INT に処理を置いたか(起動時に無視されていた信号には置けない)。
    pub hooked: [bool; 3],
    /// 終わりの信号のうち、処理を置いたもの。
    pub terminating: Vec<i32>,
    /// ログの状態(R4X-2 以降が読む)。
    #[allow(dead_code)]
    pub log: LogState,
    /// プロセスの信号の扱いを変えるか(単体テストでは変えない)。
    pub real_signals: bool,
    /// 止め所の呼び返し。
    pub stop: Option<StopHook>,
    /// ジョブを作るときに cwd を決められないか(`PWD` が無く、論理の cwd も無い。起動で cwd が消えた)。
    pub job_cwd_unknown: bool,
    #[cfg(test)]
    pub hook: Option<Hook>,
}

impl Shell {
    pub fn new(io: Box<dyn Io>) -> Self {
        Self {
            io,
            out: Stream::default(),
            err: Stream::default(),
            errno: 0,
            phase: Phase::Start,
            exit_trap: false,
            err_trap: false,
            state_dir: Vec::new(),
            hooked: [false; 3],
            terminating: Vec::new(),
            log: LogState::default(),
            real_signals: true,
            stop: None,
            job_cwd_unknown: false,
            #[cfg(test)]
            hook: None,
        }
    }

    // ---- 差し込み点 ----

    /// 差し込み点。検査では、その場所で信号を受けたことにする。
    pub fn point(&mut self, name: &str) {
        #[cfg(test)]
        if let Some(hook) = self.hook.as_mut()
            && let Some(signal) = hook(name)
        {
            os::simulate_signal(signal);
        }
        let _ = name;
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

    /// bash の組み込みの誤りの文(`builtin_error`): `implement-agent.sh: line <行>: <命令>: <文>`。
    pub fn builtin_error(&mut self, line: u32, command: &[u8], text: &[u8]) {
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

    /// `internal_warning`: `implement-agent.sh: line <行>: warning: <文>`。
    pub fn internal_warning(&mut self, line: u32, text: &[u8]) {
        let mut body = b"warning: ".to_vec();
        body.extend_from_slice(text);
        self.builtin_error(line, b"", &body);
    }

    /// `sh_wrerror`: `write error: <strerror(errno)>`。
    fn write_error(&mut self, line: u32, command: &[u8]) {
        let mut text = b"write error: ".to_vec();
        text.extend_from_slice(super::strerror(self.errno).as_bytes());
        self.builtin_error(line, command, &text);
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

    /// 境界の印(検査専用入口だけ)。書き込みの失敗は見ない(終了値は 99 のまま)。
    pub fn boundary_message(&mut self, text: &[u8]) {
        let mut message = text.to_vec();
        message.push(b'\n');
        let mut ignored = 0;
        let mut writer = Writer {
            io: self.io.as_mut(),
            errno: &mut ignored,
        };
        let _ = write_piece(&mut writer, 2, &message);
    }

    /// 起動しない道具の文(道具の stderr は bash の fd 2)。`pieces` は、道具が 1 回ずつ書く単位。
    /// 道具は別のプロセスなので、bash の errno と bash の stderr の溜め場所は変えない。
    /// 道具が書き込みで信号を受けて終わったら、その番号を返す。
    pub fn tool_write(&mut self, fd: i32, pieces: &[Vec<u8>]) -> ToolWrite {
        let mut ignored = 0;
        let mut writer = Writer {
            io: self.io.as_mut(),
            errno: &mut ignored,
        };
        let mut failed = None;
        for piece in pieces {
            let (_, ok) = write_piece(&mut writer, fd, piece);
            if !ok {
                failed = Some(ignored);
                break;
            }
        }
        match (self.child_died(), failed) {
            (Some(signal), _) => ToolWrite::Signal(signal),
            (None, Some(errno)) => ToolWrite::Failed(errno),
            (None, None) => ToolWrite::Done,
        }
    }

    /// 組み込みの `echo "<文>"`(`>&2`・`>&8` は fd 2 と同じ書き先)。成否を返す(罠と ERR は呼ばない)。
    /// 終わりの信号は bash の `QUIT` と同じ所で確かめる。
    fn echo_raw(&mut self, site: Site, text: &[u8]) -> Flow<bool> {
        let fd = 2;
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

    /// `echo "<文>" >&2` の 1 命令。errexit の働く所で失敗すれば ERR の罠へ進む。
    pub fn echo_err(&mut self, site: Site, text: &[u8]) -> Flow<bool> {
        let ok = self.echo_raw(site, text)?;
        self.after(site, ok, 1)
    }

    /// 命令の後: 受けた信号を確かめ、失敗なら ERR の罠へ進む(働かない所では失敗を返す)。
    pub fn after(&mut self, site: Site, ok: bool, status: i32) -> Flow<bool> {
        self.checkpoint()?;
        if !ok && site.errexit {
            return Err(self.fail(status, site.line));
        }
        Ok(ok)
    }

    /// errexit で終わる: ERR の罠があれば `on_err`、無ければその終了値で終わる。
    pub fn fail(&mut self, status: i32, line: u32) -> Exit {
        if self.err_trap {
            return self.on_err(status, line);
        }
        self.exit(u8::try_from(status & 0xff).unwrap_or(1))
    }

    /// bash の `QUIT`(終わりの信号を受けていれば、EXIT の罠を走らせてその信号で終わる)。
    pub fn quit(&mut self) -> Flow<()> {
        let pending = os::pending();
        let mut candidates: Vec<i32> = self.terminating.clone();
        if self.phase == Phase::ExitTrap {
            for (index, signal) in os::TRAP_SIGNALS.iter().enumerate() {
                if self.hooked.get(index).copied().unwrap_or(false) {
                    candidates.push(*signal);
                }
            }
        }
        candidates.sort_unstable();
        let Some(signal) = candidates
            .into_iter()
            .find(|&signal| pending & (1u32 << signal) != 0)
        else {
            return Ok(());
        };
        Err(self.die_by(signal))
    }

    /// termsig_handler: EXIT の罠を走らせてから、その信号で終わる。
    pub fn die_by(&mut self, signal: i32) -> Exit {
        os::clear_pending(signal);
        self.run_exit_trap();
        self.flush_all();
        if self.real_signals {
            os::die_by_signal(signal);
        }
        Exit::Signal(signal)
    }

    /// 道具(子のプロセス)の書き込みで終わりの信号を受けたか。道具は起動時の扱いを受け継ぐので、
    /// 既定なら道具がその信号で終わる(bash は終わらない)。記録を消して、道具が終わったかを返す。
    pub fn child_died(&mut self) -> Option<i32> {
        let pending = os::pending();
        let mut died = None;
        for &signal in &[os::SIGPIPE, os::SIGXFSZ] {
            if pending & (1u32 << signal) != 0 {
                os::clear_pending(signal);
                let inherited =
                    os::inherited_disposition(signal).unwrap_or(os::Disposition::Default);
                if inherited != os::Disposition::Ignore && died.is_none() {
                    died = Some(signal);
                }
            }
        }
        died
    }

    /// 命令の区切り(`QUIT` と `run_pending_traps`)。
    pub fn checkpoint(&mut self) -> Flow<()> {
        self.quit()?;
        if self.phase == Phase::Traps {
            let pending = os::pending();
            // run_pending_traps は番号の順に走らせる(HUP・INT・TERM)。
            let mut order: Vec<(i32, usize)> = os::TRAP_SIGNALS
                .iter()
                .enumerate()
                .map(|(index, signal)| (*signal, index))
                .collect();
            order.sort_unstable();
            for (signal, index) in order {
                if self.hooked.get(index).copied().unwrap_or(false)
                    && pending & (1u32 << signal) != 0
                {
                    return Err(self.on_signal(signal));
                }
            }
        }
        Ok(())
    }

    /// その地点で bash が子を待たずに終わる信号を受けたか(止め所の待ちを打ち切る)。
    pub fn dies_now(&self, wait: Wait) -> bool {
        self.dies_now_signal(wait).is_some()
    }

    /// `dies_now` の信号(番号の小さい順)。
    pub fn dies_now_signal(&self, wait: Wait) -> Option<i32> {
        let pending = os::pending();
        let mut candidates: Vec<i32> = self
            .terminating
            .iter()
            .copied()
            .filter(|s| pending & (1u32 << s) != 0)
            .collect();
        if self.phase == Phase::ExitTrap {
            for (index, &signal) in os::TRAP_SIGNALS.iter().enumerate() {
                if self.hooked.get(index).copied().unwrap_or(false)
                    && pending & (1u32 << signal) != 0
                    && !(signal == os::SIGINT && wait == Wait::Foreground)
                {
                    candidates.push(signal);
                }
            }
        }
        candidates.into_iter().min()
    }

    /// 前面の子を待ち終えた後(352 行・715 行)。罠の無い SIGINT は、子が普通に終われば捨てる
    /// (bash の `wait_sigint_handler`)。
    pub fn after_foreground(&mut self, child_died_by_int: bool) -> Flow<()> {
        if self.phase == Phase::ExitTrap
            && self.hooked.get(2).copied().unwrap_or(false)
            && os::pending() & (1u32 << os::SIGINT) != 0
            && !child_died_by_int
        {
            os::clear_pending(os::SIGINT);
        }
        self.checkpoint()
    }

    /// 止め所(検査専用入口だけが止まる)。`launched` は、Rust がその地点の道具を起動したか
    /// (起動したなら PATH の包みが止めるので、ここでは止めない)。
    pub fn stop_point(&mut self, point: Point, launched: bool) -> Flow<()> {
        self.point(point.name());
        if !launched && let Some(mut stop) = self.stop.take() {
            let wait = point.wait();
            {
                let me: &Shell = self;
                stop(point, &|| me.dies_now(wait));
            }
            self.stop = Some(stop);
        }
        match point.wait() {
            Wait::Foreground => self.after_foreground(false),
            Wait::Comsub => self.checkpoint(),
        }
    }

    /// ジョブを作るときの bash の cwd(`job_working_directory`): `PWD` が無く、論理の cwd も無く、getcwd が
    /// 失敗すれば `job-working-directory: error retrieving current directory: …` を出す(ジョブごと)。
    pub fn job_working_directory(&mut self) {
        if !self.job_cwd_unknown {
            return;
        }
        match super::paths::getcwd() {
            Ok(_) => self.job_cwd_unknown = false,
            Err(errno) => {
                let mut text = b"job-working-directory: error retrieving current directory: getcwd: cannot access parent directories: ".to_vec();
                text.extend_from_slice(super::strerror(errno).as_bytes());
                self.raw_stderr_line(&text);
            }
        }
    }

    // ---- 罠 ----

    /// `on_err` :282-304(子もログも無いとき)。`echo … >&8 || true` の失敗は無視して終了値 20。
    pub fn on_err(&mut self, status: i32, line: u32) -> Exit {
        self.err_trap = false;
        let text = format!("ERROR [internal] 予期しない失敗(終了コード {status}・行 {line})");
        match self.echo_raw(sites::ON_ERR_ECHO, text.as_bytes()) {
            Ok(_) => {}
            Err(exit) => return exit,
        }
        self.exit(20)
    }

    /// `die` :269-278(ログが無いとき)。echo が失敗すれば、ERR の罠(行 275)へ進む。
    pub fn die(&mut self, code: u8, reason: &str, text: &[u8]) -> Exit {
        let mut line = format!("ERROR [{reason}] ").into_bytes();
        line.extend_from_slice(text);
        match self.echo_err(sites::DIE_ECHO, &line) {
            Ok(_) => {}
            Err(exit) => return exit,
        }
        self.err_trap = false;
        self.exit(code)
    }

    /// `fail_usage` :334: `echo "ERROR [usage] $*" >&2; trap - ERR; exit 2`。
    pub fn fail_usage(&mut self, text: &[u8]) -> Exit {
        let mut line = b"ERROR [usage] ".to_vec();
        line.extend_from_slice(text);
        match self.echo_err(sites::FAIL_USAGE_ECHO, &line) {
            Ok(_) => {}
            Err(exit) => return exit,
        }
        self.err_trap = false;
        self.exit(2)
    }

    /// `exit <値>`。EXIT の罠(`cleanup`)が残っていれば走らせる。
    pub fn exit(&mut self, code: u8) -> Exit {
        self.run_exit_trap();
        Exit::Code(code)
    }

    /// EXIT の罠(`cleanup` :149-151)。`STATE_DIR` が空なら何もしない(この区間ではいつも空)。
    pub fn run_exit_trap(&mut self) {
        if self.exit_trap {
            self.exit_trap = false;
            if !self.state_dir.is_empty() {
                // R4X-2 で `rm -rf "$STATE_DIR"` を移す。この区間では STATE_DIR を作らない。
                self.state_dir.clear();
            }
        }
    }

    /// `on_signal` :496-515(子もログも無いとき)。
    pub fn on_signal(&mut self, signal: i32) -> Exit {
        let (name, code) = match signal {
            s if s == os::SIGTERM => ("TERM", 143u8),
            s if s == os::SIGHUP => ("HUP", 129u8),
            _ => ("INT", 130u8),
        };
        os::clear_pending(signal);
        // trap - TERM HUP INT ERR: 罠を外す。EXIT の罠があるので、bash 5.2.21 は外した信号に終わりの信号の処理を
        // 戻す(もう一度受けたら、EXIT の罠を走らせてからその信号で終わる)。受けたことの記録は続ける。
        self.err_trap = false;
        self.phase = Phase::ExitTrap;
        let text = format!(
            "ERROR [aborted] シグナル {name} を受信したため中止した(外部ランナーのプロセスグループと直接の子プロセスに終了のシグナルを送った)"
        );
        match self.echo_raw(sites::ON_SIGNAL_ECHO, text.as_bytes()) {
            Ok(true) => {}
            // set -e: 罠の中の echo の失敗で、その終了値 1 で終わる(ERR の罠は外した)。
            Ok(false) => return self.exit(1),
            Err(exit) => return exit,
        }
        self.exit(code)
    }

    /// 152 行: `trap cleanup EXIT`。bash は、起動時に無視されていない終わりの信号に処理を置く
    /// (TERM・HUP・INT を含む)。
    pub fn set_exit_trap(&mut self) {
        self.exit_trap = true;
        self.phase = Phase::ExitTrap;
        for signal in os::TERMINATING_SIGNALS {
            let inherited = os::inherited_disposition(signal).unwrap_or(os::Disposition::Default);
            if inherited == os::Disposition::Ignore {
                continue;
            }
            if !self.real_signals || os::install_recorder(signal).is_ok() {
                self.terminating.push(signal);
            }
        }
        for (index, signal) in os::TRAP_SIGNALS.iter().enumerate() {
            let inherited = os::inherited_disposition(*signal).unwrap_or(os::Disposition::Default);
            let installed = inherited != os::Disposition::Ignore
                && (!self.real_signals || os::install_recorder(*signal).is_ok());
            if let Some(slot) = self.hooked.get_mut(index) {
                *slot = installed;
            }
        }
    }

    /// 517〜519 行: TERM・HUP・INT の罠(起動時に無視されていた信号には置けない)。
    pub fn set_signal_traps(&mut self) {
        self.phase = Phase::Traps;
    }

    /// 終わる時の stdio の書き出し(glibc の `exit` は stderr、stdout の順に溜めた物を書く)。
    pub fn flush_all(&mut self) {
        let (out, err, mut writer) = self.writer_parts();
        err.flush(&mut writer, 2);
        out.flush(&mut writer, 1);
    }
}

/// 道具の書き込みの結果。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ToolWrite {
    Done,
    /// 書き込みが失敗した(番号)。
    Failed(i32),
    /// 道具がその信号で終わった。
    Signal(i32),
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
