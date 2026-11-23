//! 旧bashの自分の診断(`LC_ALL=C`の英語に固定。2026-11-11 22:31の利用者決定)と、
//! 旧スクリプトの行番号の固定表の土台。
//!
//! 行番号はRustのソースからではなく、旧スクリプトの処理地点の表から出す。表の各行には、
//! 旧スクリプトのその行にあるべき字面を持たせ、旧スクリプトと食い違えば検査で止める。

use dev_workflow_platform::r4g::process::BashExecFailure;

/// 旧スクリプトの処理地点1つ。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct LinePoint {
    /// 地点の名前(Rustの処理から引く鍵)。
    pub name: &'static str,
    /// 旧スクリプトの行番号(1から)。
    pub line: u32,
    /// 旧スクリプトのその行に含まれるべき字面。
    pub fingerprint: &'static str,
}

/// 行番号の固定表。
#[derive(Clone, Copy, Debug)]
pub struct LineTable {
    pub points: &'static [LinePoint],
}

impl LineTable {
    /// 地点の行番号。表に無い地点は誤りなので、呼出側の検査で止まるよう`None`を返す。
    pub fn line(&self, name: &str) -> Option<u32> {
        self.points
            .iter()
            .find(|point| point.name == name)
            .map(|point| point.line)
    }

    /// 旧スクリプトの本文と照らす。名前の重複・行の範囲外・字面の不一致を報告する。
    pub fn verify(&self, source: &[u8]) -> Result<(), String> {
        let lines: Vec<&[u8]> = source.split(|byte| *byte == b'\n').collect();
        for (index, point) in self.points.iter().enumerate() {
            if self.points[..index]
                .iter()
                .any(|other| other.name == point.name)
            {
                return Err(format!("地点の名前が重複: {}", point.name));
            }
            let Some(text) = (point.line as usize)
                .checked_sub(1)
                .and_then(|line| lines.get(line))
            else {
                return Err(format!("行が範囲の外: {} ({})", point.name, point.line));
            };
            if point.fingerprint.is_empty()
                || !text
                    .windows(point.fingerprint.len())
                    .any(|window| window == point.fingerprint.as_bytes())
            {
                return Err(format!("行の字面が違う: {} ({})", point.name, point.line));
            }
        }
        Ok(())
    }
}

/// 旧bashの自分の診断の前置き。`script`は旧スクリプトの呼出名(`$0`)。
///
/// `internal_error`と`file_error`は行番号を付け(`<script>: line N: `)、`sys_error`は付けない。
pub fn prefix(script: &[u8], line: Option<u32>) -> Vec<u8> {
    let mut out = script.to_vec();
    match line {
        Some(line) => out.extend_from_slice(format!(": line {line}: ").as_bytes()),
        None => out.extend_from_slice(b": "),
    }
    out
}

/// 組込みの`printf`などの書込みの失敗(`<script>: line N: printf: write error: <strerror>`)。
pub fn write_error(script: &[u8], line: u32, builtin: &str, errno: i32) -> Vec<u8> {
    let mut out = prefix(script, Some(line));
    out.extend_from_slice(
        format!(
            "{builtin}: write error: {}\n",
            super::strerror::strerror(errno)
        )
        .as_bytes(),
    );
    out
}

/// 見つからないコマンド(`<script>: line N: <name>: command not found`)。
pub fn command_not_found(script: &[u8], line: u32, name: &[u8]) -> Vec<u8> {
    let mut out = prefix(script, Some(line));
    out.extend_from_slice(name);
    out.extend_from_slice(b": command not found\n");
    out
}

