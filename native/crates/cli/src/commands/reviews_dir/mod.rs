//! `reviews-dir.sh`(旧版)のRust版。`.claude/reviews/`を用意し、基準の時点の未追跡の一覧を原子的に公開する。
//!
//! 正本は旧版のスクリプト。検査の順・診断の文・終了値・外部コマンドの引数と環境と入出力の向き・
//! 信号の扱いを、旧bash(5.2.21)で動かした旧版と同じにする。R4Gは、旧版が外部コマンドに任せる処理を
//! 同じ外部コマンドで行う(2026-11-11 22:35の決定。23:19の決定のR4Gの例外)。
//!
//! 製品の登録表にはまだ載せない。検査専用の入口(`examples/r4g_contract.rs`)だけが使い、旧スクリプトの
//! 呼出名(`$0`)を受け取る。

pub mod lines;
pub mod locale;
pub mod shell;

use shell::{Cmd, Exit, In, Out, Shell};

/// `git`の前置き(旧版の`GIT_PRE`)。
const GIT_PRE: [&[u8]; 10] = [
    b"--no-pager",
    b"--no-replace-objects",
    b"-c",
    b"core.quotePath=false",
    b"-c",
    b"core.fsmonitor=",
    b"-c",
    b"core.hooksPath=/dev/null",
    b"-c",
    b"core.ignoreCase=false",
];

fn cat(parts: &[&[u8]]) -> Vec<u8> {
    parts.concat()
}

/// `usage`: `sed -n '2,/^# --- end usage ---$/p' "$0" | sed '$d' >&2`。
fn usage(sh: &mut Shell) -> Result<(), Exit> {
    let line = lines::line("usage");
    let script = sh.script.clone();
    let mut second = Cmd::new(&[b"sed", b"$d"], line, lines::text::USAGE_SECOND);
    second.stdout = Out::ToStderr;
    let status = sh.run_pipeline(
        vec![
            Cmd::new(
                &[b"sed", b"-n", b"2,/^# --- end usage ---$/p", &script],
                line,
                lines::text::USAGE_FIRST,
            ),
            second,
        ],
        line,
        true,
    )?;
    sh.errexit(status, line)
}

/// `fail_usage`: 理由の行・使い方・終了値2。
fn fail_usage(sh: &mut Shell, message: &[u8]) -> Exit {
    let outcome = (|| {
        sh.echo_err(
            lines::line("fail_usage"),
            &cat(&[b"ERROR [usage] ", message]),
        )?;
        usage(sh)?;
        sh.clear_err_trap();
        Err(Exit::Code(2))
    })();
    match outcome {
        Err(exit) => exit,
        Ok(()) => Exit::Code(2),
    }
}

/// `die`: 理由の行と終了値。
fn die(sh: &mut Shell, code: u8, reason: &str, message: &[u8]) -> Exit {
    // `local code="$1" reason="$2"`。`exit`で関数の中から終えるので、EXITの罠の子もこの値を受け取る。
    sh.assign(b"code", code.to_string().as_bytes());
    sh.assign(b"reason", reason.as_bytes());
    sh.current = lines::text::EXIT_DIE;
    if let Err(exit) = sh.echo_err(
        lines::line("die"),
        &cat(&[b"ERROR [", reason.as_bytes(), b"] ", message]),
    ) {
        return exit;
    }
    sh.clear_err_trap();
    Exit::Code(code)
}

/// `ensure_dirs`: 階層ごとに「検査 → 無ければ作成」。
fn ensure_dirs(sh: &mut Shell, root: &[u8]) -> Result<(), Exit> {
    let claude = cat(&[root, b"/.claude"]);
    let reviews = cat(&[root, b"/.claude/reviews"]);
    // `local d`(値を持たない間は、呼出元が export した値が子へ渡る)と`for d in …`。
    let saved = sh.saved(b"d");
    for directory in [claude, reviews] {
        sh.assign(b"d", &directory);
        if sh.test_symlink(&directory) {
            return Err(die(
                sh,
                3,
                "symlink",
                &cat(&[&directory, " が symlink(辿らずに止まる)".as_bytes()]),
            ));
        }
        if !sh.test_exists(&directory) {
            let cmd = Cmd::new(
                &[b"mkdir", b"--", &directory],
                lines::line("mkdir"),
                lines::text::MKDIR,
            );
            sh.current = lines::text::MKDIR;
            let status = sh.run(cmd, lines::line("ensure_for"))?;
            if status != 0 {
                return Err(die(
                    sh,
                    20,
                    "internal",
                    &cat(&[&directory, " を作れない".as_bytes()]),
                ));
            }
        } else if !sh.test_directory(&directory) {
            return Err(die(
                sh,
                3,
                "not-directory",
                &cat(&[&directory, " が通常のディレクトリでない".as_bytes()]),
            ));
        }
    }
    sh.restore(b"d", saved);
    Ok(())
}

