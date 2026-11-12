//! 旧 `adopt()` の流れ。OS・依存部品・子の起動は trait の向こうに置き、順序と診断をここで決める。
//! 依存部品(#471 の候補解析・#472 の事前検査)の実物への接続は、両方が main に入った後に足す。
use super::args::{ArgValue, Arguments};
use super::candidate::{CandidateError, FileIdentity, HeldCandidate};
use super::diagnostics as text;
use super::index::{IndexEntry, parse_stage_zero, verify_index_unchanged};
use super::output::PyJson;
use super::paths::PyPath;
use super::plan::{Plan, Stop, Uncaught};
use dev_workflow_platform::adopt_candidate::Entry;

pub(crate) const MAX_CANDIDATE: i64 = 8 * 1024 * 1024;
pub(crate) const GIT_TIMEOUT: u32 = 30;

/// 旧 `SAFE_GIT_PREFIX`。順序も含めて旧版と同じ。
pub(crate) const SAFE_GIT_PREFIX: [&str; 22] = [
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
];
const SAFE_GIT_TAIL: [&str; 4] = [
    "-c",
    "filter.lfs.process=",
    "-c",
    "filter.lfs.required=false",
];

/// 読んだ候補の同一性(dev・ino だけを照らす)。
pub(crate) type Identity = FileIdentity;

#[derive(Debug)]
pub(crate) struct Read {
    pub(crate) data: Vec<u8>,
    pub(crate) identity: Identity,
}

/// リンク作成の失敗。既存の名前は区別する。
pub(crate) enum LinkFailure {
    Exists,
    Other(Vec<u8>),
}

/// 捕捉した子の結果。
pub(crate) struct Child {
    pub(crate) code: i32,
    pub(crate) stdout: Vec<u8>,
    pub(crate) stderr: Vec<u8>,
}

/// OS の操作の境界。失敗は、旧版の例外の表示(`候補の採用を停止: ` の後ろ)で返す。
pub(crate) trait Ops {
    type Dir;
    fn open_dir(&self, path: &PyPath) -> Result<Self::Dir, Vec<u8>>;
    fn read_candidate(&self, dir: &Self::Dir, name: &[u8]) -> Result<Read, Vec<u8>>;
    /// 読む途中(空でない塊を読むたび)に `hook` を呼ぶ版。検査専用の注入が使う。既定は途中の差し込みなし。
    fn read_candidate_during(
        &self,
        dir: &Self::Dir,
        name: &[u8],
        hook: &mut dyn FnMut(),
    ) -> Result<Read, Vec<u8>> {
        let _ = hook;
        self.read_candidate(dir, name)
    }
    fn stat(&self, dir: &Self::Dir, name: &[u8]) -> Result<Option<Entry>, Vec<u8>>;
    fn lexists(&self, path: &[u8]) -> bool;
    fn rename_noreplace(
        &self,
        dir: &Self::Dir,
        source: &[u8],
        destination: &[u8],
    ) -> Result<bool, Vec<u8>>;
    fn link(&self, dir: &Self::Dir, source: &[u8], destination: &[u8]) -> Result<(), LinkFailure>;
    fn unlink(&self, dir: &Self::Dir, name: &[u8]) -> Result<(), Vec<u8>>;
    /// 保持した root から子を起動する。環境は `env` だけ。stdin は継承する。
    fn run(
        &self,
        dir: &Self::Dir,
        argv: &[Vec<u8>],
        env: &[(Vec<u8>, Vec<u8>)],
        timeout_seconds: u32,
    ) -> Result<Child, Vec<u8>>;
    /// SIGINT の保護区間(link から unlink まで)の出入り。入るとき偽、出るとき区間の中で届いたか。
    fn protect(&self, enter: bool) -> bool {
        let _ = enter;
        false
    }
    /// 保護区間の中で SIGINT が届いたか。
    fn interrupted(&self) -> bool {
        false
    }
    /// 保護区間の後に SIGINT が見つかったときの終わり方(KeyboardInterrupt。実物は信号で終わり、戻らない)。
    fn interrupt_exit(&self) {}
    /// 親の環境(`os.environ` の項目)。
    fn environment(&self) -> Vec<(Vec<u8>, Vec<u8>)>;
    /// 環境変数 `DEV_WORKFLOW_LOOP_ITER` の値。
    fn loop_iteration(&self) -> Option<Vec<u8>>;
}

