//! 部品の木の観測(旧版の `COMPONENT_KINDS`・`_component_keep`・`_component_directory`・`_component_file`・
//! `_component_walk`・`component_tree`)。
//!
//! - 名前の集合(`@names:`)は、本文を読む前に控えと比べる。新設された項目の本文には一度も触れない。
//! - 同じ物理ファイル(`identity` が同じ)は、観測 1 回につき 1 度だけ読む(`EnvBudget::component_bytes`。旧版の
//!   `Budget.component_bytes` と同じ寿命)。論理名ごとの記録と照合は省かない。
//! - 導入リンクは 40 回まで、木の深さは 100 まで。
//! - 旧版の `_component_walk` の再帰は深さ 100 で止まるので、Python の再帰の上限(1000)には届かない。
//!   再帰の深さの `RecursionError` は、この関数では扱わない(`walk_at` は扱う)。
use super::budget::EnvBudget;
use super::entry;
use super::error::PyException;
use super::fsio::{
    DirIdentity, Obs, directory_identity, dup, fstat, identity, is_not_found, lstat_at,
    open_dir_at, open_root, read_at, readlink_at, scan_names, sha, site,
};
use super::inventory::Blobs;
use super::pyops::{Cps, lit};
use super::pyvalue::{PyDict, PyValue};
use super::specs::{Host, absolute};
use dev_workflow_platform::environment_guard as os;
use std::collections::{HashSet, VecDeque};
use std::os::fd::{AsFd, BorrowedFd, OwnedFd};

pub use super::state::COMPONENT_KINDS;

const S_IMODE: u32 = 0o7777;
/// 導入リンクを辿る回数の上限。
pub const LINK_LIMIT: usize = 40;
/// 木の深さの上限(`depth > 100` で止まる)。
pub const DEPTH_LIMIT: usize = 100;

fn stop(message: &str) -> PyException {
    PyException::stop(message)
}

fn prefixed(prefix: &str, key: &[u32]) -> Cps {
    let mut value = lit(prefix);
    value.extend_from_slice(key);
    value
}

/// 部品の木を辿る道具(旧版の関数に渡す `budget`・`entries`・`expected`・`blobs`)。
pub struct Walker<'a> {
    pub host: &'a dyn Host,
    pub budget: &'a mut EnvBudget,
    pub entries: &'a mut PyDict,
    pub expected: Option<&'a PyDict>,
    pub blobs: Option<&'a mut Blobs>,
}

/// `_component_directory` が保持する 1 段の確認(親の fd の番号・名前・保持した 3 項目・リンクの字面)。
struct Check {
    at: usize,
    name: Cps,
    held: Option<DirIdentity>,
    text: Option<Cps>,
}

