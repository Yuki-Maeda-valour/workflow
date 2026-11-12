//! 置き場の解決・列挙・走査中の変化・印の分類・今回の範囲(旧版の`resolve_directory`と`Scanner`)。
//!
//! 旧版の各関数を同じ名前で移す。OSの呼出し・時計の確認点・例外の捕まえ方と`finally`の順を
//! 旧版と同じにする。旧版の`except (OSError, NotImplementedError)`は[`Exc::Os`]だけを捕まえ、
//! `ScanError`・想定外の例外・`KeyboardInterrupt`は通す。

use super::mark::{Mark, head_is_valid, parse_mark, pointer_target};
use super::report::{Candidate, Doc, Root, StatValues};
use super::system::{Exc, Fd, System, scan_error};
use super::{MAX_CANDIDATES, MAX_DEPTH, MAX_ENTRIES, MAX_MARK, MAX_POINTER, TEMP_DIRS};
use dev_workflow_platform::r4g::fs::Stat;
use dev_workflow_platform::r4g::{Errno, OFlags};
use std::collections::{HashMap, VecDeque};

/// 走査全体の上限(秒)。
pub const TIME_LIMIT_SECONDS: f64 = 5.0;

const S_IFLNK: u32 = 0o120000;
const S_IFDIR: u32 = 0o040000;
const S_IFREG: u32 = 0o100000;

fn is_type(stat: &Stat, kind: u32) -> bool {
    stat.mode & 0o170000 == kind
}

/// 旧版の`Budget`(単調時計で、全体の5秒)。
pub struct Budget {
    started: f64,
}

/// 旧版の`Budget.now`。時計の`OSError`は`missing-feature`。
fn now(sys: &mut dyn System) -> Result<f64, Exc> {
    match sys.monotonic() {
        Err(Exc::Os(_)) => Err(scan_error(
            "missing-feature",
            "単調時計を利用できません。",
            None,
            None,
        )),
        other => other,
    }
}

impl Budget {
    pub fn new(sys: &mut dyn System) -> Result<Self, Exc> {
        Ok(Self { started: now(sys)? })
    }

    /// 旧版の`Budget.check(root_id, path)`。
    pub fn check(
        &self,
        sys: &mut dyn System,
        root_id: Option<&'static str>,
        path: Option<&[u8]>,
    ) -> Result<(), Exc> {
        if now(sys)? - self.started >= TIME_LIMIT_SECONDS {
            return Err(scan_error(
                "time-limit",
                "走査全体の5秒の上限に達しました。",
                root_id,
                path,
            ));
        }
        Ok(())
    }
}

/// `os.path.join(a, b)`(POSIX)。
pub fn join(a: &[u8], b: &[u8]) -> Vec<u8> {
    if b.starts_with(b"/") {
        return b.to_vec();
    }
    let mut out = a.to_vec();
    if !(a.is_empty() || a.ends_with(b"/")) {
        out.push(b'/');
    }
    out.extend_from_slice(b);
    out
}

/// `os.path.dirname(p)`(POSIX)。
fn dirname(p: &[u8]) -> Vec<u8> {
    let index = p
        .iter()
        .rposition(|byte| *byte == b'/')
        .map_or(0, |i| i + 1);
    let head = &p[..index];
    if !head.is_empty() && head.iter().any(|byte| *byte != b'/') {
        let end = head
            .iter()
            .rposition(|byte| *byte != b'/')
            .map_or(0, |i| i + 1);
        head[..end].to_vec()
    } else {
        head.to_vec()
    }
}

fn is_unsupported(errno: Errno) -> bool {
    errno == Errno::NOSYS || errno == Errno::NOTSUP || errno == Errno::OPNOTSUPP
}

/// 旧版の`os_failure`。
pub fn os_failure(
    errno: Errno,
    root_id: Option<&'static str>,
    path: Option<&[u8]>,
    changed: bool,
) -> Exc {
    let (code, message) = if is_unsupported(errno) {
        ("missing-feature", "必要なファイル機能を利用できません。")
    } else if errno == Errno::ACCESS || errno == Errno::PERM {
        ("unreadable", "置き場または候補の情報を読めません。")
    } else if changed {
        (
            "changed-during-scan",
            "走査中に置き場または候補が変わりました。",
        )
    } else if errno == Errno::NOTDIR {
        (
            "not-directory",
            "置き場の経路がディレクトリではありません。",
        )
    } else {
        ("resolution-failed", "置き場の実体を確定できません。")
    };
    let message = format!("{message} OSエラー番号: {}", errno.raw_os_error());
    scan_error(code, &message, root_id, path)
}