/// 候補解析が返す 1 件の問題。`code` が見送りの 2 種なら採用を拒否する。
pub(crate) struct Problem {
    pub(crate) code: Option<Vec<u8>>,
    pub(crate) json: PyJson,
}

/// #471 の `check_text(text, None)` に当たる境界。実物の接続は #471 のマージ後。
pub(crate) trait Candidates {
    fn check_text(&self, text: &str) -> Result<Vec<Problem>, Vec<u8>>;
}

/// #472 の事前検査(旧 `bash diff-snapshot.sh --cwd <root> --precheck [--accept <値>]`)に当たる境界。
/// 全体の期限は 60 秒で、期限と回収の方法は実部品の接続時(Task 2.1)に決める。
/// 失敗(時間切れ・起動失敗)は、旧版の例外の表示で返す。
pub(crate) trait Precheck<D> {
    fn run(
        &self,
        dir: &D,
        root: &[u8],
        accept: Option<&[u8]>,
        env: &[(Vec<u8>, Vec<u8>)],
    ) -> Result<Child, Vec<u8>>;
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) struct Adoption {
    pub(crate) path: Vec<u8>,
    pub(crate) tracked: bool,
    pub(crate) sha256: Vec<u8>,
    pub(crate) warnings: Vec<PyJson>,
}

fn caught(message: &str) -> Stop {
    Stop::Caught(message.as_bytes().to_vec())
}

fn convert<T>(result: Result<T, Vec<u8>>) -> Result<T, Stop> {
    result.map_err(Stop::Caught)
}

fn refuse_loop(loop_iteration: &Option<Vec<u8>>) -> Result<(), Stop> {
    if loop_iteration
        .as_ref()
        .is_some_and(|value| !value.is_empty())
    {
        return Err(caught(text::LOOP_REFUSAL));
    }
    Ok(())
}

/// 旧 `validate_candidate`。SHA の照合 → UTF-8 → 解析 → 見送り 2 種だけ拒否。
fn validate_candidate<C: Candidates>(
    data: &[u8],
    identity: Identity,
    digest: &ArgValue,
    candidates: &C,
) -> Result<(HeldCandidate, Vec<PyJson>), Stop> {
    let ArgValue::Text(digest) = digest else {
        return Err(Stop::Uncaught(Uncaught::DigestNotString));
    };
    let held = HeldCandidate::capture(data.to_vec(), identity, digest)
        .map_err(|_| caught(text::DIGEST_MISMATCH))?;
    let body = std::str::from_utf8(data)
        .map_err(|error| Stop::Caught(text::utf8_decode_message(data, error).into_bytes()))?;
    let problems = convert(candidates.check_text(body))?;
    let blocking = problems.iter().any(|problem| {
        problem
            .code
            .as_deref()
            .is_some_and(|code| code == b"skipped" || code == b"skipped-malformed")
    });
    if blocking {
        return Err(caught(text::BLOCKING_PROBLEM));
    }
    Ok((held, problems.into_iter().map(|p| p.json).collect()))
}

fn lossy(bytes: &[u8]) -> Vec<u8> {
    String::from_utf8_lossy(bytes).into_owned().into_bytes()
}

/// 子の Git を起動する関数(引数は `git` と固定の指定の後ろ)。
type GitRun<'a, O> = dyn Fn(&O, &<O as Ops>::Dir, &[&[u8]]) -> Result<Child, Vec<u8>> + 'a;

