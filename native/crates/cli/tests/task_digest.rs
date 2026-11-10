#![forbid(unsafe_code)]
//! 通常起動で専用比較を必須実行する。製品への登録は行わない。
#[path = "../src/commands/task_digest/mod.rs"]
mod task_digest;

use dev_workflow_core::{CommandOutput, ExitCode};
use dev_workflow_platform::task_digest::{ReadError, RegularPath};
use serde_json::json;
use std::ffi::OsStr;
use std::fs;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::time::{Duration, Instant};

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn descriptors() -> std::collections::BTreeSet<u32> {
    fs::read_dir("/dev/fd")
        .unwrap()
        .map(|e| e.unwrap().file_name().to_str().unwrap().parse().unwrap())
        .collect()
}

fn replace(path: &Path) {
    fs::rename(path, path.with_file_name("opened-old")).unwrap();
    fs::rename(path.with_file_name("replacement"), path).unwrap();
    // 外側がrename直後の属性を取得するまで、製品の読取りへ戻らない。
    {
        let mut out = io::stdout().lock();
        out.write_all(b"{\"checkpoint\":\"renamed\"}\n").unwrap();
        out.flush().unwrap();
    }
    let mut ack = [0; 9];
    io::stdin().lock().read_exact(&mut ack).unwrap();
    assert_eq!(&ack, b"continue\n", "差し替え境界の確認応答がない");
}

fn race(path: &Path, boundary: &str) -> CommandOutput {
    let before = descriptors();
    let mut changed = 0;
    let result = (|| {
        let checked = RegularPath::inspect(path)?;
        if boundary == "stat" {
            replace(path);
            changed += 1;
        }
        let mut file = checked.open()?;
        if boundary == "open" {
            replace(path);
            changed += 1;
        }
        task_digest::read_all(|buffer| file.read_chunk(buffer)).map_err(|error| ReadError::Io {
            error,
            filename: false,
        })
    })();
    assert_eq!(changed, 1, "差し替えの境界を通っていない");
    assert_eq!(descriptors(), before, "成功または拒否の後に fd が残った");
    let output = match result {
        Ok(data) => task_digest::from_bytes(&data),
        Err(error) => task_digest::file_error(error, path),
    };
    CommandOutput::stdout(format!("{}\n", json!({"changed":changed,"fd_closed":true,"code":output.code.as_u8(),"stdout_hex":hex(&output.stdout),"stderr_hex":hex(&output.stderr)})).into_bytes())
}

struct Fails {
    flush: bool,
    partial: bool,
    errno: i32,
    written: Vec<u8>,
}
impl Write for Fails {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if self.partial && self.written.is_empty() {
            self.written.push(bytes[0]);
            Ok(1)
        } else if self.flush {
            self.written.extend_from_slice(bytes);
            Ok(bytes.len())
        } else {
            Err(io::Error::from_raw_os_error(self.errno))
        }
    }
    fn flush(&mut self) -> io::Result<()> {
        Err(io::Error::from_raw_os_error(self.errno))
    }
}