/// 旧版の`raise_if_unsupported`。未対応なら`missing-feature`、ほかは握りつぶしてよい。
fn raise_if_unsupported(errno: Errno) -> Result<(), Exc> {
    if is_unsupported(errno) {
        Err(os_failure(errno, None, None, false))
    } else {
        Ok(())
    }
}

/// 旧版の`resolve_directory`。実在するリンクを許し、元の経路の欠落は`None`、リンク先の欠落は失敗。
pub fn resolve_directory(
    sys: &mut dyn System,
    path: &[u8],
    budget: &Budget,
    root_id: Option<&'static str>,
) -> Result<Option<(Vec<u8>, Stat)>, Exc> {
    let mut pending: VecDeque<(Vec<u8>, bool)> = path
        .split(|byte| *byte == b'/')
        .map(|part| (part.to_vec(), false))
        .collect();
    let mut resolved = b"/".to_vec();
    let mut links = 0;
    let mut info: Option<Stat> = None;
    while !pending.is_empty() {
        budget.check(sys, root_id, Some(path))?;
        let (part, required) = pending.pop_front().expect("not empty");
        if part.is_empty() || part == b"." {
            continue;
        }
        if part == b".." {
            resolved = dirname(&resolved);
            info = None;
            continue;
        }
        let probe = join(&resolved, &part);
        let found = match sys.stat(None, &probe) {
            Ok(found) => found,
            Err(Exc::Os(errno)) if errno == Errno::NOENT => {
                if required {
                    return Err(os_failure(errno, root_id, Some(path), false));
                }
                return Ok(None);
            }
            Err(Exc::Os(errno)) => return Err(os_failure(errno, root_id, Some(path), false)),
            Err(other) => return Err(other),
        };
        if is_type(&found, S_IFLNK) {
            links += 1;
            if links > 40 {
                return Err(scan_error(
                    "resolution-failed",
                    "リンクの循環または過剰な連鎖があります。",
                    root_id,
                    Some(path),
                ));
            }
            let target = match sys.readlink(&probe) {
                Ok(target) => target,
                Err(Exc::Os(errno)) => return Err(os_failure(errno, root_id, Some(path), false)),
                Err(other) => return Err(other),
            };
            if target.starts_with(b"/") {
                resolved = b"/".to_vec();
            }
            for item in target.split(|byte| *byte == b'/').rev() {
                pending.push_front((item.to_vec(), true));
            }
            info = None;
            continue;
        }
        if !is_type(&found, S_IFDIR) {
            return Err(scan_error(
                "not-directory",
                "置き場の経路がディレクトリではありません。",
                root_id,
                Some(path),
            ));
        }
        resolved = probe;
        info = Some(found);
    }
    let info = match info {
        Some(info) => info,
        None => match sys.stat(None, &resolved) {
            Ok(found) => found,
            Err(Exc::Os(errno)) => return Err(os_failure(errno, root_id, Some(path), false)),
            Err(other) => return Err(other),
        },
    };
    if !is_type(&info, S_IFDIR) {
        return Err(scan_error(
            "not-directory",
            "置き場がディレクトリではありません。",
            root_id,
            Some(path),
        ));
    }
    budget.check(sys, root_id, Some(path))?;
    Ok(Some((resolved, info)))
}

/// 旧版の`candidate_kind`。
pub fn candidate_kind(mode: i128) -> &'static str {
    match mode & 0o170000 {
        0o040000 => "directory",
        0o100000 => "file",
        0o120000 => "symlink",
        0o010000 => "fifo",
        0o140000 => "socket",
        0o060000 => "block",
        0o020000 => "character",
        _ => "other",
    }
}

/// 旧版の`mark_version`。
fn mark_version(stat: &Stat) -> (i128, i128, u32, i128, i128, i128) {
    (
        stat.dev,
        stat.ino,
        stat.mode,
        stat.size,
        stat.mtime_ns,
        stat.ctime_ns,
    )
}

/// 祖先の`(dev, ino)`の並びと、根までたどれたか。
type Chain = (Vec<(i128, i128)>, bool);

/// 旧版の`mark_stat`の結果。
enum MarkStat {
    Missing,
    Failed,
    Found(Stat),
}

fn rdonly_dir_nonblock() -> OFlags {
    OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NONBLOCK | OFlags::CLOEXEC
}