/// `index_stage_zero` の失敗。`Parse` は旧版の `ValueError`(index の解析の失敗)、`Git` は Git の起動・時間切れ。
enum IndexFailure {
    Parse(Vec<u8>),
    Git(Vec<u8>),
}

impl From<IndexFailure> for Stop {
    fn from(failure: IndexFailure) -> Self {
        match failure {
            IndexFailure::Parse(message) | IndexFailure::Git(message) => Stop::Caught(message),
        }
    }
}

/// 旧 `index_stage_zero`。
fn index_stage_zero<O: Ops>(
    ops: &O,
    dir: &O::Dir,
    git: &GitRun<'_, O>,
    relative: &[u8],
    when: &str,
) -> Result<IndexEntry, IndexFailure> {
    let staged = git(
        ops,
        dir,
        &[
            b"--literal-pathspecs",
            b"ls-files",
            b"--stage",
            b"-z",
            b"--",
            relative,
        ],
    )
    .map_err(IndexFailure::Git)?;
    parse_stage_zero(staged.code == 0, &staged.stdout, relative)
        .map_err(|error| IndexFailure::Parse(text::index_message(error, when).into_bytes()))
}

pub(crate) fn adopt<O: Ops, C: Candidates, P: Precheck<O::Dir>>(
    ops: &O,
    candidates: &C,
    precheck: &P,
    plan: &Plan,
    arguments: &Arguments,
) -> Result<Adoption, Stop> {
    let loop_iteration = ops.loop_iteration();
    refuse_loop(&loop_iteration)?;
    let source_name = plan.source.name().to_vec();
    let root_dir = convert(ops.open_dir(&plan.root))?;
    let parent_dir = convert(ops.open_dir(&plan.source.parent()))?;
    let Read { data, identity } = convert(ops.read_candidate(&parent_dir, &source_name))?;
    let (held, warnings) =
        validate_candidate(&data, identity, &arguments.expect_sha256, candidates)?;
    for state in ["候補", "進行中", "完了", "保留", "中断"] {
        let name = [state.as_bytes(), b"_", &plan.new_name, b".md"].concat();
        if name == source_name {
            continue;
        }
        if convert(ops.stat(&parent_dir, &name))?.is_some() {
            return Err(caught(text::NAME_COLLISION));
        }
    }
    let mut env: Vec<(Vec<u8>, Vec<u8>)> = ops
        .environment()
        .into_iter()
        .filter(|(key, _)| !key.starts_with(b"GIT_"))
        .collect();
    for (key, value) in [
        ("GIT_NO_REPLACE_OBJECTS", "1"),
        ("GIT_NO_LAZY_FETCH", "1"),
        ("GIT_OPTIONAL_LOCKS", "0"),
        ("GIT_TERMINAL_PROMPT", "0"),
    ] {
        env.push((key.as_bytes().to_vec(), value.as_bytes().to_vec()));
    }
    let git = |ops: &O, dir: &O::Dir, args: &[&[u8]]| -> Result<Child, Vec<u8>> {
        let mut argv = vec![b"git".to_vec()];
        argv.extend(SAFE_GIT_PREFIX.iter().map(|item| item.as_bytes().to_vec()));
        argv.extend(SAFE_GIT_TAIL.iter().map(|item| item.as_bytes().to_vec()));
        argv.extend(args.iter().map(|item| item.to_vec()));
        ops.run(dir, &argv, &env, GIT_TIMEOUT)
    };
    let relative = plan.relative.to_bytes();
    let mut repository = false;
    let mut cursor = plan.root.clone();
    loop {
        if ops.lexists(&cursor.join(b".git").to_bytes()) {
            repository = true;
            break;
        }
        if cursor.components().is_empty() {
            break;
        }
        cursor = cursor.parent();
    }
    let mut tracked = false;
    if repository {
        let accept = match &arguments.accept {
            Some(ArgValue::Text(value)) if !value.is_empty() => Some(value.as_slice()),
            _ => None,
        };
        let result = convert(precheck.run(&root_dir, &plan.root.to_bytes(), accept, &env))?;
        if result.code != 0 {
            return Err(Stop::Caught(
                [text::PRECHECK_REFUSED.as_bytes(), &lossy(&result.stderr)].concat(),
            ));
        }
        if convert(git(ops, &root_dir, &[b"rev-parse", b"--show-toplevel"]))?.code != 0 {
            return Err(caught(text::WORKTREE_UNKNOWN));
        }
        tracked = convert(git(
            ops,
            &root_dir,
            &[
                b"--literal-pathspecs",
                b"ls-files",
                b"--error-unmatch",
                b"--",
                &relative,
            ],
        ))?
        .code
            == 0;
    }
    let fresh = convert(ops.read_candidate(&parent_dir, &source_name))?;
    if held.verify_after(&fresh.data, fresh.identity).is_err() {
        return Err(caught(text::CHANGED_BEFORE_ADOPT));
    }
    // 正式入口の最後の検査。開始後に印が付いた場合にも改名も stage もしない。
    refuse_loop(&ops.loop_iteration())?;
    let destination = plan.destination()?;
    let destination_name = destination.name().to_vec();
    if tracked {
        let held_stage =
            index_stage_zero(ops, &root_dir, &git, &relative, "改名前の").map_err(Stop::from)?;
        let moved_relative = convert(
            destination
                .relative_to(&plan.root)
                .map(|path| path.to_bytes()),
        )?;
        let moved = convert(git(
            ops,
            &root_dir,
            &[
                b"--literal-pathspecs",
                b"mv",
                b"--",
                &relative,
                &moved_relative,
            ],
        ))?;
        if moved.code != 0 {
            return Err(Stop::Caught(
                [text::GIT_MV_FAILED.as_bytes(), &lossy(&moved.stderr)].concat(),
            ));
        }
        verify_tracked_move(
            ops,
            &parent_dir,
            &root_dir,
            &git,
            &destination_name,
            &moved_relative,
            &held,
            &held_stage,
        )?;
    } else if !convert(ops.rename_noreplace(&parent_dir, &source_name, &destination_name))? {
        fallback_link_unlink(ops, &parent_dir, &source_name, &destination_name, &held)?;
    } else {
        let moved = convert(ops.read_candidate(&parent_dir, &destination_name))?;
        if held.verify_after(&moved.data, moved.identity).is_err() {
            return Err(caught(text::DESTINATION_CHANGED));
        }
    }
    let ArgValue::Text(digest) = &arguments.expect_sha256 else {
        unreachable!("validate_candidate が拒否済み");
    };
    Ok(Adoption {
        path: destination.to_bytes(),
        tracked,
        sha256: digest.clone(),
        warnings,
    })
}