impl Walker<'_> {
    /// `_component_keep(key, value, entries, expected)`: 保持値と違えば止め、同じなら記録する。
    pub fn keep(&mut self, key: &[u32], value: PyValue) -> Result<(), PyException> {
        if let Some(expected) = self.expected
            && entry::get(expected, key).is_none_or(|held| held.py_ne(&value))
        {
            return Err(stop("component の実体が開始時から変わった"));
        }
        entry::put(self.entries, key, value);
        Ok(())
    }

    /// `_component_directory(start_fd, parts, key, budget, entries, expected, links, optional)` と、その `with` の本体。
    /// 各要素を `lstat` → 保持値の比較 → `openat` の順に解き、開いた親を最後まで保つ。
    /// 本体が失敗したときは、後の照合をしない(旧版は `yield` の後の文が実行されない)。
    pub fn directory<T>(
        &mut self,
        start_fd: BorrowedFd<'_>,
        parts: &[Cps],
        key: &[u32],
        links: bool,
        optional: bool,
        body: impl FnOnce(&mut Self, Option<BorrowedFd<'_>>) -> Result<T, PyException>,
    ) -> Result<T, PyException> {
        let mut fds: Vec<OwnedFd> = vec![dup(start_fd)?];
        let mut checks: Vec<Check> = Vec::new();
        let mut seen: HashSet<(u64, u64)> = HashSet::new();
        let mut queue: VecDeque<Cps> = parts.iter().cloned().collect();
        let mut index = 0usize;
        let mut link_count = 0usize;
        let mut missing = false;
        let origin = directory_identity(&fstat(fds[0].as_fd())?);
        self.keep(&prefixed("@origin:", key), entry::directory_stat(&origin))?;
        while let Some(name) = queue.pop_front() {
            self.budget.tick(1)?;
            if name.is_empty() {
                continue;
            }
            let at = fds.len() - 1;
            let mut trace = prefixed("@route:", key);
            trace.push(u32::from(b':'));
            trace.extend(index.to_string().chars().map(u32::from));
            index += 1;
            let before = match lstat_at(fds[at].as_fd(), &name) {
                Ok(stat) => stat,
                Err(error) if is_not_found(&error) => {
                    // `link_count > 0` は、`optional` のときは届かない(`optional` の根は導入リンクを許さない)。
                    if !optional || link_count > 0 {
                        // `raise Stop(...) from None`
                        return Err(stop("component の参照先が存在しない")
                            .during(error)
                            .without_context());
                    }
                    self.keep(&trace, entry::missing_step(&name))?;
                    checks.push(Check {
                        at,
                        name,
                        held: None,
                        text: None,
                    });
                    missing = true;
                    break;
                }
                Err(error) => return Err(error),
            };
            site("component-after-step-lstat");
            let id = directory_identity(&before);
            let mut held = entry::directory_step(&name, &id);
            let mut text: Option<Cps> = None;
            if before.mode & os::S_IFMT == os::S_IFLNK {
                if !links {
                    return Err(stop("component の親に導入リンクがある"));
                }
                let link = readlink_at(fds[at].as_fd(), &name)?;
                held = entry::link_step(&name, &link, &id);
                text = Some(link);
            }
            self.keep(&trace, held)?;
            checks.push(Check {
                at,
                name: name.clone(),
                held: Some(id),
                text: text.clone(),
            });
            if let Some(link) = text {
                if !seen.insert((before.dev, before.ino)) || link_count >= LINK_LIMIT {
                    return Err(stop("component 導入リンクが循環しているか上限を超える"));
                }
                link_count += 1;
                // `split('/')` は `.` と `..` と空要素、その順を保つ。
                for part in link.split(|&c| c == 0x2f).rev() {
                    queue.push_front(part.to_vec());
                }
                if link.first() == Some(&0x2f) {
                    let root = open_root()?;
                    let stat = directory_identity(&fstat(root.as_fd())?);
                    fds.push(root);
                    self.keep(
                        &prefixed("@absolute:", &trace),
                        entry::directory_stat(&stat),
                    )?;
                }
                continue;
            }
            if before.mode & os::S_IFMT != os::S_IFDIR {
                return Err(stop("component の参照先が通常ディレクトリでない"));
            }
            let child = open_dir_at(fds[at].as_fd(), &name)?;
            site("component-after-open");
            fds.push(child);
            let opened = fds.len() - 1;
            if id != directory_identity(&fstat(fds[opened].as_fd())?) {
                return Err(stop("component の親が open 時に変わった"));
            }
        }
        self.keep(
            &prefixed("@route-end:", key),
            entry::route_end(missing, index),
        )?;
        site("component-after-route");
        let last = fds.len() - 1;
        let result = body(self, (!missing).then(|| fds[last].as_fd()))?;
        for check in checks.iter().rev() {
            let fd = fds[check.at].as_fd();
            let current = match lstat_at(fd, &check.name) {
                Ok(stat) => stat,
                Err(error) if is_not_found(&error) => {
                    if check.held.is_none() {
                        continue;
                    }
                    return Err(stop("component の親が検査中に消えた")
                        .during(error)
                        .without_context());
                }
                Err(error) => return Err(error),
            };
            let changed = match (&check.held, &check.text) {
                (None, _) => true,
                (Some(held), text) => {
                    directory_identity(&current) != *held
                        || match text {
                            Some(text) => &readlink_at(fd, &check.name)? != text,
                            None => false,
                        }
                }
            };
            if changed {
                return Err(stop("component の親または導入リンクが検査中に変わった"));
            }
        }
        Ok(result)
    }

    /// `_component_file(fd, name, key, ...)`。
    pub fn file(
        &mut self,
        fd: BorrowedFd<'_>,
        name: &[u32],
        key: &[u32],
    ) -> Result<(), PyException> {
        let st = lstat_at(fd, name)?;
        let id = identity(&st);
        self.keep(&prefixed("@stat:", key), entry::file_stat(&id))?;
        if st.mode & os::S_IFMT != os::S_IFREG || st.mode & 0o444 == 0 {
            return Err(stop("component が読める通常ファイルでない"));
        }
        site("component-after-file-stat");
        // 同じ物理ファイルは一度だけ読む。論理 alias ごとの記録/照合は省略しない。
        if !self.budget.component_bytes.contains_key(&id) {
            let (raw, mode) = read_at(fd, name, self.budget, Some(id))?;
            self.budget.component_bytes.insert(id, (raw, mode));
        }
        let (raw, mode) = &self.budget.component_bytes[&id];
        let (mode, digest) = (*mode, sha(raw));
        let copy = self.blobs.is_some().then(|| raw.clone());
        if identity(&lstat_at(fd, name)?) != id {
            return Err(stop("component が読取中に変わった"));
        }
        self.keep(key, entry::file(mode, &digest))?;
        if let (Some(blobs), Some(raw)) = (self.blobs.as_deref_mut(), copy) {
            blobs.insert(key.to_vec(), raw);
        }
        Ok(())
    }

    /// `_component_walk(fd, key, budget, entries, expected, blobs, installation_links, depth)`。
    pub fn walk(
        &mut self,
        fd: BorrowedFd<'_>,
        key: &[u32],
        installation_links: bool,
        depth: usize,
    ) -> Result<(), PyException> {
        self.budget.tick(1)?;
        if depth > DEPTH_LIMIT {
            return Err(stop("component の深さが上限を超える"));
        }
        let before = fstat(fd)?;
        self.keep(
            &prefixed("@stat:", key),
            entry::directory_stat(&directory_identity(&before)),
        )?;
        self.keep(key, entry::directory(before.mode & S_IMODE))?;
        let mut names = scan_names(fd, self.budget, None)?;
        names.sort();
        // 集合を本文より先に比較する。新設 entry の本文には一度も触れない。
        self.keep(&prefixed("@names:", key), entry::names(&names))?;
        site("component-after-names");
        for name in &names {
            let mut child_key = key.to_vec();
            child_key.push(0x2f);
            child_key.extend_from_slice(name);
            let st = lstat_at(fd, name)?;
            site("component-walk-after-lstat");
            if st.mode & os::S_IFMT == os::S_IFLNK {
                if !installation_links {
                    return Err(stop("component に許可されない導入リンクがある"));
                }
                let text = readlink_at(fd, name)?;
                self.keep(
                    &prefixed("@link:", &child_key),
                    entry::installation_link(&text, &directory_identity(&st)),
                )?;
                self.directory(
                    fd,
                    std::slice::from_ref(name),
                    &child_key,
                    true,
                    false,
                    |walker, child| {
                        let child = child.ok_or_else(|| stop("component の参照先が存在しない"))?;
                        walker.walk(child, &child_key, false, depth + 1)
                    },
                )?;
                site("component-link-after-walk");
                if directory_identity(&lstat_at(fd, name)?) != directory_identity(&st)
                    || readlink_at(fd, name)? != text
                {
                    return Err(stop("component の導入リンクが検査中に変わった"));
                }
            } else if st.mode & os::S_IFMT == os::S_IFDIR {
                self.directory(
                    fd,
                    std::slice::from_ref(name),
                    &child_key,
                    false,
                    false,
                    |walker, child| {
                        let child = child.ok_or_else(|| stop("component の参照先が存在しない"))?;
                        walker.walk(child, &child_key, false, depth + 1)
                    },
                )?;
            } else if st.mode & os::S_IFMT == os::S_IFREG {
                self.file(fd, name, &child_key)?;
            } else {
                return Err(stop("component に特殊ファイルがある"));
            }
        }
        if identity(&before) != identity(&fstat(fd)?) {
            return Err(stop("component の集合が検査中に変わった"));
        }
        Ok(())
    }
}

