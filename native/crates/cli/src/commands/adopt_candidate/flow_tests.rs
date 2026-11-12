//! `flow::adopt` の単体テスト。実ファイル・実 Git・実の OS 部品で動かし、依存部品(#471・#472)だけを
//! 代役(`StubCandidates`・`StubPrecheck`。`#[cfg(test)]` の中だけにある)にする。
//! 旧 `scripts/test_adopt_candidate.py` の観点(A01〜A26 のうちこの層で作れるもの)に当たる。
use super::args::{ArgValue, Arguments};
use super::flow::{Candidates, Child, LinkFailure, Ops, Precheck, Problem, Read, adopt};
use super::output::PyJson;
use super::paths::PyPath;
use super::plan::{Stop, plan};
use dev_workflow_platform::adopt_candidate as os;
use sha2::{Digest, Sha256};
use std::cell::{Cell, RefCell};
use std::path::{Path, PathBuf};
use std::process::Command;

const BODY: &str = "> **発見元**: refactor\n> **指摘キー**: H41\n\n## 指摘\n内容\n";

/// 代役: 本文に `SKIP` があれば見送り、`WARN` があれば警告 1 件を返す(#471 の実部品ではない)。
pub(super) struct StubCandidates;
impl Candidates for StubCandidates {
    fn check_text(&self, text: &str) -> Result<Vec<Problem>, Vec<u8>> {
        let mut problems = Vec::new();
        for (needle, code) in [
            ("WARN", "source-count"),
            ("WARN2", "key-count"),
            ("SKIP", "skipped"),
            ("MALFORMED", "skipped-malformed"),
        ] {
            if text.contains(needle) {
                problems.push(Problem {
                    code: Some(code.as_bytes().to_vec()),
                    json: PyJson::Dict(vec![(b"code".to_vec(), PyJson::text(code))]),
                });
            }
        }
        Ok(problems)
    }
}

/// 代役: 決めた終了値と stderr を返す(#472 の実部品ではない)。呼ばれた引数を記録する。
pub(super) struct StubPrecheck {
    code: i32,
    stderr: Vec<u8>,
    calls: RefCell<Vec<(Vec<u8>, Option<Vec<u8>>)>>,
}
impl StubPrecheck {
    pub(super) fn passing() -> Self {
        Self {
            code: 0,
            stderr: Vec::new(),
            calls: RefCell::new(Vec::new()),
        }
    }
}
impl<D> Precheck<D> for StubPrecheck {
    fn run(
        &self,
        _dir: &D,
        root: &[u8],
        accept: Option<&[u8]>,
        _env: &[(Vec<u8>, Vec<u8>)],
    ) -> Result<Child, Vec<u8>> {
        self.calls
            .borrow_mut()
            .push((root.to_vec(), accept.map(<[u8]>::to_vec)));
        Ok(Child {
            code: self.code,
            stdout: b"filter-token".to_vec(),
            stderr: self.stderr.clone(),
        })
    }
}