/// 旧 `verify_tracked_move`。
#[allow(clippy::too_many_arguments)]
fn verify_tracked_move<O: Ops>(
    ops: &O,
    parent: &O::Dir,
    root: &O::Dir,
    git: &GitRun<'_, O>,
    destination_name: &[u8],
    moved_relative: &[u8],
    held: &HeldCandidate,
    held_stage: &IndexEntry,
) -> Result<(), Stop> {
    let moved = convert(ops.read_candidate(parent, destination_name))?;
    if held.verify_after(&moved.data, moved.identity).is_err() {
        return Err(caught(text::TRACKED_BODY_CHANGED));
    }
    // 後置きの注意は、旧版の `ValueError`(index の解析の失敗)だけに付く。Git の時間切れ・起動の失敗には付かない。
    let current = match index_stage_zero(ops, root, git, moved_relative, "改名後の") {
        Ok(entry) => entry,
        Err(IndexFailure::Parse(message)) => {
            return Err(Stop::Caught(
                [
                    message.as_slice(),
                    "。候補と index の残存状態を人が確認する".as_bytes(),
                ]
                .concat(),
            ));
        }
        Err(IndexFailure::Git(message)) => return Err(Stop::Caught(message)),
    };
    if verify_index_unchanged(held_stage, &current).is_err() {
        return Err(caught(text::TRACKED_INDEX_CHANGED));
    }
    Ok(())
}