/// 旧版の`Scanner`。
pub struct Scanner<'a> {
    pub doc: &'a mut Doc,
    pub sys: &'a mut dyn System,
    pub budget: &'a Budget,
    opened: Vec<(usize, Fd, StatValues)>,
    canonical: HashMap<(i128, i128), &'static str>,
    scope_done: bool,
    scope_chains: Option<Vec<Chain>>,
    temp_chains: HashMap<Vec<u8>, Option<Chain>>,
}

impl<'a> Scanner<'a> {
    pub fn new(doc: &'a mut Doc, sys: &'a mut dyn System, budget: &'a Budget) -> Self {
        Self {
            doc,
            sys,
            budget,
            opened: Vec::new(),
            canonical: HashMap::new(),
            scope_done: false,
            scope_chains: None,
            temp_chains: HashMap::new(),
        }
    }

    fn check(&mut self, root_id: Option<&'static str>, path: Option<&[u8]>) -> Result<(), Exc> {
        self.budget.check(self.sys, root_id, path)
    }

    fn input_path(&self, root: usize) -> Vec<u8> {
        self.doc.roots[root].input_path.clone().unwrap_or_default()
    }

    fn resolved_path(&self, root: usize) -> Vec<u8> {
        self.doc.roots[root]
            .resolved_path
            .clone()
            .unwrap_or_default()
    }

    /// 旧版の`changed`。
    fn changed(&self, root: usize, path: Option<&[u8]>) -> Exc {
        let input = self.input_path(root);
        scan_error(
            "changed-during-scan",
            "走査中に置き場または候補が変わりました。",
            Some(self.doc.roots[root].id),
            Some(path.unwrap_or(&input)),
        )
    }

    /// 旧版の`close_quietly`: `OSError`だけを握りつぶす。
    fn close_quietly(&mut self, fd: Fd) -> Result<(), Exc> {
        match self.sys.close(fd) {
            Ok(()) | Err(Exc::Os(_)) => Ok(()),
            Err(other) => Err(other),
        }
    }

    /// 旧版の`check_root`。
    fn check_root(&mut self, root: usize, fd: Fd, original: StatValues) -> Result<(), Exc> {
        let id = self.doc.roots[root].id;
        let path = self.input_path(root);
        self.check(Some(id), Some(&path))?;
        let current = match resolve_directory(self.sys, &path, self.budget, Some(id)) {
            Ok(current) => current,
            Err(Exc::Scan(error))
                if matches!(error.code, "resolution-failed" | "not-directory") =>
            {
                return Err(self.changed(root, None));
            }
            Err(other) => return Err(other),
        };
        let held = match self.sys.fstat(fd) {
            Ok(held) => held,
            Err(Exc::Os(errno)) => return Err(os_failure(errno, Some(id), Some(&path), true)),
            Err(other) => return Err(other),
        };
        // 置き場の直下は別のセッションで更新時刻が変わる。番号・種類・持ち主だけを比べる。
        let same = match &current {
            None => false,
            Some((resolved, info)) => {
                Some(resolved.as_slice()) == self.doc.roots[root].resolved_path.as_deref()
                    && StatValues::of(info).stable() == original.stable()
                    && StatValues::of(&held).stable() == original.stable()
            }
        };
        if !same {
            return Err(self.changed(root, None));
        }
        Ok(())
    }

    /// 旧版の`candidate_stat`。
    fn candidate_stat(&mut self, root: usize, fd: Fd, name: &[u8]) -> Result<StatValues, Exc> {
        let id = self.doc.roots[root].id;
        let path = join(&self.resolved_path(root), name);
        self.check(Some(id), Some(&path))?;
        match self.sys.stat(Some(fd), name) {
            Ok(stat) => Ok(StatValues::of(&stat)),
            Err(Exc::Os(errno)) => Err(os_failure(errno, Some(id), Some(&path), true)),
            Err(other) => Err(other),
        }
    }