/// 呼び出しを数え、途中で作業場所を書き換える・失敗を差し込むための包み。
struct Hooked {
    inner: super::real::RealOps,
    loop_calls: Cell<usize>,
    loop_after: Cell<usize>,
    rename_result: RefCell<Option<Result<bool, Vec<u8>>>>,
    before_run: RefCell<Option<Box<dyn Fn(&[Vec<u8>])>>>,
    after_run: RefCell<Option<Box<dyn Fn(&[Vec<u8>])>>>,
    after_link: RefCell<Option<Box<dyn Fn()>>>,
    before_unlink_source: RefCell<Option<Box<dyn Fn()>>>,
    fail_unlink: Cell<bool>,
    interrupt: Cell<bool>,
    /// 元の候補の削除が成功した直後に SIGINT が届いたことにする。
    interrupt_after_source_unlink: Cell<bool>,
    run_log: RefCell<Vec<Vec<Vec<u8>>>>,
}
impl Hooked {
    fn new() -> Self {
        Self {
            inner: super::real::RealOps,
            loop_calls: Cell::new(0),
            loop_after: Cell::new(usize::MAX),
            rename_result: RefCell::new(None),
            before_run: RefCell::new(None),
            after_run: RefCell::new(None),
            after_link: RefCell::new(None),
            before_unlink_source: RefCell::new(None),
            fail_unlink: Cell::new(false),
            interrupt: Cell::new(false),
            interrupt_after_source_unlink: Cell::new(false),
            run_log: RefCell::new(Vec::new()),
        }
    }
}
impl Ops for Hooked {
    type Dir = os::Dir;
    fn open_dir(&self, path: &PyPath) -> Result<os::Dir, Vec<u8>> {
        self.inner.open_dir(path)
    }
    fn read_candidate(&self, dir: &os::Dir, name: &[u8]) -> Result<Read, Vec<u8>> {
        self.inner.read_candidate(dir, name)
    }
    fn stat(&self, dir: &os::Dir, name: &[u8]) -> Result<Option<os::Entry>, Vec<u8>> {
        self.inner.stat(dir, name)
    }
    fn lexists(&self, path: &[u8]) -> bool {
        self.inner.lexists(path)
    }
    fn rename_noreplace(
        &self,
        dir: &os::Dir,
        source: &[u8],
        destination: &[u8],
    ) -> Result<bool, Vec<u8>> {
        match self.rename_result.borrow().clone() {
            Some(forced) => forced,
            None => self.inner.rename_noreplace(dir, source, destination),
        }
    }
    fn link(&self, dir: &os::Dir, source: &[u8], destination: &[u8]) -> Result<(), LinkFailure> {
        let result = self.inner.link(dir, source, destination);
        if result.is_ok()
            && let Some(hook) = self.after_link.borrow().as_ref()
        {
            hook();
        }
        result
    }
    fn unlink(&self, dir: &os::Dir, name: &[u8]) -> Result<(), Vec<u8>> {
        if name.starts_with("候補".as_bytes()) {
            if let Some(hook) = self.before_unlink_source.borrow().as_ref() {
                hook();
            }
            if self.fail_unlink.get() {
                return Err(b"[Errno 1] Operation not permitted: 'source'".to_vec());
            }
        }
        let result = self.inner.unlink(dir, name);
        if result.is_ok()
            && name.starts_with("候補".as_bytes())
            && self.interrupt_after_source_unlink.get()
        {
            self.interrupt.set(true);
        }
        result
    }
    fn run(
        &self,
        dir: &os::Dir,
        argv: &[Vec<u8>],
        env: &[(Vec<u8>, Vec<u8>)],
        timeout: u32,
    ) -> Result<Child, Vec<u8>> {
        self.run_log.borrow_mut().push(argv.to_vec());
        if let Some(hook) = self.before_run.borrow().as_ref() {
            hook(argv);
        }
        let result = self.inner.run(dir, argv, env, timeout);
        if let Some(hook) = self.after_run.borrow().as_ref() {
            hook(argv);
        }
        result
    }
    fn protect(&self, enter: bool) -> bool {
        !enter && self.interrupt.get()
    }
    fn interrupted(&self) -> bool {
        self.interrupt.get()
    }
    fn environment(&self) -> Vec<(Vec<u8>, Vec<u8>)> {
        self.inner.environment()
    }
    fn loop_iteration(&self) -> Option<Vec<u8>> {
        let calls = self.loop_calls.get();
        self.loop_calls.set(calls + 1);
        (calls >= self.loop_after.get()).then(|| b"late-marker".to_vec())
    }
}

