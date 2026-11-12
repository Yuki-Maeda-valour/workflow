//! コマンドの振り分け。`host-paths`・`bootstrap`・`verify`・`host-settings`・`host-allowlist` を動かす。
//! `read`・`exec`・`hook`・`verify-pair` は、引き続き本体なしで止める(段階 2b-2)。
use super::args::Parsed;
use super::bootstrap::bootstrap;
use super::error::PyException;
use super::pyjson::{DumpOptions, PyCtx, dumps};
use super::pyops::Cps;
use super::pypath::UserLookup;
use super::pystr::decode_fs;
use super::pyvalue::PyValue;
use super::reader::read_host_settings;
use super::runtime::PyStream;
use super::settings::{host_allowlist_lines, host_allowlist_rules, host_settings_output};
use super::snapshot::verify;
use super::specs::{Host, host_paths};
use dev_workflow_platform::environment_guard::{
    PasswdLookup, passwd_home, passwd_home_of_current_user,
};
use std::os::unix::ffi::OsStrExt;

/// 利用者データベースを引けない代用品(`~user` は展開しない)。
pub struct NoUsers;

impl UserLookup for NoUsers {
    fn home_of_name(&self, _name: &[u8]) -> Result<Option<Vec<u8>>, PyException> {
        Ok(None)
    }
    fn home_of_current_user(&self) -> Result<Option<Vec<u8>>, PyException> {
        Ok(None)
    }
}

/// 利用者データベース(`pwd`)の引き方(U6)。Linux の musl は `/etc/passwd` だけ、Mac は旧版と同じ仕組み。
/// 引けない・誤りのとき(旧版は `KeyError`)は、展開しない(`None`)。
pub struct PlatformUsers;

fn home(found: std::io::Result<PasswdLookup>) -> Result<Option<Vec<u8>>, PyException> {
    Ok(match found {
        Ok(PasswdLookup::Found(home)) => Some(home),
        Ok(PasswdLookup::NotFound) | Err(_) => None,
    })
}

impl UserLookup for PlatformUsers {
    fn home_of_name(&self, name: &[u8]) -> Result<Option<Vec<u8>>, PyException> {
        home(passwd_home(name))
    }
    fn home_of_current_user(&self) -> Result<Option<Vec<u8>>, PyException> {
        home(passwd_home_of_current_user())
    }
}

/// プロセスの環境変数と作業場所。
pub struct ProcessHost;

impl Host for ProcessHost {
    fn env(&self, name: &str) -> Option<Cps> {
        std::env::var_os(name).map(|value| decode_fs(value.as_bytes()))
    }
    fn cwd(&self) -> Result<Cps, PyException> {
        std::env::current_dir()
            .map(|path| decode_fs(path.as_os_str().as_bytes()))
            .map_err(|error| super::error::from_io(&error, None))
    }
    fn users(&self) -> &dyn UserLookup {
        &PlatformUsers
    }
}

/// 旧版を `python3 -B environment-guard.py` で動かしたときの、`main` のフレームの深さ
/// (`<module>` と `main`)。`json.dumps` を呼ぶフレーム。
pub const MAIN_DEPTH: usize = 2;

/// コマンドの入口の関数のフレームの深さ(旧版の `main` から 1 つ下: `run_copy`・`host_allowlist`)。
const CALLEE_DEPTH: usize = MAIN_DEPTH + 1;

fn arg_text(parsed: &Parsed, dest: &str) -> Cps {
    parsed
        .namespace
        .value(dest)
        .expect("`argparse` が必須の引数を確かめている")
        .py_str()
}

