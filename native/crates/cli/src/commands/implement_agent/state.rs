//! `--log-file` の語彙の正規化(372〜398 行)・実装用既定表(404〜455 行)・保護領域の候補の解決の始め
//! (693〜714 行と、候補が 1 つも残らないときの 725 行)。
use super::launch;
use super::locale::ToolLocale;
use super::paths;
use super::shell::{Flow, Point, Shell, ToolWrite};
use super::sites::{self, Site};
use super::tools::{self, Tool};
use super::{Context, childenv};
use dev_workflow_platform::implement_agent as os;

/// 実装用既定表の 1 行(external-runners.md §12-6 と二重管理)。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Table {
    pub name: &'static [u8],
    /// 「起動コマンド」列(`{model}` は `--model` の値に置換)。
    pub command: &'static [u8],
    /// 「書き込みフラグ」列。
    pub write_flag: &'static [u8],
    /// 「プローブ用の読み取り専用フラグ」列。
    pub probe_flag: &'static [u8],
    /// 「プローブの出力から読む形」(モデル名と CLI の版。拡張正規表現)。
    pub model_re: &'static [u8],
    pub version_re: &'static [u8],
}

/// 旧版の 404〜439 行の表(codex だけ)。
pub const TABLE: &[Table] = &[Table {
    name: b"codex",
    command: b"codex exec --sandbox workspace-write -m {model}",
    write_flag: b"--sandbox workspace-write",
    probe_flag: b"--sandbox read-only",
    model_re: b"^model: ([^[:space:]]+)",
    version_re: b"^OpenAI Codex v([0-9][0-9A-Za-z.+-]*)",
}];

/// `default_command "$RUNNER"`。
pub fn lookup(runner: &[u8]) -> Option<&'static Table> {
    TABLE.iter().find(|row| row.name == runner)
}

/// `read -r -a <名> <<<"$値"`(既定の IFS で分ける)。
pub fn read_tokens(value: &[u8]) -> Vec<Vec<u8>> {
    value
        .split(|b| matches!(b, b' ' | b'\t' | b'\n'))
        .filter(|w| !w.is_empty())
        .map(<[u8]>::to_vec)
        .collect()
}

/// 既定表の形の検査(452・453 行)が通った後の値。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Shape {
    pub write_tokens: Vec<Vec<u8>>,
    pub probe_tokens: Vec<Vec<u8>>,
    pub write_name: Vec<u8>,
    pub write_value: Vec<u8>,
}

/// 452・453 行: 「フラグ 値」の形でなければ `die 20 internal`。
pub fn check_shape(shell: &mut Shell, table: &Table) -> Flow<Shape> {
    let write_tokens = read_tokens(table.write_flag);
    let probe_tokens = read_tokens(table.probe_flag);
    for (tokens, site, label, flag) in [
        (
            &write_tokens,
            sites::WRITE_FLAG_SHAPE,
            "書き込みフラグ",
            table.write_flag,
        ),
        (
            &probe_tokens,
            sites::PROBE_FLAG_SHAPE,
            "プローブ用フラグ",
            table.probe_flag,
        ),
    ] {
        shell.checkpoint()?;
        if tokens.len() < 2 {
            let _: Site = site;
            let mut text =
                format!("実装用既定表の{label}が「フラグ 値」の形になっていない: '").into_bytes();
            text.extend_from_slice(flag);
            text.push(b'\'');
            return Err(shell.die(20, "internal", &text));
        }
    }
    Ok(Shape {
        write_name: write_tokens.first().cloned().unwrap_or_default(),
        write_value: write_tokens.last().cloned().unwrap_or_default(),
        write_tokens,
        probe_tokens,
    })
}

/// `build_cmd`(569〜603 行。R4X-3 で使う。G1 の単体テストのためにここに置く)。
#[allow(dead_code)]
pub fn build_cmd(template: &[u8], model: &[u8], prompt: &[u8]) -> Vec<Vec<u8>> {
    let mut cmd: Vec<Vec<u8>> = Vec::new();
    let mut placed = false;
    for word in read_tokens(template) {
        match word.as_slice() {
            b"{model}" => {
                if !model.is_empty() {
                    cmd.push(model.to_vec());
                } else if cmd.last().is_some_and(|last| last.first() == Some(&b'-')) {
                    cmd.pop();
                }
            }
            b"{prompt}" => {
                cmd.push(prompt.to_vec());
                placed = true;
            }
            _ => cmd.push(word),
        }
    }
    if !placed {
        if prompt.first() == Some(&b'-') {
            cmd.push(b"--".to_vec());
        }
        cmd.push(prompt.to_vec());
    }
    cmd
}