pub(super) struct Tmp(PathBuf);
impl Tmp {
    /// 先に置かれた symlink を辿らないよう、`create_dir`(既存なら失敗)と乱数の名前で作り、実体のパスにする。
    pub(super) fn new(label: &str) -> Self {
        let base = std::env::temp_dir().canonicalize().unwrap();
        for attempt in 0..100u32 {
            let nanos = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .subsec_nanos();
            let path = base.join(format!(
                "r4c-flow-{label}-{}-{nanos:x}-{attempt}",
                std::process::id()
            ));
            if std::fs::create_dir(&path).is_err() {
                continue;
            }
            let path = path.canonicalize().unwrap();
            let mut at = Some(path.as_path());
            while let Some(dir) = at {
                assert!(
                    !dir.join(".git").exists(),
                    "一時フォルダの祖先に Git がある。TMPDIR を Git の外へ指定すること"
                );
                at = dir.parent();
            }
            return Self(path);
        }
        panic!("一時フォルダを作れない");
    }
    fn path(&self, name: &str) -> PathBuf {
        self.0.join(name)
    }
    pub(super) fn root(&self) -> &Path {
        &self.0
    }
    pub(super) fn write(&self, name: &str, body: &str) {
        std::fs::write(self.path(name), body).unwrap();
    }
    fn read(&self, name: &str) -> String {
        std::fs::read_to_string(self.path(name)).unwrap()
    }
    fn exists(&self, name: &str) -> bool {
        self.path(name).symlink_metadata().is_ok()
    }
    pub(super) fn git(&self, args: &[&str]) -> String {
        let output = Command::new("git")
            .arg("-C")
            .arg(&self.0)
            .args([
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.com",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "init.defaultBranch=main",
            ])
            .args(args)
            .env_remove("GIT_DIR")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "git {args:?}: {}",
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8(output.stdout).unwrap()
    }
}
impl Drop for Tmp {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn digest(body: &str) -> String {
    format!("{:x}", Sha256::digest(body.as_bytes()))
}

fn arguments(tmp: &Tmp, name: Option<&str>, accept: Option<&str>, sha: &str) -> Arguments {
    let text = |value: &str| ArgValue::Text(value.as_bytes().to_vec());
    Arguments {
        root: text(tmp.0.to_str().unwrap()),
        expect_sha256: text(sha),
        name: name.map(text),
        accept: accept.map(text),
        unattended: false,
        candidate: text(tmp.path("候補_例.md").to_str().unwrap()),
    }
}

fn adopt_with(
    ops: &Hooked,
    pre: &StubPrecheck,
    args: &Arguments,
) -> Result<super::flow::Adoption, Stop> {
    let planned = plan(args, None, &|| Ok(b"/".to_vec())).map_err(|stop| stop)?;
    adopt(ops, &StubCandidates, pre, &planned, args)
}

fn message(result: Result<super::flow::Adoption, Stop>) -> String {
    match result {
        Err(Stop::Caught(message)) => String::from_utf8_lossy(&message).into_owned(),
        other => panic!("停止のはず: {other:?}"),
    }
}

fn setup(body: &str) -> Tmp {
    let tmp = Tmp::new("flow");
    tmp.write("候補_例.md", body);
    tmp
}

#[test]
fn untracked_adoption_renames_without_git_and_returns_the_old_result_shape() {
    let tmp = setup(BODY);
    let pre = StubPrecheck::passing();
    let adopted = adopt_with(
        &Hooked::new(),
        &pre,
        &arguments(&tmp, None, None, &digest(BODY)),
    )
    .unwrap();
    assert_eq!(
        adopted.path,
        format!("{}/進行中_例.md", tmp.0.display()).into_bytes()
    );
    assert!(!adopted.tracked && adopted.warnings.is_empty());
    assert!(!tmp.exists("候補_例.md") && tmp.read("進行中_例.md") == BODY);
    assert!(
        pre.calls.borrow().is_empty(),
        "Git が無ければ事前検査は呼ばない"
    );
}

#[test]
fn digest_shape_and_value_are_checked_before_anything_else() {
    let tmp = setup(BODY);
    for bad in [
        digest("other"),
        digest(BODY).to_uppercase(),
        digest(BODY)[..63].to_string(),
        String::new(),
    ] {
        let result = adopt_with(
            &Hooked::new(),
            &StubPrecheck::passing(),
            &arguments(&tmp, None, None, &bad),
        );
        assert_eq!(message(result), "候補の sha256 が保持値と違う");
        assert!(tmp.exists("候補_例.md") && !tmp.exists("進行中_例.md"));
    }
}

#[test]
fn only_the_two_skip_codes_block_and_other_warnings_keep_their_order() {
    let tmp = setup("WARN WARN2\n");
    let adopted = adopt_with(
        &Hooked::new(),
        &StubPrecheck::passing(),
        &arguments(&tmp, None, None, &digest("WARN WARN2\n")),
    )
    .unwrap();
    let codes: Vec<_> = adopted.warnings.iter().map(|w| w.render()).collect();
    assert_eq!(
        codes,
        [
            br#"{"code": "source-count"}"#.to_vec(),
            br#"{"code": "key-count"}"#.to_vec()
        ]
    );
    for body in ["SKIP\n", "MALFORMED\n"] {
        let tmp = setup(body);
        let result = adopt_with(
            &Hooked::new(),
            &StubPrecheck::passing(),
            &arguments(&tmp, None, None, &digest(body)),
        );
        assert_eq!(
            message(result),
            "見送りの行を確認し、削除して commit してから呼び直す"
        );
        assert!(tmp.exists("候補_例.md"));
    }
}

#[test]
fn non_utf8_candidate_stops_with_the_decode_message() {
    let tmp = Tmp::new("utf8");
    std::fs::write(tmp.path("候補_例.md"), b"ab\xe3x").unwrap();
    let sha = format!("{:x}", Sha256::digest(b"ab\xe3x"));
    let result = adopt_with(
        &Hooked::new(),
        &StubPrecheck::passing(),
        &arguments(&tmp, None, None, &sha),
    );
    assert_eq!(
        message(result),
        "'utf-8' codec can't decode byte 0xe3 in position 2: invalid continuation byte"
    );
}

#[test]
fn every_state_name_collides_except_the_source_itself() {
    for state in ["進行中", "完了", "保留", "中断"] {
        let tmp = setup(BODY);
        tmp.write(&format!("{state}_例.md"), "untouched");
        let result = adopt_with(
            &Hooked::new(),
            &StubPrecheck::passing(),
            &arguments(&tmp, None, None, &digest(BODY)),
        );
        assert_eq!(message(result), "同じ名の状態ファイルがある");
        assert_eq!(tmp.read(&format!("{state}_例.md")), "untouched");
        assert!(tmp.exists("候補_例.md"));
    }
    let tmp = setup(BODY);
    tmp.write("候補_別名.md", "third party");
    let result = adopt_with(
        &Hooked::new(),
        &StubPrecheck::passing(),
        &arguments(&tmp, Some("別名"), None, &digest(BODY)),
    );
    assert_eq!(message(result), "同じ名の状態ファイルがある");
    assert_eq!(tmp.read("候補_別名.md"), "third party");
    // 壊れたリンクも存在として拒否する。
    let tmp = setup(BODY);
    std::os::unix::fs::symlink("nowhere", tmp.path("完了_例.md")).unwrap();
    let result = adopt_with(
        &Hooked::new(),
        &StubPrecheck::passing(),
        &arguments(&tmp, None, None, &digest(BODY)),
    );
    assert_eq!(message(result), "同じ名の状態ファイルがある");
}

#[test]
fn symlink_and_fifo_candidates_are_refused() {
    let tmp = Tmp::new("kinds");
    tmp.write("real.md", BODY);
    std::os::unix::fs::symlink("real.md", tmp.path("候補_例.md")).unwrap();
    let result = adopt_with(
        &Hooked::new(),
        &StubPrecheck::passing(),
        &arguments(&tmp, None, None, &digest(BODY)),
    );
    // ELOOP の番号は機種で違う(Linux 40・Mac 62)。文は同じ。
    let eloop = if cfg!(target_os = "linux") { 40 } else { 62 };
    assert_eq!(
        message(result),
        format!("[Errno {eloop}] Too many levels of symbolic links: '候補_例.md'")
    );
    let tmp = Tmp::new("fifo");
    assert!(
        Command::new("mkfifo")
            .arg(tmp.path("候補_例.md"))
            .status()
            .unwrap()
            .success()
    );
    let result = adopt_with(
        &Hooked::new(),
        &StubPrecheck::passing(),
        &arguments(&tmp, None, None, &digest(BODY)),
    );
    assert_eq!(message(result), "候補は 8 MiB 以下の通常ファイルだけを使う");
}

#[test]
fn marker_appearing_before_the_rename_stops_unchanged() {
    let tmp = setup(BODY);
    let ops = Hooked::new();
    ops.loop_after.set(1);
    let result = adopt_with(
        &ops,
        &StubPrecheck::passing(),
        &arguments(&tmp, None, None, &digest(BODY)),
    );
    assert_eq!(
        message(result),
        "DEV_WORKFLOW_LOOP_ITER があるため候補を採用できない"
    );
    assert!(tmp.exists("候補_例.md") && !tmp.exists("進行中_例.md"));
}

#[test]
fn forced_fallback_links_then_unlinks_and_keeps_the_same_inode() {
    use std::os::unix::fs::MetadataExt;
    let tmp = setup(BODY);
    let inode = std::fs::metadata(tmp.path("候補_例.md")).unwrap().ino();
    let ops = Hooked::new();
    *ops.rename_result.borrow_mut() = Some(Ok(false));
    adopt_with(
        &ops,
        &StubPrecheck::passing(),
        &arguments(&tmp, None, None, &digest(BODY)),
    )
    .unwrap();
    assert!(!tmp.exists("候補_例.md"));
    assert_eq!(
        std::fs::metadata(tmp.path("進行中_例.md")).unwrap().ino(),
        inode
    );
}

#[test]
fn rename_errors_other_than_enosys_and_einval_stop_with_the_old_text() {
    let tmp = setup(BODY);
    let ops = Hooked::new();
    *ops.rename_result.borrow_mut() = Some(Err(b"[Errno 17] \xe4\xb8\x8a\xe6\x9b\xb8\xe3\x81\x8d\xe3\x81\x97\xe3\x81\xaa\xe3\x81\x84\xe6\x94\xb9\xe5\x90\x8d\xe3\x81\xab\xe5\xa4\xb1\xe6\x95\x97".to_vec()));
    let result = adopt_with(
        &ops,
        &StubPrecheck::passing(),
        &arguments(&tmp, None, None, &digest(BODY)),
    );
    assert_eq!(message(result), "[Errno 17] 上書きしない改名に失敗");
    assert!(tmp.exists("候補_例.md"));
}

fn fallback_ops(tmp: &Tmp) -> Hooked {
    let _ = tmp;
    let ops = Hooked::new();
    *ops.rename_result.borrow_mut() = Some(Ok(false));
    ops
}

#[test]
fn fallback_keeps_a_destination_replaced_after_link() {
    let tmp = setup(BODY);
    let ops = fallback_ops(&tmp);
    let (dest, body) = (tmp.path("進行中_例.md"), "third party");
    *ops.after_link.borrow_mut() = Some(Box::new(move || {
        std::fs::remove_file(&dest).unwrap();
        std::fs::write(&dest, body).unwrap();
    }));
    let result = adopt_with(
        &ops,
        &StubPrecheck::passing(),
        &arguments(&tmp, None, None, &digest(BODY)),
    );
    assert_eq!(
        message(result),
        "改名先を自分が作った通常ファイルと確認できない"
    );
    assert!(tmp.read("候補_例.md") == BODY && tmp.read("進行中_例.md") == "third party");
}

#[test]
fn fallback_unlink_failure_removes_only_our_own_destination() {
    let tmp = setup(BODY);
    let ops = fallback_ops(&tmp);
    ops.fail_unlink.set(true);
    let result = adopt_with(
        &ops,
        &StubPrecheck::passing(),
        &arguments(&tmp, None, None, &digest(BODY)),
    );
    assert_eq!(
        message(result),
        "非原子的な採用を完了できない(自身が作った宛先を戻した)"
    );
    assert!(tmp.read("候補_例.md") == BODY && !tmp.exists("進行中_例.md"));

    let tmp = setup(BODY);
    let ops = fallback_ops(&tmp);
    ops.fail_unlink.set(true);
    let dest = tmp.path("進行中_例.md");
    *ops.before_unlink_source.borrow_mut() = Some(Box::new(move || {
        std::fs::remove_file(&dest).unwrap();
        std::fs::write(&dest, "third party").unwrap();
    }));
    let result = adopt_with(
        &ops,
        &StubPrecheck::passing(),
        &arguments(&tmp, None, None, &digest(BODY)),
    );
    assert_eq!(
        message(result),
        "非原子的な採用を完了できない(候補と宛先を保全した)"
    );
    assert!(tmp.read("候補_例.md") == BODY && tmp.read("進行中_例.md") == "third party");
}

#[test]
fn sigint_between_link_and_unlink_rolls_back_our_destination_like_the_old_base_exception_handler() {
    let tmp = setup(BODY);
    let ops = fallback_ops(&tmp);
    ops.interrupt.set(true);
    let result = adopt_with(
        &ops,
        &StubPrecheck::passing(),
        &arguments(&tmp, None, None, &digest(BODY)),
    );
    assert_eq!(
        message(result),
        "非原子的な採用を完了できない(自身が作った宛先を戻した)"
    );
    assert!(tmp.read("候補_例.md") == BODY && !tmp.exists("進行中_例.md"));
}

#[test]
fn sigint_noticed_right_after_the_source_unlink_rolls_back_like_the_old_try_block() {
    let tmp = setup(BODY);
    let ops = fallback_ops(&tmp);
    ops.interrupt_after_source_unlink.set(true);
    let result = adopt_with(
        &ops,
        &StubPrecheck::passing(),
        &arguments(&tmp, None, None, &digest(BODY)),
    );
    assert_eq!(
        message(result),
        "非原子的な採用を完了できない(自身が作った宛先を戻した)"
    );
    // 旧版の動作: 元は削除済みで、宛先も戻すので、どちらの名前も残らない。
    assert!(!tmp.exists("候補_例.md") && !tmp.exists("進行中_例.md"));
}

#[test]
fn fallback_refuses_an_existing_destination_that_appears_late() {
    let tmp = setup(BODY);
    let ops = fallback_ops(&tmp);
    // 衝突検査の後・link の前に、第三者が先を作る。
    let dest = tmp.path("進行中_例.md");
    let created = Cell::new(false);
    *ops.after_run.borrow_mut() = None;
    *ops.before_unlink_source.borrow_mut() = None;
    let _ = created;
    std::fs::write(&dest, "third party").unwrap();
    let args = arguments(&tmp, Some("別"), None, &digest(BODY));
    // 採用名を変えて衝突検査を通し、link の先だけ既存にする。
    std::fs::write(tmp.path("進行中_別.md"), "x").unwrap();
    std::fs::remove_file(tmp.path("進行中_別.md")).unwrap();
    let planned = plan(&args, None, &|| Ok(b"/".to_vec())).unwrap();
    let held = ops.open_dir(&planned.root).unwrap();
    assert!(matches!(
        ops.link(&held, "候補_例.md".as_bytes(), "進行中_例.md".as_bytes()),
        Err(LinkFailure::Exists)
    ));
    assert_eq!(tmp.read("進行中_例.md"), "third party");
}

fn tracked_setup(body: &str) -> Tmp {
    let tmp = setup(body);
    tmp.git(&["init", "-q"]);
    tmp.git(&["add", "候補_例.md"]);
    tmp.git(&["commit", "-q", "-m", "init"]);
    tmp
}

fn staged_names(tmp: &Tmp) -> String {
    tmp.git(&[
        "-c",
        "core.quotePath=false",
        "diff",
        "--cached",
        "--name-status",
        "-M",
    ])
}

#[test]
fn tracked_candidate_is_moved_with_git_and_the_index_follows() {
    let tmp = tracked_setup(BODY);
    let pre = StubPrecheck::passing();
    let ops = Hooked::new();
    let adopted = adopt_with(
        &ops,
        &pre,
        &arguments(&tmp, None, Some("ok"), &digest(BODY)),
    )
    .unwrap();
    assert!(adopted.tracked);
    assert!(!tmp.exists("候補_例.md") && tmp.read("進行中_例.md") == BODY);
    assert!(staged_names(&tmp).starts_with("R100\t候補_例.md\t進行中_例.md"));
    assert_eq!(
        pre.calls.borrow().as_slice(),
        [(
            tmp.0.to_str().unwrap().as_bytes().to_vec(),
            Some(b"ok".to_vec())
        )]
    );
    // 子の起動順と、旧 `SAFE_GIT_PREFIX` の全 26 要素(順序も同じ)。
    let log = ops.run_log.borrow();
    let after_prefix: Vec<String> = log
        .iter()
        .map(|argv| {
            argv[27..]
                .iter()
                .map(|a| String::from_utf8_lossy(a).into_owned())
                .collect::<Vec<_>>()
                .join(" ")
        })
        .collect();
    assert_eq!(after_prefix[0], "rev-parse --show-toplevel");
    assert!(after_prefix[1].starts_with("--literal-pathspecs ls-files --error-unmatch --"));
    assert!(after_prefix[2].starts_with("--literal-pathspecs ls-files --stage -z --"));
    assert!(after_prefix[3].starts_with("--literal-pathspecs mv --"));
    assert!(after_prefix[4].starts_with("--literal-pathspecs ls-files --stage -z --"));
    let expected: Vec<String> = [
        "git",
        "--no-pager",
        "--no-replace-objects",
        "-c",
        "core.quotePath=false",
        "-c",
        "core.fsmonitor=",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.ignoreCase=false",
        "-c",
        "core.splitIndex=false",
        "-c",
        "core.ignoreStat=false",
        "-c",
        "commit.gpgSign=false",
        "-c",
        "push.gpgSign=false",
        "-c",
        "filter.lfs.smudge=",
        "-c",
        "filter.lfs.clean=",
        "-c",
        "filter.lfs.process=",
        "-c",
        "filter.lfs.required=false",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    for argv in log.iter() {
        let head: Vec<String> = argv[..27]
            .iter()
            .map(|a| String::from_utf8_lossy(a).into_owned())
            .collect();
        assert_eq!(head, expected);
    }
}

#[test]
fn empty_accept_is_not_passed_to_the_precheck() {
    let tmp = tracked_setup(BODY);
    let pre = StubPrecheck::passing();
    adopt_with(
        &Hooked::new(),
        &pre,
        &arguments(&tmp, None, Some(""), &digest(BODY)),
    )
    .unwrap();
    assert_eq!(pre.calls.borrow()[0].1, None);
}

#[test]
fn unstaged_and_partially_staged_edits_keep_the_held_index_blob() {
    let tmp = tracked_setup(BODY);
    let committed = tmp.git(&["rev-parse", ":候補_例.md"]);
    let edited = format!("{BODY}unstaged\n");
    tmp.write("候補_例.md", &edited);
    adopt_with(
        &Hooked::new(),
        &StubPrecheck::passing(),
        &arguments(&tmp, None, None, &digest(&edited)),
    )
    .unwrap();
    assert_eq!(tmp.git(&["rev-parse", ":進行中_例.md"]), committed);
    assert_eq!(tmp.read("進行中_例.md"), edited);

    let tmp = tracked_setup(BODY);
    let staged = format!("{BODY}staged\n");
    tmp.write("候補_例.md", &staged);
    tmp.git(&["add", "候補_例.md"]);
    let staged_blob = tmp.git(&["rev-parse", ":候補_例.md"]);
    let full = format!("{staged}more\n");
    tmp.write("候補_例.md", &full);
    adopt_with(
        &Hooked::new(),
        &StubPrecheck::passing(),
        &arguments(&tmp, None, None, &digest(&full)),
    )
    .unwrap();
    assert_eq!(tmp.git(&["rev-parse", ":進行中_例.md"]), staged_blob);
    assert_eq!(tmp.read("進行中_例.md"), full);
}

#[test]
fn replacement_before_git_mv_is_reported_with_the_staged_residue() {
    let tmp = tracked_setup(BODY);
    let ops = Hooked::new();
    let source = tmp.path("候補_例.md");
    *ops.before_run.borrow_mut() = Some(Box::new(move |argv| {
        if argv.iter().any(|a| a == b"mv") {
            std::fs::remove_file(&source).unwrap();
            std::fs::write(&source, "third party\n").unwrap();
        }
    }));
    let result = adopt_with(
        &ops,
        &StubPrecheck::passing(),
        &arguments(&tmp, None, None, &digest(BODY)),
    );
    assert_eq!(
        message(result),
        "追跡済み候補の改名後に本文または inode が保持値と違う。候補と index の残存状態を人が確認する"
    );
    assert!(!tmp.exists("候補_例.md") && tmp.read("進行中_例.md") == "third party\n");
    assert!(staged_names(&tmp).starts_with("R"));
}

#[test]
fn index_replaced_after_git_mv_is_reported_without_repair() {
    let tmp = tracked_setup(BODY);
    let ops = Hooked::new();
    let (root, dest) = (tmp.0.clone(), "進行中_例.md");
    *ops.after_run.borrow_mut() = Some(Box::new(move |argv| {
        if argv.iter().any(|a| a == b"mv") {
            let hashed = Command::new("git")
                .arg("-C")
                .arg(&root)
                .args(["hash-object", "-w", "--stdin"])
                .stdin(std::process::Stdio::piped())
                .stdout(std::process::Stdio::piped())
                .spawn()
                .and_then(|mut child| {
                    use std::io::Write;
                    child.stdin.take().unwrap().write_all(b"third party\n")?;
                    child.wait_with_output()
                })
                .unwrap();
            let oid = String::from_utf8(hashed.stdout).unwrap().trim().to_owned();
            assert!(
                Command::new("git")
                    .arg("-C")
                    .arg(&root)
                    .args([
                        "update-index",
                        "--cacheinfo",
                        &format!("100644,{oid},{dest}")
                    ])
                    .status()
                    .unwrap()
                    .success()
            );
        }
    }));
    let result = adopt_with(
        &ops,
        &StubPrecheck::passing(),
        &arguments(&tmp, None, None, &digest(BODY)),
    );
    assert_eq!(
        message(result),
        "追跡済み候補の改名後の index stage が改名前の保持値と違う。候補と index の残存状態を人が確認する"
    );
    assert_eq!(tmp.read("進行中_例.md"), BODY);
    assert_eq!(
        tmp.git(&["cat-file", "-p", ":進行中_例.md"]),
        "third party\n"
    );
}

#[test]
fn precheck_refusal_keeps_the_stderr_and_stops_before_any_git_command() {
    let tmp = tracked_setup(BODY);
    let pre = StubPrecheck {
        code: 1,
        stderr: b"ERROR bad \xff".to_vec(),
        calls: RefCell::new(Vec::new()),
    };
    let ops = Hooked::new();
    let result = adopt_with(&ops, &pre, &arguments(&tmp, None, None, &digest(BODY)));
    assert_eq!(
        message(result),
        "git の事前検査に通らない: ERROR bad \u{fffd}"
    );
    assert!(tmp.exists("候補_例.md") && ops.run_log.borrow().is_empty());
    assert!(staged_names(&tmp).is_empty());
}

#[test]
fn untracked_candidate_inside_a_repository_uses_the_native_rename() {
    let tmp = Tmp::new("untracked");
    tmp.git(&["init", "-q"]);
    tmp.write("候補_例.md", BODY);
    let ops = Hooked::new();
    let adopted = adopt_with(
        &ops,
        &StubPrecheck::passing(),
        &arguments(&tmp, None, None, &digest(BODY)),
    )
    .unwrap();
    assert!(!adopted.tracked);
    assert_eq!(ops.run_log.borrow().len(), 2, "rev-parse と ls-files だけ");
    assert!(tmp.exists("進行中_例.md") && staged_names(&tmp).is_empty());
}

#[test]
fn missing_git_command_is_the_old_file_not_found_text() {
    let tmp = Tmp::new("nogit");
    tmp.git(&["init", "-q"]);
    tmp.write("候補_例.md", BODY);
    // 実の Ops の run で、PATH に git が無い環境(空の環境)を与える。
    let dir = super::real::RealOps
        .open_dir(&PyPath::parse(tmp.0.to_str().unwrap().as_bytes()))
        .unwrap();
    let error = super::real::RealOps
        .run(
            &dir,
            &[b"git".to_vec(), b"status".to_vec()],
            &[(b"PATH".to_vec(), b"/nonexistent".to_vec())],
            30,
        )
        .err()
        .unwrap();
    assert_eq!(
        String::from_utf8(error).unwrap(),
        "[Errno 2] No such file or directory: 'git'"
    );
    let _ = Path::new("");
}
