//! ビルド専用。バイト列の定義は native/README.md に置く。

use sha2::{Digest, Sha256};
use std::fs;
use std::io;
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};

// base-commit.md の共通の安全用のオプションと同じ並び。
const SAFE_GIT_PREFIX: &[&str] = &[
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
];

#[derive(Debug)]
pub struct Entry {
    pub path: String,
    pub mode: String,
}

pub struct SourceInput {
    pub root: PathBuf,
    pub index: PathBuf,
    pub entries: Vec<Entry>,
}

impl SourceInput {
    pub fn discover(manifest_dir: &Path) -> io::Result<Self> {
        let root = PathBuf::from(git_line(manifest_dir, &["rev-parse", "--show-toplevel"])?);
        if !root.is_absolute() {
            return Err(invalid("Git のリポジトリ直下が絶対パスではありません"));
        }
        let index = root.join(git_line(&root, &["rev-parse", "--git-path", "index"])?);
        let entries = parse_entries(&git(&root, &["ls-files", "-s", "-z", "--", "native"])?)?;
        Ok(Self {
            root,
            index,
            entries,
        })
    }

    pub fn sha256(&self) -> io::Result<String> {
        let mut hash = Sha256::new();
        hash.update(b"dev-workflow-source-input-v1\0");
        hash.update((self.entries.len() as u64).to_be_bytes());
        for entry in &self.entries {
            let path = self.root.join(&entry.path);
            // 親のリンクをたどってリポジトリの外の本文を読むことを防ぐ。
            let mut parent = self.root.clone();
            let components = Path::new(&entry.path).components().collect::<Vec<_>>();
            for component in &components[..components.len() - 1] {
                parent.push(component);
                if !fs::symlink_metadata(&parent)?.is_dir() {
                    return Err(invalid("追跡ファイルの親が通常のフォルダではありません"));
                }
            }
            let metadata = fs::symlink_metadata(&path)?;
            let body = match entry.mode.as_str() {
                "100644" | "100755" if metadata.is_file() => fs::read(&path)?,
                "120000" if metadata.is_symlink() => fs::read_link(&path)?
                    .as_os_str()
                    .as_encoded_bytes()
                    .to_vec(),
                _ => return Err(invalid("Git のモードと追跡ファイルの種類が一致しません")),
            };
            for field in [
                entry.path.as_bytes(),
                entry.mode.as_bytes(),
                body.as_slice(),
            ] {
                hash.update((field.len() as u64).to_be_bytes());
                hash.update(field);
            }
        }
        Ok(format!("{:x}", hash.finalize()))
    }
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn git(dir: &Path, args: &[&str]) -> io::Result<Vec<u8>> {
    let output = Command::new("git")
        .args(SAFE_GIT_PREFIX)
        .args(args)
        .current_dir(dir)
        .env("GIT_NO_LAZY_FETCH", "1")
        .env("GIT_TERMINAL_PROMPT", "0")
        .stdin(Stdio::null())
        .output()?;
    if !output.status.success() {
        return Err(io::Error::other(format!(
            "git {} が失敗しました: {}",
            args.join(" "),
            String::from_utf8_lossy(&output.stderr).trim_end()
        )));
    }
    Ok(output.stdout)
}

fn git_line(dir: &Path, args: &[&str]) -> io::Result<String> {
    let bytes = git(dir, args)?;
    let line = bytes.strip_suffix(b"\n").unwrap_or(&bytes);
    if line.is_empty() || line.iter().any(u8::is_ascii_control) {
        return Err(invalid("Git のパスが空か、制御文字を含みます"));
    }
    String::from_utf8(line.to_vec()).map_err(|_| invalid("Git のパスが UTF-8 ではありません"))
}

fn parse_entries(bytes: &[u8]) -> io::Result<Vec<Entry>> {
    let Some(records) = bytes.strip_suffix(b"\0") else {
        return Err(invalid("native/ の追跡ファイル一覧が空か、壊れています"));
    };
    let mut entries = Vec::new();
    for record in records.split(|byte| *byte == 0) {
        let tab = record
            .iter()
            .position(|byte| *byte == b'\t')
            .ok_or_else(|| invalid("Git の一覧に区切りがありません"))?;
        let header = std::str::from_utf8(&record[..tab])
            .map_err(|_| invalid("Git の一覧のヘッダが不正です"))?;
        let fields = header.split(' ').collect::<Vec<_>>();
        if fields.len() != 3
            || !matches!(fields[0], "100644" | "100755" | "120000")
            || !matches!(fields[1].len(), 40 | 64)
            || !fields[1].bytes().all(|byte| byte.is_ascii_hexdigit())
            || fields[2] != "0"
        {
            return Err(invalid("Git のモード・オブジェクト名・競合状態が不正です"));
        }
        let path = std::str::from_utf8(&record[tab + 1..])
            .map_err(|_| invalid("追跡パスが UTF-8 ではありません"))?;
        if !path.starts_with("native/")
            || path.bytes().any(|byte| byte.is_ascii_control())
            || path.split('/').any(|part| matches!(part, "" | "." | ".."))
            || Path::new(path)
                .components()
                .any(|part| !matches!(part, Component::Normal(_)))
        {
            return Err(invalid("追跡パスが native/ の相対パスではありません"));
        }
        entries.push(Entry {
            path: path.into(),
            mode: fields[0].into(),
        });
    }
    entries.sort_by(|left, right| left.path.as_bytes().cmp(right.path.as_bytes()));
    if entries.windows(2).any(|pair| pair[0].path == pair[1].path) {
        return Err(invalid("Git の一覧に同じパスが重複しています"));
    }
    Ok(entries)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static NEXT: AtomicUsize = AtomicUsize::new(0);

    struct Fixture(PathBuf);

    impl Fixture {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!(
                "dev-workflow-source-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&root).unwrap();
            fs::create_dir_all(root.join("native/crates/core")).unwrap();
            git(&root, &["init", "--quiet"]).unwrap();
            Self(root)
        }

        fn track(&self, path: &str, body: &[u8]) {
            fs::write(self.0.join(path), body).unwrap();
            git(&self.0, &["add", "--", path]).unwrap();
        }

        fn input(&self) -> SourceInput {
            SourceInput::discover(&self.0.join("native/crates/core")).unwrap()
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.0).unwrap();
        }
    }