/// 旧 `rollback_created_destination`。作成直後の自分の実体だけを消す。
fn rollback_created_destination<O: Ops>(
    ops: &O,
    parent: &O::Dir,
    destination: &[u8],
    held: (u64, u64),
) -> Result<bool, Vec<u8>> {
    let owned = match ops.stat(parent, destination)? {
        Some(entry) => entry.regular && (entry.dev, entry.ino) == held,
        None => false,
    };
    if owned {
        ops.unlink(parent, destination)?;
        return Ok(true);
    }
    Ok(false)
}

/// 旧 `fallback_link_unlink`。link は上書きせず、unlink 前の失敗では自分の link だけ戻す。
fn fallback_link_unlink<O: Ops>(
    ops: &O,
    parent: &O::Dir,
    source: &[u8],
    destination: &[u8],
    held_candidate: &HeldCandidate,
) -> Result<(), Stop> {
    match ops.link(parent, source, destination) {
        Ok(()) => {}
        Err(LinkFailure::Exists) => return Err(caught(text::DESTINATION_EXISTS)),
        Err(LinkFailure::Other(message)) => return Err(Stop::Caught(message)),
    }
    let identity = held_candidate.identity();
    let created = convert(ops.stat(parent, destination))?;
    let Some(created) = created else {
        return Err(Stop::Caught(text::os_error(
            2,
            &text::strerror(2),
            Some(destination),
        )));
    };
    let held = (created.dev, created.ino);
    if !created.regular || i128::from(held.0) != identity.dev || i128::from(held.1) != identity.ino
    {
        // link 成功直後でも、同じ UID の別プロセスが宛先を差し替え得る。自分の作成物と証明できないので削除しない。
        return Err(caught(text::DESTINATION_NOT_OURS));
    }
    // 旧版は、この区間の例外(KeyboardInterrupt を含む)を `BaseException` で受けて、自分が作った宛先を戻す。
    ops.protect(true);
    let attempt = (|| -> Result<(), Vec<u8>> {
        let fresh = ops.read_candidate(parent, source)?;
        if ops.interrupted() {
            return Err(b"KeyboardInterrupt".to_vec());
        }
        if held_candidate
            .verify_after(&fresh.data, fresh.identity)
            .is_err()
        {
            return Err(text::CHANGED_BEFORE_UNLINK.as_bytes().to_vec());
        }
        ops.unlink(parent, source)?;
        // unlink が返った直後の印(旧版の `try` の中の最後の点)。間に処理を挟まない。
        // 旧版はここで届いた SIGINT も巻き戻す(この狭い窓では、候補の最後の名前を消し得る。旧版の動作)。
        if ops.interrupted() {
            return Err(b"KeyboardInterrupt".to_vec());
        }
        Ok(())
    })();
    let interrupted = ops.protect(false);
    if attempt.is_err() {
        let rolled = rollback_created_destination(ops, parent, destination, held).unwrap_or(false);
        return Err(Stop::Caught(text::non_atomic_failure(rolled).into_bytes()));
    }
    if interrupted {
        // `try` を出た後に届いた SIGINT: 宛先を残し、KeyboardInterrupt で終わる。
        return Err(Stop::Interrupted);
    }
    let moved = convert(ops.read_candidate(parent, destination))?;
    if held_candidate
        .verify_after(&moved.data, moved.identity)
        .is_err()
        || (i128::from(held.0), i128::from(held.1)) != (moved.identity.dev, moved.identity.ino)
    {
        return Err(caught(text::DESTINATION_CHANGED));
    }
    Ok(())
}

impl From<CandidateError> for Stop {
    fn from(_: CandidateError) -> Self {
        caught(text::DIGEST_MISMATCH)
    }
}
