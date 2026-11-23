//! S2Lのwrite時と終了時flushの失敗を分ける。
use super::{
    args::Arguments,
    error::{self, LegacyException as E},
    runtime::LegacyRuntime,
};
use std::io::{self, Write};
const BUFFER: usize = 8192;
struct StdoutBuffer<'a> {
    writer: &'a mut dyn Write,
    pending: Vec<u8>,
    text: Vec<u8>,
    unbuffered: bool,
    line_buffered: bool,
}
impl StdoutBuffer<'_> {
    fn flush_binary(&mut self) -> io::Result<()> {
        let mut used = 0;
        while used < self.pending.len() {
            match self.writer.write(&self.pending[used..]) {
                Ok(0) => {
                    self.pending.drain(..used);
                    return Err(io::ErrorKind::WriteZero.into());
                }
                Ok(n) => used += n,
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(e) => {
                    self.pending.drain(..used);
                    return Err(e);
                }
            }
        }
        self.pending.clear();
        Ok(())
    }
    fn binary(&mut self, raw: &[u8]) -> io::Result<()> {
        if self.unbuffered {
            return self.writer.write_all(raw);
        }
        if raw.len() <= BUFFER.saturating_sub(self.pending.len()) {
            self.pending.extend(raw);
            return Ok(());
        }
        self.flush_binary()?;
        if raw.len() > BUFFER {
            self.writer.write_all(raw)
        } else {
            self.pending.extend(raw);
            Ok(())
        }
    }
    fn flush_text(&mut self) -> io::Result<()> {
        let text = std::mem::take(&mut self.text);
        self.binary(&text)
    }
    fn write(&mut self, raw: &[u8], binary: bool) -> io::Result<()> {
        if binary {
            return self.binary(raw);
        }
        if self.unbuffered {
            return self.writer.write_all(raw);
        }
        self.text.extend(raw);
        if self.text.len() >= BUFFER
            || (self.line_buffered && raw.iter().any(|c| *c == 10 || *c == 13))
        {
            self.flush_text()?;
        }
        if self.line_buffered && raw.iter().any(|c| *c == 10 || *c == 13) {
            self.flush_binary()?;
        }
        Ok(())
    }
    fn finish(&mut self) -> io::Result<()> {
        self.flush_text()?;
        self.flush_binary()?;
        self.writer.flush()
    }
}
fn output_frame(error: E, args: Option<&Arguments>, stderr: bool, offset: usize) -> E {
    let (line, source) = if stderr {
        (
            2196,
            "print(f\"ERROR [loop-state] {exc}\", file=sys.stderr)",
        )
    } else {
        match args.map(|a| a.command.as_str()) {
            Some("state-digest") => (
                2170,
                "print(state_digest(args.state, args.max_items, args.max_bytes, args.max_file_bytes, args.max_seconds))",
            ),
            Some("state-digests") => (
                2188,
                "sys.stdout.write(\"\".join(line + \"\\n\" for line in lines))",
            ),
            Some("stop-control") => (
                2155,
                "sys.stdout.buffer.write(value.encode(\"utf-8\", \"surrogateescape\") + b\"\\0\")",
            ),
            Some("compare") => {
                let site = args.unwrap().print_sites.borrow();
                let (_,line,source)=site.iter().find(|(end,_,_)|*end>offset).copied().unwrap_or((0,2040,r#"for line in diagnostic_diffs(before, after, required): print("差分: " + line)"#));
                return error
                    .trace("compare", line, source)
                    .trace("main", 2194, "return compare(args)")
                    .trace("<module>", 2201, "raise SystemExit(main())");
            }
            _ => (2194, "return compare(args)"),
        }
    };
    error
        .at("main", line, source)
        .at("<module>", 2201, "raise SystemExit(main())")
}
pub fn emit(
    args: Option<&Arguments>,
    runtime: &LegacyRuntime,
    stdout: &[u8],
    stderr: &[u8],
    code: u8,
    active: Option<&E>,
) -> u8 {
    use dev_workflow_platform::loop_state::RawOutput;
    emit_to(
        args,
        runtime,
        stdout,
        stderr,
        code,
        &mut RawOutput::Stdout(std::io::stdout()),
        &mut RawOutput::Stderr(std::io::stderr()),
        active,
    )
}
// 出力・終了時flushの旧評価順を変えず、2つの書込み先と原因を明示する。
#[allow(clippy::too_many_arguments)]
fn emit_to(
    args: Option<&Arguments>,
    runtime: &LegacyRuntime,
    stdout: &[u8],
    stderr: &[u8],
    mut code: u8,
    out: &mut dyn Write,
    err: &mut dyn Write,
    active: Option<&E>,
) -> u8 {
    let mut buffer = StdoutBuffer {
        writer: out,
        pending: Vec::new(),
        text: Vec::new(),
        unbuffered: runtime.unbuffered,
        line_buffered: runtime.stdout_terminal,
    };
    let binary = args.is_some_and(|a| a.command == "stop-control");
    let mut write_error = None;
    let mut write_offset = 0;
    if binary {
        for part in stdout.split_inclusive(|c| *c == 0) {
            if let Err(e) = buffer.write(part, true) {
                write_error = Some(e);
                break;
            }
            write_offset += part.len();
        }
    } else if args.is_some_and(|a| a.command == "state-digests") || args.is_none() {
        if let Err(e) = buffer.write(stdout, false) {
            write_error = Some(e)
        }
    } else {
        for part in stdout.split_inclusive(|c| *c == 10) {
            let (text, newline) = if part.ends_with(b"\n") {
                (&part[..part.len() - 1], true)
            } else {
                (part, false)
            };
            if let Err(e) = buffer.write(text, false) {
                write_error = Some(e);
                break;
            }
            if newline && let Err(e) = buffer.write(b"\n", false) {
                write_error = Some(e);
                break;
            }
            write_offset += part.len();
        }
    }
    if let Some(e) = write_error {
        let exception = output_frame(E::os(e, "output"), args, false, write_offset);
        let exception = if let Some(cause) =
            active.filter(|_| !args.is_some_and(|a| a.command == "state-digests"))
        {
            super::error::render(cause)
                + "\nDuring handling of the above exception, another exception occurred:\n\n"
                + &error::render(&exception)
        } else {
            error::render(&exception)
        };
        let _ = err.write_all(exception.as_bytes());
        code = 1;
    } else if !stderr.is_empty()
        && let Err(e) = err.write_all(stderr)
    {
        let exception = output_frame(E::os(e, "output"), args, true, 0);
        let _ = err.write_all(error::render(&exception).as_bytes());
        code = if !runtime.unbuffered && stderr.len() <= BUFFER {
            120
        } else {
            1
        };
    }
    if let Err(e) = buffer.finish() {
        let diagnostic = error::render(&E::os(e, "finalize"));
        let text = format!(
            "Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>\n{diagnostic}"
        );
        let _ = err.write_all(text.as_bytes());
        code = 120;
    }
    if err.flush().is_err() {
        code = 120;
    }
    code
}
#[cfg(test)]
mod tests {
    use super::*;
    struct Full;
    impl Write for Full {
        fn write(&mut self, _: &[u8]) -> io::Result<usize> {
            Err(io::Error::from_raw_os_error(28))
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }
    #[test]
    fn write_and_finalization_faults_are_distinct() {
        let runtime = LegacyRuntime {
            int_limit: 4300,
            unbuffered: false,
            stdout_terminal: false,
        };
        let mut diagnostic = Vec::new();
        assert_eq!(
            emit_to(
                None,
                &runtime,
                b"abc\n",
                &[],
                0,
                &mut Full,
                &mut diagnostic,
                None
            ),
            120
        );
        assert!(diagnostic.starts_with(b"Exception ignored in:"));
        let runtime = LegacyRuntime {
            unbuffered: true,
            ..runtime
        };
        diagnostic.clear();
        assert_eq!(
            emit_to(
                None,
                &runtime,
                b"abc\n",
                &[],
                0,
                &mut Full,
                &mut diagnostic,
                None
            ),
            1
        );
        assert!(diagnostic.starts_with(b"Traceback"));
        assert!(!diagnostic.windows(17).any(|s| s == b"Exception ignored"));
    }
}