/// `execve`の失敗の診断(`shell_execve`の文。版の規則は[`BashExecFailure`]を作るときに選ぶ)。
/// `ShellScript`は診断を出さないので`None`。
pub fn exec_failure(
    script: &[u8],
    line: u32,
    path: &[u8],
    failure: &BashExecFailure,
) -> Option<Vec<u8>> {
    let with_line = |rest: &str| {
        let mut out = prefix(script, Some(line));
        out.extend_from_slice(path);
        out.extend_from_slice(rest.as_bytes());
        out
    };
    let strerror =
        |errno: &dev_workflow_platform::r4g::Errno| super::strerror::strerror(errno.raw_os_error());
    Some(match failure {
        BashExecFailure::IsDirectory { .. } => {
            with_line(&format!(": {}\n", super::strerror::strerror(21)))
        }
        BashExecFailure::FileError { errno, .. } => with_line(&format!(": {}\n", strerror(errno))),
        BashExecFailure::RequiredFileNotFound => {
            with_line(": cannot execute: required file not found\n")
        }
        BashExecFailure::BadInterpreter { errno, interpreter } => {
            let mut out = prefix(script, None);
            out.extend_from_slice(path);
            out.extend_from_slice(b": ");
            out.extend_from_slice(interpreter);
            out.extend_from_slice(format!(": bad interpreter: {}\n", strerror(errno)).as_bytes());
            out
        }
        BashExecFailure::BinaryFile => with_line(&format!(
            ": cannot execute binary file: {}\n",
            super::strerror::strerror(8)
        )),
        BashExecFailure::EmptyFile | BashExecFailure::ShellScript => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use dev_workflow_platform::r4g::Errno;
    use std::process::Command;

    const TABLE: LineTable = LineTable {
        points: &[
            LinePoint {
                name: "usage",
                line: 39,
                fingerprint: "usage() { sed -n",
            },
            LinePoint {
                name: "publish-print",
                line: 159,
                fingerprint: "printf '%s\\n' \"$SHA\"",
            },
        ],
    };

    fn reviews_dir() -> Vec<u8> {
        let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..");
        std::fs::read(root.join("plugins/dev-workflow/skills/do-task/scripts/reviews-dir.sh"))
            .unwrap()
    }

    #[test]
    fn the_line_table_is_checked_against_the_old_script() {
        let source = reviews_dir();
        TABLE.verify(&source).unwrap();
        assert_eq!(TABLE.line("publish-print"), Some(159));
        assert_eq!(TABLE.line("missing"), None);
        // 行がずれた・字面が違う・名前が重複する表は止める。
        for points in [
            &[LinePoint {
                name: "x",
                line: 158,
                fingerprint: "printf '%s\\n' \"$SHA\"",
            }][..],
            &[LinePoint {
                name: "x",
                line: 100_000,
                fingerprint: "printf",
            }],
            &[LinePoint {
                name: "x",
                line: 159,
                fingerprint: "",
            }],
            &[TABLE.points[0], TABLE.points[0]],
        ] {
            let points: &'static [LinePoint] = Box::leak(points.to_vec().into_boxed_slice());
            assert!(LineTable { points }.verify(&source).is_err());
        }
    }

    #[test]
    fn diagnostics_match_bash_in_the_c_locale() {
        // 文は基準の版(bash 5.2.21。2026-11-12 00:35 の決定)の規則で作る。基準の版の無い機械(Mac の
        // /bin/bash 3.2 など)では比べず、比べなかったことを出す。
        let version = Command::new("bash")
            .args([
                "-c",
                "echo ${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}.${BASH_VERSINFO[2]}",
            ])
            .env("PATH", "/usr/bin:/bin")
            .env_remove("BASH_ENV")
            .output()
            .unwrap();
        let version = String::from_utf8_lossy(&version.stdout).trim().to_string();
        if version != "5.2.21" {
            eprintln!(
                "diagnostics_match_bash_in_the_c_locale: 比べなかった(この機械の bash は {version}。基準は 5.2.21)"
            );
            return;
        }
        let tree = std::env::temp_dir().join(format!("r4g-bash-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tree);
        std::fs::create_dir(&tree).unwrap();
        let script = tree.join("probe.sh");
        std::fs::write(
            &script,
            "missing-r4g-command\nprintf '%s\\n' x >&-\n./noexec\n",
        )
        .unwrap();
        std::fs::write(tree.join("noexec"), "x\n").unwrap();
        let output = Command::new("bash")
            .arg(&script)
            .current_dir(&tree)
            .env("LC_ALL", "C")
            .env("PATH", "/usr/bin:/bin")
            .env_remove("BASH_ENV")
            .output()
            .unwrap();
        let name = script.as_os_str().as_encoded_bytes();
        let mut expected = command_not_found(name, 1, b"missing-r4g-command");
        expected.extend(write_error(name, 2, "printf", 9));
        expected.extend(
            exec_failure(
                name,
                3,
                b"./noexec",
                &BashExecFailure::FileError {
                    errno: Errno::ACCESS,
                    status: 126,
                },
            )
            .unwrap(),
        );
        assert_eq!(
            String::from_utf8_lossy(&output.stderr),
            String::from_utf8_lossy(&expected)
        );
        std::fs::remove_dir_all(&tree).unwrap();
    }
}