/// `tokens_in_argv`(650〜662 行。R4X-3 の判定 3′-argv)。
#[allow(dead_code)]
pub fn tokens_in_argv(cmd: &[Vec<u8>], name: &[u8], value: &[u8]) -> bool {
    if cmd.windows(2).any(|pair| {
        pair.first().is_some_and(|a| a == name) && pair.get(1).is_some_and(|b| b == value)
    }) {
        return true;
    }
    let mut joined = name.to_vec();
    joined.push(b'=');
    joined.extend_from_slice(value);
    cmd.contains(&joined)
}

/// `lex_norm`(372〜386 行): 字面で `.` と空の階層を捨て、`..` は 1 つ前と相殺する(相手が無ければ捨てる)。
pub fn lex_norm(value: &[u8]) -> Vec<u8> {
    let mut rest = value.strip_prefix(b"/").unwrap_or(value).to_vec();
    let mut out: Vec<Vec<u8>> = Vec::new();
    while !rest.is_empty() {
        let (segment, tail) = match rest.iter().position(|b| *b == b'/') {
            Some(slash) => (
                rest.get(..slash).unwrap_or_default().to_vec(),
                rest.get(slash + 1..).unwrap_or_default().to_vec(),
            ),
            None => (rest.clone(), Vec::new()),
        };
        rest = tail;
        match segment.as_slice() {
            b"" | b"." => {}
            b".." => {
                out.pop();
            }
            _ => out.push(segment),
        }
    }
    let mut lex = Vec::new();
    for segment in out {
        lex.push(b'/');
        lex.extend_from_slice(&segment);
    }
    if lex.is_empty() {
        lex = b"/".to_vec();
    }
    lex
}

/// コマンド置換の値(NUL を落とし、末尾の改行を消す)。NUL があれば bash の警告を出す。
fn comsub_value(shell: &mut Shell, line: u32, mut output: Vec<u8>) -> Vec<u8> {
    if output.contains(&0) {
        output.retain(|b| *b != 0);
        shell.internal_warning(line, b"command substitution: ignored null byte in input");
    }
    paths::strip_newlines(output)
}

/// 390・391 行の道具の 1 回分(Rust の中で作るか、決定の表の (2) なら起動する)。
fn run_tool(shell: &mut Shell, context: &mut Context, tool: Tool, site: Site) -> Flow<Vec<u8>> {
    let log_file = context.args.log_file.clone();
    let script = context.script_values();
    let env = childenv::child_environment(
        &context.startup,
        &script,
        site.line,
        tool.name(),
        &[],
        (0, 0),
    );
    let first = |name: &[u8]| childenv::first_value(&env, name);
    let mut locale = ToolLocale::from_env(&first);
    // 文字コードが UTF-8 でも ASCII でもなければ、Rust の中で作らずに起動する(決定の表の (2))。
    let emulated = if matches!(locale.codeset, super::locale::Codeset::Other(_)) {
        Err(super::locale::NeedTool)
    } else {
        tools::run(tool, &mut locale, tool.name(), &[&log_file])
    };
    let point = match tool {
        Tool::Dirname => Point::Dirname,
        Tool::Basename => Point::Basename,
    };
    let (status, stdout, launched) = match emulated {
        Ok(run) => {
            let status = match shell.tool_write(2, &run.stderr) {
                ToolWrite::Signal(signal) => 128 + signal,
                ToolWrite::Done | ToolWrite::Failed(_) => run.status,
            };
            (status, run.stdout, false)
        }
        Err(_) => {
            let argv = vec![tool.name().to_vec(), log_file.clone()];
            let started = launch::start(
                shell,
                context,
                &launch::Command {
                    name: tool.name(),
                    argv: &argv,
                    line: site.line,
                    temporary: &[],
                    stdout: os::ChildOut::Pipe,
                    stderr: os::ChildErr::Inherit,
                },
            );
            let done = launch::wait(shell, started, launch::Kind::Comsub)?;
            (done.code().unwrap_or(1), done.stdout, true)
        }
    };
    shell.stop_point(point, launched)?;
    let value = comsub_value(shell, site.line, stdout);
    if status != 0 {
        return Err(shell.fail(status, site.line));
    }
    Ok(value)
}

