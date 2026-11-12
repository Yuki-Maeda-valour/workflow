//! 旧版の各階層のhelp(Python 3.10.11の`HelpFormatter`と`textwrap`)。
//!
//! 幅は旧版と同じく`shutil.get_terminal_size().columns - 2`。`COLUMNS`が読めて正の値ならその値、
//! ほかは標準出力の端末の幅(起動の時点で標準出力が無いか端末でなければ80)。
//! 折返しは`_format_usage`・`_format_action`・`_split_lines`・`_fill_text`と、`TextWrapper`の
//! `_wrap_chunks`・`_handle_long_word`の手順をそのまま移した。最後に旧版の`format_help`が
//! 最初の`usage: `を`使い方: `へ置き換える。

use super::args::{Kind, OptionSpec, ParserId, ParserSpec, spec};

fn len(text: &str) -> i64 {
    text.chars().count() as i64
}

/// 幅の計算に使う上限(Pythonの整数は大きさに上限が無い。比べる長さはこれより十分に小さい)。
const HUGE: i64 = 1 << 40;

/// `shutil.get_terminal_size().columns`を、比べる範囲に収めた値。
pub fn clamp_columns(value: i64) -> i64 {
    value.clamp(-HUGE, HUGE)
}

/// `HelpFormatter`の状態。
struct Formatter {
    width: i64,
    max_help_position: i64,
    current_indent: i64,
    action_max_length: i64,
}

fn invocation(option: &OptionSpec) -> String {
    match option.kind {
        Kind::Help | Kind::Flag => option.strings.join(", "),
        Kind::Once | Kind::Append => {
            let args = metavar(option);
            option
                .strings
                .iter()
                .map(|name| format!("{name} {args}"))
                .collect::<Vec<_>>()
                .join(", ")
        }
    }
}

fn metavar(option: &OptionSpec) -> String {
    match option.choices {
        Some(choices) => format!("{{{}}}", choices.join(",")),
        None => option.metavar.to_string(),
    }
}

fn usage_part(option: &OptionSpec) -> String {
    let part = match option.kind {
        Kind::Help | Kind::Flag => option.strings[0].to_string(),
        Kind::Once | Kind::Append => format!("{} {}", option.strings[0], metavar(option)),
    };
    if option.required {
        part
    } else {
        format!("[{part}]")
    }
}

/// `_format_usage`の`part_regexp`(`\(.*?\)+(?=\s|$)|\[.*?\]+(?=\s|$)|\S+`)の`findall`。
fn usage_parts(text: &str) -> Vec<String> {
    let chars: Vec<char> = text.chars().collect();
    let space =
        |c: char| matches!(c, ' ' | '\t' | '\n' | '\r' | '\x0b' | '\x0c') || c.is_whitespace();
    let mut parts = Vec::new();
    let mut index = 0;
    while index < chars.len() {
        let bracket = |open: char, close: char| -> Option<usize> {
            if chars[index] != open {
                return None;
            }
            let mut k = index + 1;
            while k < chars.len() {
                if chars[k] == '\n' {
                    return None;
                }
                if chars[k] == close {
                    let mut end = k;
                    while end < chars.len() && chars[end] == close {
                        end += 1;
                    }
                    if end == chars.len() || space(chars[end]) {
                        return Some(end);
                    }
                }
                k += 1;
            }
            None
        };
        let matched = bracket('(', ')').or_else(|| bracket('[', ']')).or_else(|| {
            let mut end = index;
            while end < chars.len() && !space(chars[end]) {
                end += 1;
            }
            (end > index).then_some(end)
        });
        match matched {
            Some(end) => {
                parts.push(chars[index..end].iter().collect());
                index = end;
            }
            None => index += 1,
        }
    }
    parts
}

impl Formatter {
    fn new(columns: i64) -> Self {
        let width = columns - 2;
        Self {
            width,
            max_help_position: 24.min((width - 20).max(4)),
            current_indent: 0,
            action_max_length: 0,
        }
    }