fn fixture(args: &[std::ffi::OsString]) -> ExitCode {
    let mode = args.first().and_then(|v| v.to_str()).expect("fixture mode");
    if mode == "writer-full-fd" {
        let mut sink = Fails {
            flush: false,
            partial: false,
            errno: 28,
            written: vec![],
        };
        let mut err = Vec::new();
        let code =
            task_digest::output::emit_to(CommandOutput::stdout(b"ok\n"), &mut sink, &mut err);
        let record = json!({"code":code.as_u8(),"written_hex":hex(&sink.written),"stdout_hex":"","stderr_hex":hex(&err)});
        io::stderr()
            .lock()
            .write_all(format!("{record}\n").as_bytes())
            .unwrap();
        return ExitCode::SUCCESS;
    }
    if mode == "cli" {
        return task_digest::execute_to(
            &args[1..],
            &mut io::stdin().lock(),
            &mut io::stdout().lock(),
            &mut io::stderr().lock(),
        );
    }
    let output = match mode {
        "unbuffered" => CommandOutput::stdout(format!("{}\n", json!({"enabled": task_digest::output::unbuffered_from(std::env::var_os("PYTHONUNBUFFERED").as_deref())})).into_bytes()),
        "normalize" => {
            assert_eq!(args.len(), 1);
            let mut data = Vec::new();
            io::stdin().lock().read_to_end(&mut data).unwrap();
            let result = std::str::from_utf8(&data)
                .map_err(|e| {
                    format!(
                        "UTF-8 として読めない: {}",
                        task_digest::errors::utf8(&data, e)
                    )
                })
                .and_then(task_digest::normalize::body);
            let record = match result {
                Ok(body) => {
                    json!({"normalized_hex":hex(body.as_bytes()),"digest":task_digest::normalize::digest(&body),"error":null})
                }
                Err(error) => json!({"normalized_hex":null,"digest":null,"error":error}),
            };
            CommandOutput::stdout(format!("{record}\n").into_bytes())
        }
        "race" => {
            assert_eq!(args.len(), 3);
            race(Path::new(&args[1]), args[2].to_str().unwrap())
        }
        "writer" => {
            assert!((3..=4).contains(&args.len()));
            let mut sink = Fails {
                flush: args[1] == "flush",
                partial: args[1] == "partial",
                errno: if args.get(3).is_some_and(|arg| arg == "io-error") {
                    5
                } else if args.get(3).is_some_and(|arg| arg == "no-space") {
                    28
                } else {
                    32
                },
                written: vec![],
            };
            let mut out = Vec::new();
            let mut err = Vec::new();
            let code = if args[2] == "stdout" {
                task_digest::output::emit_to(CommandOutput::stdout(b"ok\n"), &mut sink, &mut err)
            } else {
                task_digest::output::emit_to(task_digest::failure(b"bad"), &mut out, &mut sink)
            };
            CommandOutput::stdout(format!("{}\n", json!({"code":code.as_u8(),"written_hex":hex(&sink.written),"stdout_hex":hex(&out),"stderr_hex":hex(&err)})).into_bytes())
        }
        "reader" => {
            assert_eq!(args.len(), 2);
            let mut data = Vec::new();
            io::stdin().lock().read_to_end(&mut data).unwrap();
            let mut offset = 0;
            let result = task_digest::read_all(|buffer| {
                if args[1] == "would-block" {
                    // 故障注入だけ。製品のファイル入力へ特別なEAGAIN規則を足さない。
                    let errno = if cfg!(target_os = "macos") { 35 } else { 11 };
                    return Err(io::Error::from_raw_os_error(errno));
                }
                if args[1] == "fail" && offset > 0 {
                    return Err(io::Error::from_raw_os_error(5));
                }
                let count = (data.len() - offset).min(1).min(buffer.len());
                buffer[..count].copy_from_slice(&data[offset..offset + count]);
                offset += count;
                Ok(count)
            });
            match result {
                Ok(data) => task_digest::from_bytes(&data),
                Err(error) => task_digest::file_error(
                    ReadError::Io {
                        error,
                        filename: false,
                    },
                    Path::new("-"),
                ),
            }
        }
        _ => panic!("unknown fixture mode: {mode}"),
    };
    task_digest::output::emit_to(output, &mut io::stdout().lock(), &mut io::stderr().lock())
}

struct Checker(Child);
impl Drop for Checker {
    fn drop(&mut self) {
        let _ = self.0.kill();
        self.0.wait().expect("専用比較器を回収できない");
    }
}

fn main() -> std::process::ExitCode {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.first().map(|v| v.as_os_str()) == Some(OsStr::new("--task-digest-fixture")) {
        return fixture(&args[1..]).into();
    }
    assert!(args.is_empty(), "専用比較を省く引数は受け付けない");
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .unwrap();
    let checker = root.join("native/ci/check_task_digest.py");
    assert!(checker.is_file(), "task-digest の比較器がない");
    let mut child = Checker(
        Command::new("python3")
            .arg("-B")
            .arg(checker)
            .arg("--fixture")
            .arg(std::env::current_exe().unwrap())
            .current_dir(root)
            .spawn()
            .expect("専用比較器を起動できない"),
    );
    let deadline = Instant::now() + Duration::from_secs(300);
    loop {
        if let Some(status) = child.0.try_wait().expect("専用比較器の待機に失敗") {
            assert!(status.success(), "task-digest の比較が不合格: {status}");
            return std::process::ExitCode::SUCCESS;
        }
        if Instant::now() >= deadline {
            panic!("task-digest の比較が時間切れ");
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}
