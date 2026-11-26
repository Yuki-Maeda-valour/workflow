//! 利用者設定などの 1 ファイルの記録(旧版の `record`)。
//!
//! - 保持値(`expected`)があれば、保持していない鍵・不在として保持した所の新設・リンクの字面や対象の差し替えを、
//!   新しい本文を読む前に止める。
//! - 最終のリンクは 40 段まで辿り、各段を `@file-link:<段>:<鍵>` に記録する。リンク先の本文は `required` で読む。
//! - `FileNotFoundError` は、まだ何も開いていない(`lstat` が済んでいない)ときだけ「不在」として記録する。
use super::budget::EnvBudget;
use super::entry;
use super::error::PyException;
use super::fsio::{
    absolute, directory_identity, identity, is_not_found, lstat_at, read_at, readlink_at, sha,
    site, with_parent,
};
use super::pyops::{lit, slice_to};
use super::pypath::PyPath;
use super::pyvalue::{PyDict, PyValue};
use super::specs::Host;
use dev_workflow_platform::environment_guard as os;

/// リンクを辿る段数の上限(`depth > 40` で止まる)。
pub const LINK_DEPTH_LIMIT: usize = 40;

fn stop(message: &str) -> PyException {
    PyException::stop(message)
}

/// `record(path, key, budget, entries, expected, depth, required, expected_identity)` の、省略できる引数。
#[derive(Default)]
pub struct RecordOptions<'a> {
    pub expected: Option<&'a PyDict>,
    pub depth: usize,
    pub required: bool,
    pub expected_identity: Option<super::fsio::Identity>,
}

/// 本文を返す。不在は `None`(`entries[key] = ['missing']`)。
pub fn record(
    host: &dyn Host,
    path: &[u32],
    key: &[u32],
    budget: &mut EnvBudget,
    entries: &mut PyDict,
    options: &RecordOptions<'_>,
) -> Result<Option<Vec<u8>>, PyException> {
    budget.tick(1)?;
    if options.depth > LINK_DEPTH_LIMIT {
        return Err(stop("利用者設定のリンクが循環しているか上限を超える"));
    }
    let expected = options.expected;
    // 保持していないパスを設定の参照から新しく信頼しない。
    if let Some(expected) = expected
        && !entry::contains(expected, key)
    {
        return Err(stop("保持していない利用者設定の参照先が現れた"));
    }
    let mut opened = false;
    let result = with_parent(host, path, |fd, name| {
        let before = lstat_at(fd, name)?;
        opened = true;
        site("record-after-lstat");
        if let Some(expected) = expected
            && entry::get(expected, key).is_some_and(|held| {
                held.py_eq(&PyValue::List(vec![super::pyops::text_of("missing")]))
            })
        {
            return Err(stop("不在として保持した利用者設定が新設された"));
        }
        if let Some(wanted) = options.expected_identity
            && identity(&before) != wanted
        {
            return Err(stop("本文読取前にリンク対象が変わった"));
        }
        if before.mode & os::S_IFMT == os::S_IFLNK {
            let text = readlink_at(fd, name)?;
            let as_path = PyPath::new(&text);
            let target = if as_path.is_absolute() {
                as_path
            } else {
                absolute(host, path)?.parent().join(&text)
            };
            let target_text = target.to_cps();
            let mut link_key = lit("@file-link:");
            link_key.extend(options.depth.to_string().chars().map(u32::from));
            link_key.push(u32::from(b':'));
            link_key.extend_from_slice(key);
            let mut held = entry::link_head("configuration-link", &text, before.dev, before.ino);
            if let Some(expected) = expected {
                let current = match entry::get(expected, &link_key) {
                    Some(value) => slice_to(value, 4)?,
                    None => PyValue::List(Vec::new()),
                };
                if current.py_ne(&PyValue::List(held.clone())) {
                    return Err(stop("利用者設定のリンクが開始時から変わった"));
                }
            }
            let target_stat = with_parent(host, &target_text, |target_fd, target_name| {
                lstat_at(target_fd, target_name)
            })?;
            held.extend(entry::directory_values(&directory_identity(&target_stat)));
            if let Some(expected) = expected
                && entry::get(expected, &link_key)
                    .is_none_or(|value| value.py_ne(&PyValue::List(held.clone())))
            {
                return Err(stop("利用者設定のリンク対象が開始時から変わった"));
            }
            entry::put(entries, &link_key, PyValue::List(held));
            site("record-before-link-target");
            let raw = record(
                host,
                &target_text,
                key,
                budget,
                entries,
                &RecordOptions {
                    expected,
                    depth: options.depth + 1,
                    required: true,
                    expected_identity: Some(identity(&target_stat)),
                },
            )?;
            site("record-after-link-read");
            with_parent(host, &target_text, |target_fd, target_name| {
                if directory_identity(&target_stat)
                    != directory_identity(&lstat_at(target_fd, target_name)?)
                {
                    return Err(stop("利用者設定のリンク対象が読取中に変わった"));
                }
                Ok(())
            })?;
            if identity(&before) != identity(&lstat_at(fd, name)?) {
                return Err(stop("利用者設定のリンクが読取中に変わった"));
            }
            return Ok(Recorded::Link(raw));
        }
        let (raw, mode) = read_at(fd, name, budget, Some(identity(&before)))?;
        Ok(Recorded::File(raw, mode))
    });
    match result {
        Ok(Recorded::Link(raw)) => Ok(raw),
        Ok(Recorded::File(raw, mode)) => {
            entry::put(entries, key, entry::file(mode, &sha(&raw)));
            Ok(Some(raw))
        }
        Err(error) if is_not_found(&error) && !(opened || options.required) => {
            entry::put(entries, key, entry::missing());
            Ok(None)
        }
        Err(error) => Err(error),
    }
}

enum Recorded {
    /// 最終のリンクを辿った結果(`entries[key]` は、辿った先の `record` が書いた)。
    Link(Option<Vec<u8>>),
    File(Vec<u8>, u32),
}