    /// `_format_usage(usage=None, actions, groups, prefix=None)`。
    fn usage(&self, spec: &ParserSpec) -> String {
        let prefix = "usage: ";
        let prog = spec.prog.to_string();
        let optionals: Vec<String> = spec.options.iter().map(usage_part).collect();
        let positionals: Vec<String> = spec
            .subparsers
            .iter()
            .map(|sub| {
                let names: Vec<&str> = sub.choices.iter().map(|(name, _, _)| *name).collect();
                format!("{{{}}} ...", names.join(","))
            })
            .collect();
        let opt_usage = optionals.join(" ");
        let pos_usage = positionals.join(" ");
        let action_usage = optionals
            .iter()
            .chain(positionals.iter())
            .cloned()
            .collect::<Vec<_>>()
            .join(" ");
        let mut usage = [prog.clone(), action_usage]
            .into_iter()
            .filter(|part| !part.is_empty())
            .collect::<Vec<_>>()
            .join(" ");
        let text_width = self.width - self.current_indent;
        if len(prefix) + len(&usage) > text_width {
            let opt_parts = usage_parts(&opt_usage);
            let pos_parts = usage_parts(&pos_usage);
            let get_lines = |parts: &[String], indent: &str, prefix: Option<&str>| -> Vec<String> {
                let mut lines = Vec::new();
                let mut line: Vec<&str> = Vec::new();
                let mut line_len = match prefix {
                    Some(prefix) => len(prefix) - 1,
                    None => len(indent) - 1,
                };
                for part in parts {
                    if line_len + 1 + len(part) > text_width && !line.is_empty() {
                        lines.push(format!("{indent}{}", line.join(" ")));
                        line.clear();
                        line_len = len(indent) - 1;
                    }
                    line.push(part);
                    line_len += len(part) + 1;
                }
                if !line.is_empty() {
                    lines.push(format!("{indent}{}", line.join(" ")));
                }
                if prefix.is_some() {
                    lines[0] = lines[0].chars().skip(indent.chars().count()).collect();
                }
                lines
            };
            let lines: Vec<String> = if 4 * (len(prefix) + len(&prog)) <= 3 * text_width {
                let indent = " ".repeat((len(prefix) + len(&prog) + 1) as usize);
                if !opt_parts.is_empty() {
                    let mut first = vec![prog.clone()];
                    first.extend(opt_parts.iter().cloned());
                    let mut lines = get_lines(&first, &indent, Some(prefix));
                    lines.extend(get_lines(&pos_parts, &indent, None));
                    lines
                } else if !pos_parts.is_empty() {
                    let mut first = vec![prog.clone()];
                    first.extend(pos_parts.iter().cloned());
                    get_lines(&first, &indent, Some(prefix))
                } else {
                    vec![prog.clone()]
                }
            } else {
                let indent = " ".repeat(len(prefix) as usize);
                let mut parts = opt_parts.clone();
                parts.extend(pos_parts.iter().cloned());
                let mut lines = get_lines(&parts, &indent, None);
                if lines.len() > 1 {
                    lines = Vec::new();
                    lines.extend(get_lines(&opt_parts, &indent, None));
                    lines.extend(get_lines(&pos_parts, &indent, None));
                }
                let mut all = vec![prog.clone()];
                all.extend(lines);
                all
            };
            usage = lines.join("\n");
        }
        format!("{prefix}{usage}\n\n")
    }

    /// `_format_text`(`text_width = max(width - indent, 11)`)。
    fn text(&self, text: &str) -> String {
        let text_width = (self.width - self.current_indent).max(11);
        let indent = " ".repeat(self.current_indent as usize);
        let lines = wrap(text, text_width);
        let filled: Vec<String> = lines.iter().map(|line| format!("{indent}{line}")).collect();
        format!("{}\n\n", filled.join("\n"))
    }

    /// `add_argument`の`_action_max_length`の更新(区切りの中の字下げ`indent`)。
    fn measure(&mut self, invocations: &[(String, i64)]) {
        for (text, indent) in invocations {
            self.action_max_length = self.action_max_length.max(len(text) + indent);
        }
    }

