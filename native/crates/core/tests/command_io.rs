#![forbid(unsafe_code)]

use dev_workflow_core::{CommandError, CommandOutput, ExitCode, execute_to};
use std::io::{self, BufWriter, Read, Write};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::mpsc::{self, Receiver, Sender};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const CHILD_ARGUMENT: &str = "--command-io-child";
const READY_OUT: &[u8] = b"ready\0\xff";
const READY_ERR: &[u8] = b"detail\xfe\0";
const INPUT: &[u8] = b"\xff\0\xfeA";
const FINAL_ERR: &[u8] = b"\0\xfeend";
const CHILD_CODE: u8 = 237;
const TIMEOUT: Duration = Duration::from_secs(20);

fn child() -> ExitCode {
    // 改行による自動 flush に頼らず、共通入口が途中で flush するかを調べる。
    let (stdin, stdout, stderr) = (io::stdin(), io::stdout(), io::stderr());
    let mut input = stdin.lock();
    let mut output = BufWriter::with_capacity(4096, stdout.lock());
    let mut errors = BufWriter::with_capacity(4096, stderr.lock());
    execute_to(&mut input, &mut output, &mut errors, |io| {
        io.write_stdout(READY_OUT)
            .map_err(|error| CommandError::runtime(error.to_string()))?;
        io.write_stderr(READY_ERR)
            .map_err(|error| CommandError::runtime(error.to_string()))?;
        let mut buffer = [0; INPUT.len()];
        let mut count = 0;
        while count < buffer.len() {
            let read = io
                .read_input(&mut buffer[count..])
                .map_err(|error| CommandError::runtime(error.to_string()))?;
            if read == 0 {
                return Err(CommandError::runtime("入力が足りません"));
            }
            count += read;
        }
        let mut stdout = buffer.to_vec();
        stdout.extend_from_slice(b"done");
        Ok(CommandOutput {
            stdout,
            stderr: FINAL_ERR.to_vec(),
            code: ExitCode::new(CHILD_CODE),
        })
    })
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Stream {
    Stdout,
    Stderr,
}

#[derive(Debug)]
enum Message {
    Ready(Stream, io::Result<Vec<u8>>),
    Finished(Stream, io::Result<Vec<u8>>),
}

// 途中の assert・時間切れ・読取り失敗でも直接の子を回収してからスレッドを待つ。
struct RunningChild {
    child: Child,
    readers: Vec<JoinHandle<()>>,
}

impl Drop for RunningChild {
    fn drop(&mut self) {
        let _ = self.child.kill();
        if let Err(error) = self.child.wait() {
            eprintln!("テストの子を回収できません: {error}");
        }
        for reader in self.readers.drain(..) {
            let _ = reader.join();
        }
    }
}

fn watch(
    mut pipe: impl Read + Send + 'static,
    stream: Stream,
    ready_len: usize,
    sender: Sender<Message>,
) -> JoinHandle<()> {
    thread::spawn(move || {
        let mut bytes = vec![0; ready_len];
        let result = pipe.read_exact(&mut bytes).map(|()| bytes);
        let ready = result.is_ok();
        if sender.send(Message::Ready(stream, result)).is_err() || !ready {
            return;
        }
        let mut tail = Vec::new();
        // 予想外の大量出力でも、テスト側に無制限に溜めない。
        let result = pipe.take(128).read_to_end(&mut tail).map(|_| tail);
        let _ = sender.send(Message::Finished(stream, result));
    })
}

fn receive(receiver: &Receiver<Message>, deadline: Instant) -> Message {
    receiver
        .recv_timeout(deadline.saturating_duration_since(Instant::now()))
        .expect("途中の入出力が時間内に届きません")
}

fn wait(child: &mut Child, deadline: Instant) -> ExitStatus {
    loop {
        if let Some(status) = child.try_wait().expect("子の終了状態を取得できません")
        {
            return status;
        }
        assert!(Instant::now() < deadline, "入力後に子が終了しません");
        thread::sleep(Duration::from_millis(10));
    }
}

fn intermediate_io_is_visible_while_child_waits() {
    let deadline = Instant::now() + TIMEOUT;
    let child = Command::new(std::env::current_exe().unwrap())
        .arg(CHILD_ARGUMENT)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("テストの子を起動できません");
    let mut running = RunningChild {
        child,
        readers: Vec::new(),
    };
    let (sender, receiver) = mpsc::channel();
    running.readers.push(watch(
        running.child.stdout.take().unwrap(),
        Stream::Stdout,
        READY_OUT.len(),
        sender.clone(),
    ));
    running.readers.push(watch(
        running.child.stderr.take().unwrap(),
        Stream::Stderr,
        READY_ERR.len(),
        sender,
    ));

    let (mut saw_stdout, mut saw_stderr) = (false, false);
    for _ in 0..2 {
        match receive(&receiver, deadline) {
            Message::Ready(Stream::Stdout, result) => {
                assert!(!saw_stdout);
                assert_eq!(result.unwrap(), READY_OUT);
                saw_stdout = true;
            }
            Message::Ready(Stream::Stderr, result) => {
                assert!(!saw_stderr);
                assert_eq!(result.unwrap(), READY_ERR);
                saw_stderr = true;
            }
            message => panic!("入力を送る前に出力が終了しました: {message:?}"),
        }
    }
    assert!(saw_stdout && saw_stderr);
    assert!(running.child.try_wait().unwrap().is_none());
    // 両方の途中出力を受け取るまで入力を送らない。
    let mut input = running.child.stdin.take().unwrap();
    input.write_all(INPUT).unwrap();
    input.flush().unwrap();
    drop(input);

    let (mut final_stdout, mut final_stderr) = (None, None);
    for _ in 0..2 {
        match receive(&receiver, deadline) {
            Message::Finished(Stream::Stdout, result) => {
                assert!(final_stdout.is_none());
                final_stdout = Some(result.unwrap());
            }
            Message::Finished(Stream::Stderr, result) => {
                assert!(final_stderr.is_none());
                final_stderr = Some(result.unwrap());
            }
            message => panic!("予期しない途中出力です: {message:?}"),
        }
    }
    assert_eq!(final_stdout.unwrap(), b"\xff\0\xfeAdone");
    assert_eq!(final_stderr.unwrap(), FINAL_ERR);
    // シグナル終了を専用の数値終了として扱わない。
    assert_eq!(
        wait(&mut running.child, deadline).code(),
        Some(i32::from(CHILD_CODE))
    );
    for reader in running.readers.drain(..) {
        reader.join().expect("出力読取りのスレッドが失敗しました");
    }
}

fn main() -> std::process::ExitCode {
    if std::env::args().nth(1).as_deref() == Some(CHILD_ARGUMENT) {
        return child().into();
    }
    intermediate_io_is_visible_while_child_waits();
    println!("command_io: 途中の flush・生バイトの入出力・専用終了値の検査に合格");
    std::process::ExitCode::SUCCESS
}