    /// 旧版の`enumerate_root`。
    fn enumerate_root(&mut self, root: usize, fd: Fd) -> Result<(), Exc> {
        let id = self.doc.roots[root].id;
        let canonical_id = self.doc.roots[root].canonical_id.unwrap_or(id);
        let path = self.resolved_path(root);
        let (names, trailing) = match self.sys.scandir(fd, MAX_ENTRIES + 1) {
            Ok(found) => found,
            Err(Exc::Os(errno)) => return Err(os_failure(errno, Some(id), Some(&path), true)),
            Err(other) => return Err(other),
        };
        let mut seen: Vec<Vec<u8>> = Vec::new();
        // 旧版の count は、この名前より前に数えた件数(読んだ順の番号と同じ)。
        for (count, name) in names.into_iter().enumerate() {
            self.check(Some(id), Some(&path))?;
            if count >= MAX_ENTRIES {
                return Err(scan_error(
                    "entry-limit",
                    "置き場の4096件の上限を超えました。",
                    Some(id),
                    Some(&path),
                ));
            }
            if !name.starts_with(b"guard-") || name.len() <= b"guard-".len() {
                continue;
            }
            if seen.contains(&name) {
                return Err(self.changed(root, Some(&join(&path, &name))));
            }
            seen.push(name.clone());
            if self.doc.candidates.len() >= MAX_CANDIDATES {
                return Err(scan_error(
                    "candidate-limit",
                    "合計128候補の上限を超えました。",
                    Some(id),
                    Some(&path),
                ));
            }
            let values = self.candidate_stat(root, fd, &name)?;
            let candidate_path = join(&path, &name);
            self.doc.candidates.push(Candidate {
                root_id: canonical_id,
                path: candidate_path,
                name,
                kind: candidate_kind(values.mode),
                values,
                relation: "confirmation-required",
                reason: Some("no-mark"),
                owner: None,
            });
        }
        match trailing {
            None => Ok(()),
            Some(Exc::Os(errno)) => Err(os_failure(errno, Some(id), Some(&path), true)),
            Some(other) => Err(other),
        }
    }

    /// 旧版の`same_candidate`。
    fn same_candidate(&mut self, root: usize, fd: Fd, item: usize) -> Result<(), Exc> {
        let name = self.doc.candidates[item].name.clone();
        let values = self.candidate_stat(root, fd, &name)?;
        if values.stable() != self.doc.candidates[item].values.stable() {
            let path = self.doc.candidates[item].path.clone();
            return Err(self.changed(root, Some(&path)));
        }
        Ok(())
    }

    /// 旧版の`entry_stat`。`FileNotFoundError`だけを`None`にする。
    fn entry_stat(&mut self, dir: Fd, name: &[u8]) -> Result<Option<Stat>, Exc> {
        self.check(None, None)?;
        match self.sys.stat(Some(dir), name) {
            Ok(stat) => Ok(Some(stat)),
            Err(Exc::Os(errno)) if errno == Errno::NOENT => Ok(None),
            Err(other) => Err(other),
        }
    }

    /// 旧版の`mark_stat`。
    fn mark_stat(&mut self, root: usize, fd: Fd, name: &[u8]) -> Result<MarkStat, Exc> {
        let id = self.doc.roots[root].id;
        self.check(Some(id), None)?;
        match self.sys.stat(Some(fd), name) {
            Ok(stat) => Ok(MarkStat::Found(stat)),
            Err(Exc::Os(errno)) if errno == Errno::NOENT => Ok(MarkStat::Missing),
            Err(Exc::Os(errno)) => {
                raise_if_unsupported(errno)?;
                Ok(MarkStat::Failed)
            }
            Err(other) => Err(other),
        }
    }

    /// 旧版の`read_small`。名前で取った属性と同じ通常ファイルを、リンクを辿らず・書き手を待たずに開き、
    /// 上限付きで読む。
    fn read_small(
        &mut self,
        dir: Fd,
        name: &[u8],
        before: Option<&Stat>,
        limit: usize,
    ) -> Result<Option<Vec<u8>>, Exc> {
        let Some(before) = before else {
            return Ok(None);
        };
        if !is_type(before, S_IFREG) || before.size > limit as i128 {
            return Ok(None);
        }
        let flags = OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::NONBLOCK | OFlags::CLOEXEC;
        let fd = match self.sys.open(Some(dir), name, flags) {
            Ok(fd) => fd,
            Err(Exc::Os(errno)) => {
                raise_if_unsupported(errno)?;
                return Ok(None);
            }
            Err(other) => return Err(other),
        };
        let body = self.read_small_body(fd, before, limit);
        let outcome = match body {
            Err(Exc::Os(errno)) => raise_if_unsupported(errno).map(|()| None),
            other => other,
        };
        self.close_quietly(fd)?;
        outcome
    }