/// 387〜398 行。
pub fn log_file(shell: &mut Shell, context: &mut Context) -> Flow<()> {
    // 387 行: PHYS_PWD="$(pwd -P)"。
    let phys = match paths::pwd_physical(&context.logical) {
        Ok(path) => {
            let mut out = path;
            out.push(b'\n');
            paths::strip_newlines(out)
        }
        Err(errno) => {
            let mut text = b"pwd: error retrieving current directory: getcwd: cannot access parent directories: ".to_vec();
            text.extend_from_slice(super::strerror(errno).as_bytes());
            shell.raw_stderr_line(&text);
            shell.checkpoint()?;
            return Err(shell.fail(1, sites::PWD_P.line));
        }
    };
    shell.checkpoint()?;
    context.extra.set("PHYS_PWD", &phys);
    context.extra.set("LOG_DIR", b"");
    context.extra.set("LOG_NAME", b"");
    context.extra.set("LOG_NORM", b"");
    if context.args.log_file.is_empty() {
        return Ok(());
    }
    let dir = run_tool(shell, context, Tool::Dirname, sites::DIRNAME)?;
    context.extra.set("LOG_DIR", &dir);
    let name = run_tool(shell, context, Tool::Basename, sites::BASENAME)?;
    context.extra.set("LOG_NAME", &name);
    let absolute = if dir.first() == Some(&b'/') {
        dir
    } else {
        let mut joined = phys.clone();
        joined.push(b'/');
        joined.extend_from_slice(&dir);
        joined
    };
    let log_dir = lex_norm(&absolute);
    let mut norm = log_dir.strip_suffix(b"/").unwrap_or(&log_dir).to_vec();
    norm.push(b'/');
    norm.extend_from_slice(&name);
    context.extra.set("LOG_DIR", &log_dir);
    context.extra.set("LOG_NORM", &norm);
    context.log_dir = log_dir.clone();
    context.log_name = name;
    context.log_norm = norm.clone();
    shell.checkpoint()?;
    if paths::exists(&norm) || paths::is_link(&norm) {
        let mut text = "--log-file のパスに既にエントリが在る: "
            .as_bytes()
            .to_vec();
        text.extend_from_slice(&norm);
        text.extend_from_slice("(--log-file の置き場 '".as_bytes());
        text.extend_from_slice(&log_dir);
        text.extend_from_slice("' に同じ名前が先に置かれている。上書きしない。symlink やハードリンクを経由して外のファイルへ書かないため。別の名前を渡す)".as_bytes());
        return Err(shell.fail_usage(&text));
    }
    Ok(())
}

/// 442〜455 行。
pub fn default_table(shell: &mut Shell, context: &mut Context) -> Flow<()> {
    let Some(table) = lookup(&context.args.runner) else {
        let mut text = "ランナー '".as_bytes().to_vec();
        text.extend_from_slice(&context.args.runner);
        text.extend_from_slice("' は実装用既定表に無い(実装経路には既定表外を受け付ける経路が無い。external-runners.md §12-6 を参照)".as_bytes());
        return Err(shell.fail_usage(&text));
    };
    let shape = check_shape(shell, table)?;
    context.shape = Some(shape);
    Ok(())
}

/// 699 行 `real_dir` を使う 705〜708 行と、候補(711〜714 行)。最初の候補まで進んだら、その `<cand>` を返す。
pub fn candidates(shell: &mut Shell, context: &mut Context) -> Flow<Vec<u8>> {
    let state = context.cd_state();
    context.excl_tmp = paths::real_dir(&state, b"/tmp").unwrap_or_default();
    shell.checkpoint()?;
    context.excl_tmpdir = Vec::new();
    if let Some(tmpdir) = context.places.tmpdir.clone().filter(|v| !v.is_empty()) {
        context.excl_tmpdir = paths::real_dir(&state, &tmpdir).unwrap_or_default();
        shell.checkpoint()?;
    }
    let cwd = context.args.cwd.clone();
    match paths::real_dir(&state, &cwd) {
        Some(value) => context.excl_cwd = value,
        None => {
            shell.checkpoint()?;
            let mut text = "--cwd の実体パスを解決できない: ".as_bytes().to_vec();
            text.extend_from_slice(&cwd);
            return Err(shell.die(20, "internal", &text));
        }
    }
    shell.checkpoint()?;
    let home = context.places.home.clone().unwrap_or_default();
    let list = [
        context.places.xdg_state_home.clone().unwrap_or_default(),
        [home.as_slice(), b"/.local/state"].concat(),
        [home.as_slice(), b"/.cache"].concat(),
    ];
    for cand in list {
        if cand.is_empty() || cand == b"/.local/state" || cand == b"/.cache" {
            continue;
        }
        let mut cand = cand;
        cand.extend_from_slice(b"/dev-workflow");
        context.state_tried.push(b' ');
        context.state_tried.extend_from_slice(&cand);
        return Ok(cand);
    }
    let tried = if context.state_tried.is_empty() {
        " なし".as_bytes().to_vec()
    } else {
        context.state_tried.clone()
    };
    let mut text = "本実行の生出力を置く保護領域を外部の書き込み範囲外に解決できない(試した候補:"
        .as_bytes()
        .to_vec();
    text.extend_from_slice(&tried);
    text.extend_from_slice(
        "。/tmp・$TMPDIR・--cwd の配下は使えない。$XDG_STATE_HOME を書き込み範囲外へ設定する)"
            .as_bytes(),
    );
    Err(shell.die(20, "internal", &text))
}