    /// `_format_action`(字下げ`indent`。`help`が無ければ見出しだけの行)。
    fn action(&self, indent: i64, header: &str, help: Option<&str>) -> String {
        let help_position = (self.action_max_length + 2).min(self.max_help_position);
        let help_width = (self.width - help_position).max(11);
        let action_width = help_position - indent - 2;
        let pad = " ".repeat(indent as usize);
        let Some(help) = help else {
            return format!("{pad}{header}\n");
        };
        let (mut out, indent_first) = if len(header) <= action_width {
            let fill = " ".repeat((action_width - len(header)) as usize);
            (format!("{pad}{header}{fill}  "), 0)
        } else {
            (format!("{pad}{header}\n"), help_position)
        };
        let lines = wrap(help, help_width);
        out.push_str(&format!(
            "{}{}\n",
            " ".repeat(indent_first as usize),
            lines[0]
        ));
        for line in &lines[1..] {
            out.push_str(&format!("{}{line}\n", " ".repeat(help_position as usize)));
        }
        out
    }
}

/// `textwrap.wrap(text, width)`(既定の`TextWrapper`)。旧版の文は空白1つで区切られた語だけで、
/// `wordsep_re`の切り方は空白での切り方と同じになる(Pythonとの照合を単体検査で確かめる)。
pub fn wrap(text: &str, width: i64) -> Vec<String> {
    let mut chunks: Vec<Vec<char>> = Vec::new();
    for (index, word) in text.split(' ').enumerate() {
        if index > 0 {
            chunks.push(vec![' ']);
        }
        if !word.is_empty() {
            chunks.push(word.chars().collect());
        }
    }
    wrap_chunks(chunks, width)
}

/// `TextWrapper._wrap_chunks`(字下げなし・`break_long_words`・`break_on_hyphens`・`drop_whitespace`)。
fn wrap_chunks(mut chunks: Vec<Vec<char>>, width: i64) -> Vec<String> {
    let blank = |chunk: &[char]| chunk.iter().all(|c| c.is_whitespace());
    let mut lines: Vec<String> = Vec::new();
    chunks.reverse();
    while !chunks.is_empty() {
        let mut cur_line: Vec<Vec<char>> = Vec::new();
        let mut cur_len: i64 = 0;
        if !lines.is_empty() && blank(chunks.last().expect("not empty")) {
            chunks.pop();
        }
        while let Some(chunk) = chunks.last() {
            let l = chunk.len() as i64;
            if cur_len + l <= width {
                cur_line.push(chunks.pop().expect("checked"));
                cur_len += l;
            } else {
                break;
            }
        }
        if let Some(chunk) = chunks.pop_if(|chunk| chunk.len() as i64 > width) {
            // `_handle_long_word`
            let space_left = if width < 1 { 1 } else { width - cur_len };
            let mut end = space_left as usize;
            if chunk.len() as i64 > space_left {
                let limit = (space_left as usize).min(chunk.len());
                if let Some(hyphen) = chunk[..limit].iter().rposition(|c| *c == '-')
                    && hyphen > 0
                    && chunk[..hyphen].iter().any(|c| *c != '-')
                {
                    end = hyphen + 1;
                }
            }
            let end = end.min(chunk.len());
            cur_line.push(chunk[..end].to_vec());
            chunks.push(chunk[end..].to_vec());
        }
        if let Some(last) = cur_line.last()
            && blank(last)
        {
            cur_line.pop();
        }
        if !cur_line.is_empty() {
            lines.push(cur_line.iter().flatten().collect());
        }
    }
    lines
}