    fn read_small_body(
        &mut self,
        fd: Fd,
        before: &Stat,
        limit: usize,
    ) -> Result<Option<Vec<u8>>, Exc> {
        let held = self.sys.fstat(fd)?;
        if (held.dev, held.ino) != (before.dev, before.ino) || !is_type(&held, S_IFREG) {
            return Ok(None);
        }
        let mut data = Vec::new();
        while data.len() <= limit {
            self.check(None, None)?;
            let part = self.sys.read(fd, 8192.min(limit + 1 - data.len()))?;
            if part.is_empty() {
                break;
            }
            data.extend_from_slice(&part);
        }
        if data.len() > limit || data.len() as i128 != held.size {
            return Ok(None);
        }
        Ok(Some(data))
    }

    /// 旧版の`chain`。fdのフォルダから`..`を開いて根までたどる。fdは閉じない。
    fn chain(&mut self, fd: Fd) -> Result<Chain, Exc> {
        let mut ids = Vec::new();
        let mut current = fd;
        let body = self.chain_body(fd, &mut current, &mut ids);
        let outcome = match body {
            Err(Exc::Os(errno)) => raise_if_unsupported(errno).map(|()| (ids.clone(), false)),
            other => other,
        };
        if current != fd {
            self.close_quietly(current)?;
        }
        outcome
    }

    fn chain_body(
        &mut self,
        fd: Fd,
        current: &mut Fd,
        ids: &mut Vec<(i128, i128)>,
    ) -> Result<Chain, Exc> {
        let mut info = self.sys.fstat(fd)?;
        for _ in 0..=MAX_DEPTH {
            self.check(None, None)?;
            ids.push((info.dev, info.ino));
            let parent = self
                .sys
                .open(Some(*current), b"..", rdonly_dir_nonblock())?;
            if *current != fd {
                self.close_quietly(*current)?;
            }
            *current = parent;
            info = self.sys.fstat(*current)?;
            if Some(&(info.dev, info.ino)) == ids.last() {
                return Ok((ids.clone(), true));
            }
        }
        Ok((ids.clone(), false))
    }

    /// 旧版の`temp_chain`。無いものは`None`(比べない)、開けないものはたどれない扱い。
    fn temp_chain(&mut self, raw: &[u8]) -> Result<Option<Chain>, Exc> {
        if let Some(found) = self.temp_chains.get(raw) {
            return Ok(found.clone());
        }
        let value = match self.sys.open(None, raw, rdonly_dir_nonblock()) {
            Err(Exc::Os(errno)) if errno == Errno::NOENT || errno == Errno::NOTDIR => None,
            Err(Exc::Os(errno)) => {
                raise_if_unsupported(errno)?;
                Some((Vec::new(), false))
            }
            Err(other) => return Err(other),
            Ok(dfd) => {
                let chained = self.chain(dfd);
                self.close_quietly(dfd)?;
                Some(chained?)
            }
        };
        self.temp_chains.insert(raw.to_vec(), value.clone());
        Ok(value)
    }

    fn scope(&mut self) -> Result<Option<Vec<Chain>>, Exc> {
        if !self.scope_done {
            self.scope_done = true;
            self.scope_chains = self.resolve_scope()?;
        }
        Ok(self.scope_chains.clone())
    }

    /// 旧版の`resolve_scope`。今回の範囲(--cwd・最上位・Gitの置き場・共通の置き場)の祖先の並び。
    /// Gitを起動しない。
    fn resolve_scope(&mut self) -> Result<Option<Vec<Chain>>, Exc> {
        for name in [&b"GIT_DIR"[..], b"GIT_WORK_TREE", b"GIT_COMMON_DIR"] {
            if self
                .sys
                .environ(name)
                .is_some_and(|value| !value.is_empty())
            {
                return Ok(None);
            }
        }
        let mut fds = Vec::new();
        let body = self.resolve_scope_body(&mut fds);
        let mut outcome = match body {
            Err(Exc::Os(errno)) => raise_if_unsupported(errno).map(|()| None),
            other => other,
        };
        for dfd in fds.into_iter().rev() {
            if let Err(error) = self.close_quietly(dfd) {
                outcome = Err(error);
                break;
            }
        }
        outcome
    }