/// `component_tree(path, kind, budget, entries, expected, blobs)`。戻り値は部品の鍵。
pub fn component_tree(
    obs: &mut Obs<'_>,
    path: &[u32],
    kind: &[u32],
    expected: Option<&PyDict>,
    blobs: Option<&mut Blobs>,
) -> Result<Cps, PyException> {
    if !COMPONENT_KINDS.iter().any(|known| lit(known) == kind) {
        return Err(stop("component の種別が不明"));
    }
    let path = absolute(obs.host, path)?.to_cps();
    let mut key = lit("component:");
    key.extend_from_slice(kind);
    key.push(u32::from(b':'));
    key.extend_from_slice(&path);
    let root = open_root()?;
    // marketplace-manifest は一つの JSON だけを保持する。entry の他の plugin を含む marketplace tree 全体へ
    // 探索を広げない。
    let parts: Vec<Cps> = path
        .split(|&c| c == 0x2f)
        .skip(1)
        .map(<[u32]>::to_vec)
        .collect();
    let manifest = kind == lit("marketplace-manifest");
    let directory_parts: &[Cps] = if manifest {
        &parts[..parts.len() - 1]
    } else {
        &parts
    };
    let installation_links = kind == lit("personal-skill") || kind == lit("enterprise-skill");
    let mut walker = Walker {
        host: obs.host,
        budget: &mut *obs.budget,
        entries: &mut *obs.entries,
        expected,
        blobs,
    };
    walker.directory(
        root.as_fd(),
        directory_parts,
        &key,
        false,
        true,
        |walker, fd| {
            let Some(fd) = fd else {
                if kind == lit("plugin") {
                    return Err(stop("component の plugin root が存在しない"));
                }
                return walker.keep(&key, entry::missing());
            };
            if manifest {
                let last = &parts[parts.len() - 1];
                let parent_before = identity(&fstat(fd)?);
                match lstat_at(fd, last) {
                    Err(error) if is_not_found(&error) => {
                        walker.keep(&key, entry::missing())?;
                        site("component-manifest-after-missing");
                        match lstat_at(fd, last) {
                            Err(error) if is_not_found(&error) => {}
                            Err(error) => return Err(error),
                            Ok(_) => {
                                return Err(stop("component の不在 entry が検査中に新設された"));
                            }
                        }
                    }
                    Err(error) => return Err(error),
                    // missing -> regular は stat 比較の時点で拒否する。
                    Ok(_) => walker.file(fd, last, &key)?,
                }
                if parent_before != identity(&fstat(fd)?) {
                    return Err(stop("component の集合が検査中に変わった"));
                }
                Ok(())
            } else {
                walker.walk(fd, &key, installation_links, 0)
            }
        },
    )?;
    Ok(key)
}