/// 旧版の `main` の、コマンドごとの分岐。戻り値は `SystemExit` の値。
pub fn dispatch(
    host: &dyn Host,
    parsed: &Parsed,
    stdout: &mut PyStream<'_>,
) -> Result<i64, PyException> {
    let digits = host.max_str_digits();
    let main_ctx = PyCtx {
        depth: MAIN_DEPTH,
        max_str_digits: digits,
    };
    match parsed.command.as_str() {
        "host-paths" => {
            let paths = host_paths(host)?;
            // 整数を出さないので、桁数の上限は関係しない。
            let ctx = PyCtx::new(MAIN_DEPTH);
            let line = dumps(&PyValue::Dict(paths), DumpOptions::HOST, ctx)?;
            stdout.print(&line)?;
            Ok(0)
        }
        "bootstrap" => {
            bootstrap(
                host,
                &parsed.namespace,
                &mut |line| stdout.print(line),
                main_ctx,
            )?;
            Ok(0)
        }
        "verify" => {
            let current = parsed
                .namespace
                .value("current_inventory")
                .map(|v| v.py_str());
            verify(
                host,
                &arg_text(parsed, "state"),
                &arg_text(parsed, "expect_sha256"),
                current.as_deref(),
                PyCtx {
                    depth: CALLEE_DEPTH,
                    ..main_ctx
                },
            )?;
            Ok(0)
        }
        "host-settings" => {
            let result = read_host_settings(
                host,
                &arg_text(parsed, "state"),
                &arg_text(parsed, "expect_sha256"),
                main_ctx,
            )?;
            let line = host_settings_output(&result, main_ctx)?;
            stdout.print(&line)?;
            Ok(0)
        }
        "host-allowlist" => {
            // `host_allowlist` の本体(`main` の 1 つ下)が `read_host_settings` を呼ぶ。
            let settings = read_host_settings(
                host,
                &arg_text(parsed, "state"),
                &arg_text(parsed, "expect_sha256"),
                PyCtx {
                    depth: CALLEE_DEPTH,
                    ..main_ctx
                },
            )?;
            let (rules, unused) =
                host_allowlist_rules(&settings, parsed.namespace.many("allowed_tools"))?;
            for line in host_allowlist_lines(&rules, &unused, main_ctx)? {
                stdout.print(&line)?;
            }
            Ok(0)
        }
        _ => Err(PyException::stop("このコマンドの本体は段階 2b で実装する")),
    }
}

#[cfg(test)]
mod tests {
    use super::super::output::run_main;
    use super::super::pystr::decode_fs;
    use super::super::runtime::{Env, StreamKind};
    use super::super::testing::{fake_host, fixture, from_hex};
    use super::*;

    #[test]
    fn host_paths_through_run_main_matches_python_3_10() {
        // 旧版の `main` を動かした出力(標準出力・標準エラー・終了コード)と比べる。
        let cases = fixture!("settings_cases.json");
        let list = cases["host_paths_main"].as_array().unwrap();
        assert!(list.len() >= 100, "{}", list.len());
        let (mut ok, mut failed) = (0, 0);
        for case in list {
            let host = fake_host(&case["env"], &decode_fs(b"/"));
            let args = vec![decode_fs(b"host-paths")];
            let env = Env {
                columns: None,
                terminal_columns: 80,
                unbuffered: false,
                max_str_digits: 4300,
            };
            let (mut out, mut err) = (Vec::new(), Vec::new());
            let status = {
                let mut stdout = PyStream::new(&mut out, StreamKind::Stdout, false, 4096, false);
                let mut stderr = PyStream::new(&mut err, StreamKind::Stderr, false, 4096, false);
                run_main(
                    &args,
                    &env,
                    &mut stdout,
                    &mut stderr,
                    &mut |parsed, stdout, _| dispatch(&host, parsed, stdout),
                )
            };
            if status == 0 {
                ok += 1;
            } else {
                failed += 1;
            }
            let label = format!("{}", case["env"]);
            assert_eq!(i64::from(status), case["code"].as_i64().unwrap(), "{label}");
            assert_eq!(out, from_hex(case["stdout"].as_str().unwrap()), "{label}");
            assert_eq!(err, from_hex(case["stderr"].as_str().unwrap()), "{label}");
        }
        assert!(ok >= 20 && failed >= 20, "{ok} {failed}");
    }

    #[test]
    fn the_other_commands_stop_without_a_body() {
        let host = fake_host(&serde_json::json!({}), &decode_fs(b"/"));
        let args = vec![
            decode_fs(b"hook"),
            decode_fs(b"--state"),
            decode_fs(b"s"),
            decode_fs(b"--expect-sha256"),
            decode_fs(b"h"),
            decode_fs(b"--path"),
            decode_fs(b"p"),
        ];
        let env = Env {
            columns: None,
            terminal_columns: 80,
            unbuffered: false,
            max_str_digits: 4300,
        };
        let (mut out, mut err) = (Vec::new(), Vec::new());
        let status = {
            let mut stdout = PyStream::new(&mut out, StreamKind::Stdout, false, 4096, false);
            let mut stderr = PyStream::new(&mut err, StreamKind::Stderr, false, 4096, false);
            run_main(
                &args,
                &env,
                &mut stdout,
                &mut stderr,
                &mut |parsed, stdout, _| dispatch(&host, parsed, stdout),
            )
        };
        assert_eq!(status, 20);
        assert!(out.is_empty());
        assert!(
            String::from_utf8(err)
                .unwrap()
                .starts_with("ERROR [environment-guard] ")
        );
    }
}