    fn resolve_scope_body(&mut self, fds: &mut Vec<Fd>) -> Result<Option<Vec<Chain>>, Exc> {
        let flags = rdonly_dir_nonblock();
        let cwd = self.doc.cwd.clone().unwrap_or_default();
        fds.push(self.sys.open(None, &cwd, flags)?);
        let mut level = fds[0];
        let mut dotgit = None;
        for _ in 0..MAX_DEPTH {
            dotgit = self.entry_stat(level, b".git")?;
            if dotgit.is_some() {
                break;
            }
            fds.push(self.sys.open(Some(level), b"..", flags)?);
            let up_fd = *fds.last().expect("pushed");
            let here = self.sys.fstat(level)?;
            let up = self.sys.fstat(up_fd)?;
            if (here.dev, here.ino) == (up.dev, up.ino) {
                return Ok(None);
            }
            level = up_fd;
        }
        let Some(dotgit) = dotgit else {
            return Ok(None);
        };
        let top_fd = level;
        if is_type(&dotgit, S_IFDIR) {
            fds.push(
                self.sys
                    .open(Some(top_fd), b".git", flags | OFlags::NOFOLLOW)?,
            );
            let held = self.sys.fstat(*fds.last().expect("pushed"))?;
            if (held.dev, held.ino) != (dotgit.dev, dotgit.ino) {
                return Ok(None);
            }
        } else if is_type(&dotgit, S_IFREG) {
            let data = self.read_small(top_fd, b".git", Some(&dotgit), MAX_POINTER)?;
            let Some(target) = pointer_target(data.as_deref(), b"gitdir: ") else {
                return Ok(None);
            };
            let target = target.to_vec();
            fds.push(self.sys.open(Some(top_fd), &target, flags)?);
        } else {
            return Ok(None);
        }
        let git_fd = *fds.last().expect("pushed");
        let head = self.entry_stat(git_fd, b"HEAD")?;
        let data = self.read_small(git_fd, b"HEAD", head.as_ref(), MAX_POINTER)?;
        if !head_is_valid(data.as_deref()) {
            return Ok(None);
        }
        let commondir = self.entry_stat(git_fd, b"commondir")?;
        let mut common_fd = git_fd;
        if let Some(commondir) = commondir {
            let data = self.read_small(git_fd, b"commondir", Some(&commondir), MAX_POINTER)?;
            let Some(target) = pointer_target(data.as_deref(), b"") else {
                return Ok(None);
            };
            let target = target.to_vec();
            fds.push(self.sys.open(Some(git_fd), &target, flags)?);
            common_fd = *fds.last().expect("pushed");
        }
        for name in [&b"objects"[..], b"refs"] {
            match self.entry_stat(common_fd, name)? {
                Some(entry) if is_type(&entry, S_IFDIR) => {}
                _ => return Ok(None),
            }
        }
        let first = fds[0];
        let mut chains = Vec::new();
        for dfd in [first, top_fd, git_fd, common_fd] {
            chains.push(self.chain(dfd)?);
        }
        Ok(Some(chains))
    }