    #[test]
    fn byte_format_matches_the_independent_vector() {
        let fixture = Fixture::new();
        fixture.track("native/a", b"hello\0\n");
        assert_eq!(
            fixture.input().sha256().unwrap(),
            "64c7696a322f08896afd9e2f6e7d824dc1c780f16282b89d40fdacfbd79c3f06"
        );
    }

    #[test]
    fn outside_native_and_untracked_files_do_not_change_the_hash() {
        let fixture = Fixture::new();
        fixture.track("native/input", b"source\0\n");
        fixture.track("outside", b"before");
        let before = fixture.input().sha256().unwrap();
        fs::write(fixture.0.join("outside"), b"after").unwrap();
        fixture.track("new-outside", b"not native");
        fs::write(fixture.0.join("native/untracked"), b"not tracked").unwrap();
        fs::create_dir(fixture.0.join("native/target")).unwrap();
        fs::write(fixture.0.join("native/target/generated"), b"output").unwrap();
        let input = fixture.input();
        assert_eq!(input.root, fixture.0);
        assert_eq!(input.index, fixture.0.join(".git/index"));
        assert_eq!(input.entries.len(), 1);
        assert_eq!(input.sha256().unwrap(), before);
        fs::write(fixture.0.join("native/input"), b"changed").unwrap();
        assert_ne!(fixture.input().sha256().unwrap(), before);
        fs::remove_file(fixture.0.join("native/input")).unwrap();
        assert!(fixture.input().sha256().is_err());
    }

    #[test]
    fn paths_and_git_modes_are_part_of_the_hash() {
        let fixture = Fixture::new();
        fixture.track("native/a", b"same");
        let before = fixture.input().sha256().unwrap();
        git(&fixture.0, &["update-index", "--chmod=+x", "native/a"]).unwrap();
        assert_ne!(fixture.input().sha256().unwrap(), before);
        git(&fixture.0, &["update-index", "--chmod=-x", "native/a"]).unwrap();
        assert_eq!(fixture.input().sha256().unwrap(), before);
        git(&fixture.0, &["rm", "--cached", "native/a"]).unwrap();
        fixture.track("native/b", b"same");
        assert_ne!(fixture.input().sha256().unwrap(), before);
    }

    #[test]
    fn empty_or_non_git_repository_is_an_error() {
        let fixture = Fixture::new();
        assert!(SourceInput::discover(&fixture.0).is_err());
        fs::remove_dir_all(fixture.0.join(".git")).unwrap();
        assert!(SourceInput::discover(&fixture.0).is_err());
    }

    #[test]
    fn malformed_paths_conflicts_and_duplicates_are_rejected() {
        let oid = "a".repeat(40);
        for record in [
            String::new(),
            format!("100644 {oid} 1\tnative/a\0"),
            format!("160000 {oid} 0\tnative/a\0"),
            format!("100644 {oid} 0\tnative/../a\0"),
            format!("100644 {oid} 0\tnative/a\nb\0"),
            format!("100644 {oid} 0\tother/a\0"),
            format!("100644 {oid} 0\tnative/a\0").repeat(2),
        ] {
            assert!(parse_entries(record.as_bytes()).is_err(), "{record:?}");
        }
        let entries = parse_entries(
            format!(
                "100644 {oid} 0\tnative/日本語\0\
             100644 {oid} 0\tnative/a b\0"
            )
            .as_bytes(),
        )
        .unwrap();
        assert_eq!(entries[0].path, "native/a b");
        assert_eq!(entries[1].path, "native/日本語");
    }

    #[cfg(unix)]
    #[test]
    fn symlink_hashes_the_link_text_without_reading_its_target() {
        use std::os::unix::fs::symlink;
        let fixture = Fixture::new();
        fixture.track("outside", b"before");
        symlink("../outside", fixture.0.join("native/link")).unwrap();
        git(&fixture.0, &["add", "--", "native/link"]).unwrap();
        let before = fixture.input().sha256().unwrap();
        fs::write(fixture.0.join("outside"), b"after").unwrap();
        assert_eq!(fixture.input().sha256().unwrap(), before);
        fs::remove_file(fixture.0.join("native/link")).unwrap();
        symlink("../missing", fixture.0.join("native/link")).unwrap();
        assert_ne!(fixture.input().sha256().unwrap(), before);
    }
}