/// 引数の検査から公開まで(旧版の本体)。`Ok`はスクリプトの終わりまで進んだこと。
fn body(sh: &mut Shell, args: &[Vec<u8>]) -> Result<(), Exit> {
    // 旧版の32・34行目: `GIT_PRE=(…)`(配列は書き出されない)と`TMP=""`。
    sh.assign_array(b"GIT_PRE");
    sh.assign(b"TMP", b"");
    let Some(sub) = args.first() else {
        return Err(fail_usage(sh, "サブコマンドが必要です".as_bytes()));
    };
    sh.assign(b"SUB", sub);
    for name in [b"ROOT".as_slice(), b"TOP", b"LIST"] {
        sh.assign(name, b"");
    }
    match sub.as_slice() {
        b"-h" | b"--help" => {
            usage(sh)?;
            return Err(Exit::Code(0));
        }
        b"ensure" | b"save-untracked" => {}
        _ => {
            return Err(fail_usage(
                sh,
                &cat(&["不明なサブコマンド: ".as_bytes(), sub]),
            ));
        }
    }
    let (mut root, mut top, mut list) = (Vec::new(), Vec::new(), Vec::new());
    let mut rest = &args[1..];
    while let Some(arg) = rest.first() {
        let (slot, name): (_, &[u8]) = match arg.as_slice() {
            b"--root" => (&mut root, b"ROOT"),
            b"--top" => (&mut top, b"TOP"),
            b"--list" => (&mut list, b"LIST"),
            _ => return Err(fail_usage(sh, &cat(&["不明な引数: ".as_bytes(), arg]))),
        };
        if rest.len() < 2 {
            return Err(fail_usage(sh, &cat(&[arg, " に値が必要です".as_bytes()])));
        }
        *slot = rest[1].clone();
        sh.assign(name, &rest[1]);
        rest = &rest[2..];
    }
    if root.is_empty() {
        return Err(fail_usage(sh, "--root が必要です".as_bytes()));
    }
    if !sh.test_directory(&root) {
        return Err(fail_usage(
            sh,
            &cat(&["--root がディレクトリでない: ".as_bytes(), &root]),
        ));
    }
    if sub.as_slice() == b"ensure" {
        if !(top.is_empty() && list.is_empty()) {
            return Err(fail_usage(
                sh,
                "ensure は --root だけを受け付ける".as_bytes(),
            ));
        }
        ensure_dirs(sh, &root)?;
        return Err(Exit::Code(0));
    }
    if top.is_empty() {
        return Err(fail_usage(sh, "--top が必要です".as_bytes()));
    }
    if list.is_empty() {
        return Err(fail_usage(sh, "--list が必要です".as_bytes()));
    }
    if !sh.test_directory(&top) {
        return Err(fail_usage(
            sh,
            &cat(&["--top がディレクトリでない: ".as_bytes(), &top]),
        ));
    }
    let reviews = cat(&[&root, b"/.claude/reviews"]);
    sh.assign(b"REVIEWS", &reviews);
    let not_direct = || {
        cat(&[
            "--list は ".as_bytes(),
            &reviews,
            "/ の直下のファイル: ".as_bytes(),
            &list,
        ])
    };
    let prefix = cat(&[&reviews, b"/"]);
    if !list.starts_with(&prefix) {
        let message = not_direct();
        return Err(fail_usage(sh, &message));
    }
    let name = &list[prefix.len()..];
    sh.assign(b"NAME", name);
    if name.is_empty() || name.contains(&b'/') || name == b"." || name == b".." {
        let message = not_direct();
        return Err(fail_usage(sh, &message));
    }
    ensure_dirs(sh, &root)?;
    if (sh.test_exists(&list) || sh.test_symlink(&list))
        && (sh.test_symlink(&list) || !sh.test_regular(&list))
    {
        return Err(die(
            sh,
            3,
            "list-not-regular",
            &cat(&[
                &list,
                " が symlink か通常ファイルでない(何も書かずに止まる)".as_bytes(),
            ]),
        ));
    }
    // 公開の手段と sha256 の道具を、一時ファイルの前に決める。
    let mut check = Cmd::new(
        &[b"python3", b"-I", b"-c", b"import os"],
        lines::line("python_check"),
        lines::text::PYTHON_CHECK,
    );
    check.stdout = Out::Null;
    check.stderr = Out::ToStdout;
    sh.current = lines::text::PYTHON_CHECK;
    sh.assign(b"PUBLISH", b"mv");
    let use_python = sh.run(check, lines::line("python_check"))? == 0;
    if use_python {
        sh.assign(b"PUBLISH", b"os.replace");
    }
    let sha_cmd: &[u8] = if sh.command_v(b"sha256sum") {
        b"sha256sum"
    } else if sh.command_v(b"shasum") {
        b"shasum"
    } else if sh.command_v(b"openssl") {
        b"openssl"
    } else {
        sh.assign(b"SHA_CMD", b"");
        return Err(die(
            sh,
            20,
            "tool-missing",
            "sha256 を計算する道具が無い(sha256sum・shasum・openssl のどれか)".as_bytes(),
        ));
    };
    sh.assign(b"SHA_CMD", sha_cmd);
    // 一時ファイル。
    let template = cat(&[&reviews, b"/.base-untracked.XXXXXX"]);
    sh.current = lines::text::MKTEMP;
    let (tmp, status) = sh.command_substitution(
        vec![Cmd::new(
            &[b"mktemp", &template],
            lines::line("mktemp"),
            lines::text::MKTEMP,
        )],
        lines::line("mktemp"),
        false,
        None,
    )?;
    sh.tmp = tmp.clone();
    sh.assign(b"TMP", &tmp);
    if status != 0 {
        return Err(die(
            sh,
            20,
            "internal",
            &cat(&["一時ファイルを作れない: ".as_bytes(), &reviews]),
        ));
    }
    sh.checkpoint()?;
    // git ls-files を一時ファイルへ。
    let mut git_argv: Vec<&[u8]> = vec![b"git", b"-C", &top];
    git_argv.extend(GIT_PRE);
    git_argv.extend([b"ls-files".as_slice(), b"-o", b"--exclude-standard", b"-z"]);
    let mut git = Cmd::new(&git_argv, lines::line("git"), lines::text::GIT);
    git.stdout = Out::File(tmp.clone());
    sh.current = lines::text::GIT;
    sh.assign(b"rc", b"0");
    let rc = sh.run(git, lines::line("git"))?;
    if rc != 0 {
        sh.assign(b"rc", rc.to_string().as_bytes());
        return Err(die(
            sh,
            4,
            "git",
            format!("git ls-files が失敗した(終了コード {rc}。何も公開しない)").as_bytes(),
        ));
    }
    // sha256 を一時ファイルから(サブシェルの中のパイプライン。失敗はサブシェルのERRの罠と、親のERRの罠)。
    let (tool_line, first, second): (u32, Vec<&[u8]>, Vec<&[u8]>) = match sha_cmd {
        b"sha256sum" => (
            lines::line("sha256sum"),
            vec![b"sha256sum"],
            vec![b"cut", b"-d ", b"-f1"],
        ),
        b"shasum" => (
            lines::line("shasum"),
            vec![b"shasum", b"-a", b"256"],
            vec![b"cut", b"-d ", b"-f1"],
        ),
        _ => (
            lines::line("openssl"),
            vec![b"openssl", b"dgst", b"-sha256"],
            vec![b"sed", b"s/.*[= ]//"],
        ),
    };
    let mut producer = Cmd::new(&first, tool_line, "");
    producer.stdin = In::File(tmp.clone());
    let consumer = Cmd::new(&second, tool_line, "");
    sh.current = lines::text::SHA;
    // サブシェルの中のパイプライン。失敗はサブシェルのERRの罠(set -E で受け継ぐ)と、親のERRの罠。
    let (sha, subshell_status) = sh.command_substitution(
        vec![producer, consumer],
        lines::line("sha"),
        false,
        Some(tool_line),
    )?;
    sh.assign(b"SHA", &sha);
    sh.errexit(subshell_status, lines::line("sha"))?;
    sh.assign(b"NL", b"\n");
    // 公開。
    let (reason, status, python) = if use_python {
        sh.current = lines::text::PUBLISH_PYTHON;
        let (reason, status) = sh.command_substitution(
            vec![Cmd::new(
                &[
                    b"python3",
                    b"-I",
                    b"-c",
                    lines::PUBLISH_SOURCE.as_bytes(),
                    &tmp,
                    &list,
                ],
                lines::line("publish_python"),
                lines::text::PUBLISH_PYTHON,
            )],
            lines::line("publish_python"),
            true,
            None,
        )?;
        (reason, status, true)
    } else {
        sh.current = lines::text::PUBLISH_MV;
        let (reason, status) = sh.command_substitution(
            vec![Cmd::new(
                &[b"mv", b"-f", b"-T", b"--", &tmp, &list],
                lines::line("publish_mv"),
                lines::text::PUBLISH_MV,
            )],
            lines::line("publish_mv"),
            true,
            None,
        )?;
        (reason, status, false)
    };
    sh.assign(b"reason", &reason);
    if status != 0 {
        let first_line = reason
            .split(|byte| *byte == b'\n')
            .next()
            .unwrap_or_default();
        let message = if python {
            cat(&[
                "一覧を公開できない: ".as_bytes(),
                &list,
                "(".as_bytes(),
                first_line,
                ")".as_bytes(),
            ])
        } else {
            cat(&[
                "一覧を公開できない: ".as_bytes(),
                &list,
                "(".as_bytes(),
                first_line,
                "。python3 が無いので mv -f -T で公開した。-T を持たない mv では python3 が要る)"
                    .as_bytes(),
            ])
        };
        return Err(die(sh, 20, "internal", &message));
    }
    sh.checkpoint()?;
    sh.tmp.clear();
    sh.assign(b"TMP", b"");
    let mut out = sha;
    out.push(b'\n');
    let status = sh.builtin_write(1, &out, lines::line("printf"), "printf")?;
    sh.errexit(status, lines::line("printf"))
}

/// 旧版の起動(`bash reviews-dir.sh <引数>`)。`script`は旧版の`$0`。戻らない。
pub fn main(script: Vec<u8>, args: Vec<Vec<u8>>) -> ! {
    let mut sh = Shell::start(script);
    let outcome = body(&mut sh, &args);
    sh.finish(outcome)
}