    /// 旧版の`relate`。別プロジェクトの記録なら`None`。
    fn relate(&mut self, mark: &Mark, item: usize) -> Result<Option<&'static str>, Exc> {
        let values = self.doc.candidates[item].values;
        if mark.guard != (values.dev, values.ino) {
            return Ok(Some("guard-mismatch"));
        }
        let mut fds = Vec::new();
        let body = self.relate_body(mark, &mut fds);
        let mut outcome = body;
        for dfd in fds.into_iter().rev() {
            if let Err(error) = self.close_quietly(dfd) {
                outcome = Err(error);
                break;
            }
        }
        outcome
    }

    fn relate_body(&mut self, mark: &Mark, fds: &mut Vec<Fd>) -> Result<Option<&'static str>, Exc> {
        let flags = rdonly_dir_nonblock() | OFlags::NOFOLLOW;
        for (path, numbers) in &mark.paths {
            self.check(None, None)?;
            let info = match self.sys.open(None, path, flags) {
                Ok(fd) => {
                    fds.push(fd);
                    self.sys.fstat(fd)
                }
                Err(error) => Err(error),
            };
            let info = match info {
                Ok(info) => info,
                Err(Exc::Os(errno)) => {
                    raise_if_unsupported(errno)?;
                    return Ok(Some("owner-path-mismatch"));
                }
                Err(other) => return Err(other),
            };
            if (info.dev, info.ino) != *numbers {
                return Ok(Some("owner-path-mismatch"));
            }
        }
        let Some(mine) = self.scope()? else {
            return Ok(Some("git-unavailable"));
        };
        let mut theirs: Vec<Option<Chain>> = Vec::new();
        for dfd in fds.clone() {
            theirs.push(Some(self.chain(dfd)?));
        }
        let mut temps: Vec<Vec<u8>> = TEMP_DIRS.iter().map(|raw| raw.to_vec()).collect();
        if !mark.tmpdir.is_empty() {
            temps.push(mark.tmpdir.clone());
        }
        for raw in temps {
            theirs.push(self.temp_chain(&raw)?);
        }
        let (mut hit, mut unknown) = (false, false);
        for other in theirs.iter().flatten() {
            for (b_ids, b_complete) in &mine {
                let (a_ids, a_complete) = other;
                if a_ids.is_empty() || b_ids.is_empty() {
                    unknown = true;
                } else if b_ids.contains(&a_ids[0]) || a_ids.contains(&b_ids[0]) {
                    hit = true;
                } else if !(*a_complete && *b_complete) {
                    unknown = true;
                }
            }
        }
        if hit {
            return Ok(Some("overlap"));
        }
        Ok(if unknown {
            Some("overlap-unknown")
        } else {
            None
        })
    }

    /// 旧版の`classify`。同じ置き場の`owner-*`を名前で読み、分類する。
    fn classify(&mut self, root: usize, fd: Fd, item: usize) -> Result<(), Exc> {
        let name = self.doc.candidates[item].name.clone();
        let mut mark_name = b"owner-".to_vec();
        mark_name.extend_from_slice(&name[b"guard-".len()..]);
        self.same_candidate(root, fd, item)?;
        let before = self.mark_stat(root, fd, &mark_name)?;
        let mut reason: Option<&'static str> = Some("no-mark");
        let mut mark: Option<Mark> = None;
        match &before {
            MarkStat::Failed => reason = Some("mark-invalid"),
            MarkStat::Missing => {}
            MarkStat::Found(stat) => {
                if stat.uid != self.doc.candidates[item].values.uid {
                    reason = Some("mark-invalid");
                } else {
                    let data = self.read_small(fd, &mark_name, Some(stat), MAX_MARK)?;
                    mark = parse_mark(data.as_deref().unwrap_or(b""));
                    reason = if mark.is_none() {
                        Some("mark-invalid")
                    } else {
                        None
                    };
                }
            }
        }
        let after = self.mark_stat(root, fd, &mark_name)?;
        match (&before, &after) {
            (MarkStat::Missing, _) | (_, MarkStat::Missing) => {
                // 走査の途中で現れた・消えた
                reason = Some("no-mark");
                mark = None;
            }
            (MarkStat::Failed, _) | (_, MarkStat::Failed) => {
                reason = Some("mark-invalid");
                mark = None;
            }
            (MarkStat::Found(first), MarkStat::Found(second)) => {
                if mark_version(first) != mark_version(second) {
                    reason = Some("mark-invalid");
                    mark = None;
                }
            }
        }
        let mut owner = None;
        if let Some(mark) = &mark {
            owner = Some([
                mark.paths[0].0.clone(),
                mark.paths[1].0.clone(),
                mark.paths[2].0.clone(),
            ]);
            reason = self.relate(mark, item)?;
        }
        self.same_candidate(root, fd, item)?;
        let candidate = &mut self.doc.candidates[item];
        candidate.relation = if reason.is_none() {
            "other-project"
        } else {
            "confirmation-required"
        };
        candidate.reason = reason;
        candidate.owner = owner;
        Ok(())
    }

    /// 旧版の`scan_root`。
    fn scan_root(&mut self, root: usize) -> Result<(), Exc> {
        let id = self.doc.roots[root].id;
        let path = self.input_path(root);
        let Some((physical, info)) = resolve_directory(self.sys, &path, self.budget, Some(id))?
        else {
            self.doc.roots[root].status = "absent";
            self.doc.roots[root].reason = Some("missing");
            return Ok(());
        };
        let original = StatValues::of(&info);
        self.doc.roots[root].resolved_path = Some(physical.clone());
        let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
        let fd = match self.sys.open(None, &physical, flags) {
            Ok(fd) => fd,
            Err(Exc::Os(errno)) => return Err(os_failure(errno, Some(id), Some(&path), true)),
            Err(other) => return Err(other),
        };
        self.opened.push((root, fd, original));
        self.check_root(root, fd, original)?;
        let key = (original.dev, original.ino);
        let canonical_id = self.canonical.get(&key).copied().unwrap_or(id);
        {
            let entry = &mut self.doc.roots[root];
            entry.status = "present";
            entry.reason = None;
            entry.canonical_id = Some(canonical_id);
            entry.identity = Some([original.dev, original.ino, original.mode, original.uid]);
        }
        if let std::collections::hash_map::Entry::Vacant(slot) = self.canonical.entry(key) {
            slot.insert(canonical_id);
            self.enumerate_root(root, fd)?;
        }
        self.check_root(root, fd, original)
    }

    /// 旧版の`scan`。最後に、開いた置き場のfdを逆の順にすべて閉じる。
    pub fn scan(&mut self) -> Result<(), Exc> {
        let body = self.scan_body();
        let mut failure = None;
        for (root, fd, _) in std::mem::take(&mut self.opened).into_iter().rev() {
            match self.sys.close(fd) {
                Ok(()) => {}
                Err(Exc::Os(errno)) => {
                    let id = self.doc.roots[root].id;
                    let path = self.input_path(root);
                    failure = Some(os_failure(errno, Some(id), Some(&path), false));
                }
                Err(other) => return Err(other),
            }
        }
        match failure {
            Some(failure) => Err(failure),
            None => body,
        }
    }

    fn scan_body(&mut self) -> Result<(), Exc> {
        let inputs: [(&'static str, &[u8], &[u8]); 3] = [
            ("xdg_state", b"XDG_STATE_HOME", b"dev-workflow"),
            ("home_state", b"HOME", b".local/state/dev-workflow"),
            ("home_cache", b"HOME", b".cache/dev-workflow"),
        ];
        for (root_id, variable, suffix) in inputs {
            let value = self.sys.environ(variable);
            self.doc.roots.push(Root::skipped(root_id));
            let root = self.doc.roots.len() - 1;
            self.check(Some(root_id), None)?;
            let Some(raw) = value.filter(|value| !value.is_empty()) else {
                continue;
            };
            if !raw.starts_with(b"/") {
                self.doc.roots[root].reason = Some("relative");
                continue;
            }
            self.doc.roots[root].input_path = Some(join(&raw, suffix));
            self.scan_root(root)?;
        }
        if !self.doc.roots.iter().any(|root| root.input_path.is_some()) {
            return Err(scan_error(
                "no-roots",
                "HOMEとXDG_STATE_HOMEから置き場を得られません。",
                None,
                None,
            ));
        }
        // 他の置き場を調べている間の変化も、保持したfdと再取得で確かめる。
        for (root, fd, original) in self.opened.clone() {
            self.check_root(root, fd, original)?;
            let id = self.doc.roots[root].id;
            if self.doc.roots[root].canonical_id == Some(id) {
                for item in 0..self.doc.candidates.len() {
                    if self.doc.candidates[item].root_id == id {
                        self.classify(root, fd, item)?;
                    }
                }
            }
            self.check_root(root, fd, original)?;
        }
        for root in 0..self.doc.roots.len() {
            if self.doc.roots[root].status == "absent" {
                let id = self.doc.roots[root].id;
                let path = self.input_path(root);
                if resolve_directory(self.sys, &path, self.budget, Some(id))?.is_some() {
                    return Err(self.changed(root, None));
                }
            }
        }
        self.check(None, None)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn path_helpers_match_posixpath() {
        assert_eq!(join(b"/a", b"b"), b"/a/b");
        assert_eq!(join(b"/a/", b"b"), b"/a/b");
        assert_eq!(join(b"", b"b"), b"b");
        assert_eq!(join(b"/a", b"/b"), b"/b");
        assert_eq!(join(b"/", b".local/state"), b"/.local/state");
        assert_eq!(dirname(b"/"), b"/");
        assert_eq!(dirname(b"/a"), b"/");
        assert_eq!(dirname(b"/a/b"), b"/a");
        assert_eq!(dirname(b"//a//b"), b"//a");
        assert_eq!(dirname(b"a"), b"");
    }

    #[test]
    fn special_kinds_are_classified_without_devices() {
        assert_eq!(candidate_kind(0o060600), "block");
        assert_eq!(candidate_kind(0o020600), "character");
        assert_eq!(candidate_kind(0o600), "other");
        assert_eq!(candidate_kind(0o040700), "directory");
    }
}

#[cfg(test)]
mod kind_tests {
    use super::*;

    #[test]
    fn kinds_match_the_old_function() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../ci/r4g_fixtures/pending-function-cases.json");
        let cases: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        let kinds = cases["kinds"].as_array().unwrap();
        assert_eq!(kinds.len(), 8);
        for pair in kinds {
            let mode = i128::from(pair[0].as_i64().unwrap());
            assert_eq!(candidate_kind(mode), pair[1].as_str().unwrap(), "{mode:o}");
        }
    }
}