/// 旧版の`Parser.format_help()`(幅は`shutil.get_terminal_size().columns`)。
pub fn format_help(id: ParserId, columns: i64) -> String {
    let spec = spec(id);
    let mut formatter = Formatter::new(clamp_columns(columns));
    // add_argument の段: 区切りの中は字下げ2、subparsers の各入口は4。
    let mut invocations: Vec<(String, i64)> = spec
        .options
        .iter()
        .map(|option| (invocation(option), 2))
        .collect();
    if let Some(sub) = &spec.subparsers {
        let names: Vec<&str> = sub.choices.iter().map(|(name, _, _)| *name).collect();
        invocations.push((format!("{{{}}}", names.join(",")), 2));
        for (name, _, _) in sub.choices {
            invocations.push((name.to_string(), 4));
        }
    }
    formatter.measure(&invocations);
    let mut parts: Vec<String> = vec![formatter.usage(&spec)];
    if let Some(description) = spec.description {
        parts.push(formatter.text(description));
    }
    // 位置引数の区切りは空なので出ない。
    let mut options = String::new();
    for option in spec.options {
        options.push_str(&formatter.action(2, &invocation(option), Some(option.help)));
    }
    parts.push(format!("\nオプション:\n{options}\n"));
    if let Some(sub) = &spec.subparsers {
        let names: Vec<&str> = sub.choices.iter().map(|(name, _, _)| *name).collect();
        let mut items = formatter.action(2, &format!("{{{}}}", names.join(",")), None);
        for (name, help, _) in sub.choices {
            items.push_str(&formatter.action(4, name, Some(help)));
        }
        parts.push(format!("\n{}:\n{items}\n", sub.title));
    }
    parts.push(formatter.text(super::args::EXIT_HELP));
    let mut help: String = parts.concat();
    // `_long_break_matcher.sub('\n\n', help)`と`strip('\n') + '\n'`。
    while help.contains("\n\n\n") {
        help = help.replace("\n\n\n", "\n\n");
    }
    let help = format!("{}\n", help.trim_matches('\n'));
    help.replacen("usage: ", "使い方: ", 1)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 旧版の特性テストの期待(Python 3.10.12・COLUMNS=80)と同じ。
    #[test]
    fn help_at_80_columns_matches_the_characterization() {
        let top = format_help(ParserId::Top, 80);
        assert!(top.starts_with("使い方: implementation-git.py [-h] {prepare,run} ...\n\n"));
        assert!(top.contains("\nオプション:\n  -h, --help     使い方を表示して終了\n\n入口:\n  {prepare,run}\n    prepare      委託直前の確認と承認情報の JSON 出力\n"));
        let run = format_help(ParserId::Run, 80);
        assert!(run.starts_with("使い方: implementation-git.py run [-h] --cwd CWD [--accept ACCEPT]\n                                 --expect-context-sha256 EXPECT_CONTEXT_SHA256\n                                 {status,diff,log,show,ls-files} ...\n\n"));
    }

    #[test]
    fn usage_parts_follow_the_regular_expression() {
        assert_eq!(
            usage_parts("[-h] --cwd CWD [--accept ACCEPT]"),
            ["[-h]", "--cwd", "CWD", "[--accept ACCEPT]"]
        );
        assert_eq!(usage_parts("{a,b} ..."), ["{a,b}", "..."]);
    }

    /// 全階層・広い範囲の幅で、旧版のargparseと全文が一致する(旧版を読み込み、同じ幅で作る)。
    #[test]
    fn help_matches_python_argparse_for_many_widths() {
        let script = super::super::lines::old_script_path();
        let oracle = r#"
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location('ig', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.argv[0] = 'implementation-git.py'
parser = module.build_parser()
subs = {}
for action in parser._actions:
    if hasattr(action, '_name_parser_map'):
        subs.update(action._name_parser_map)
run = subs['run']
for action in run._actions:
    if hasattr(action, '_name_parser_map'):
        subs.update(action._name_parser_map)
order = [('top', parser)] + [(name, subs[name]) for name in ('prepare', 'run', 'status', 'diff', 'log', 'show', 'ls-files')]
out = {}
for width in list(range(1, 131)) + [150, 200, 1000]:
    os.environ['COLUMNS'] = str(width)
    out[str(width)] = [p.format_help() for _, p in order]
print(json.dumps(out))
"#;
        let output = std::process::Command::new("python3")
            .args(["-I", "-B", "-c", oracle])
            .arg(&script)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let text = String::from_utf8(output.stdout).unwrap();
        let ids = [
            ParserId::Top,
            ParserId::Prepare,
            ParserId::Run,
            ParserId::Status,
            ParserId::Diff,
            ParserId::Log,
            ParserId::Show,
            ParserId::LsFiles,
        ];
        let widths: Vec<i64> = (1..131).chain([150, 200, 1000]).collect();
        let parsed: serde_json::Value = serde_json::from_str(&text).unwrap();
        for width in widths {
            let expected = parsed[width.to_string()].as_array().unwrap();
            for (index, id) in ids.iter().enumerate() {
                assert_eq!(
                    format_help(*id, width),
                    expected[index].as_str().unwrap(),
                    "{id:?} {width}"
                );
            }
        }
    }
}
