use super::*;
use dev_workflow_platform::diff_snapshot::{Operations, SyncPoint};
/// The product operations: `mv` is done in process with its diagnostics.
pub struct Native;
impl Operations for Native {
    fn sync(&mut self, _: SyncPoint) -> std::io::Result<()> {
        Ok(())
    }
    fn rename(&mut self, from: &Path, to: &Path) -> std::result::Result<(), Vec<u8>> {
        tools::move_path(from, to)
    }
    fn copy(&mut self, from: &Path, data: &[u8], to: &Path) -> std::result::Result<(), Vec<u8>> {
        tools::copy_bytes(from, data, to)
    }
}
/// check_out_path: the old lexical split, prefix checks, `mkdir -p`, and the
/// string `"$pp/$base"` are kept as bytes.
pub fn check_path(root: &Path, path: &[u8], patch: bool) -> Result<PathBuf> {
    let parent = bytes::dirname(path);
    let base = bytes::basename(path);
    let mut prefixes = Vec::new();
    let mut accumulated = Vec::new();
    let mut rest = parent.strip_prefix(b"/").unwrap_or(&parent).to_vec();
    while !rest.is_empty() {
        let segment = match rest.iter().position(|c| *c == b'/') {
            Some(i) => rest[..i].to_vec(),
            None => rest.clone(),
        };
        accumulated.push(b'/');
        accumulated.extend(&segment);
        prefixes.push(PathBuf::from(os(&accumulated)));
        rest = if segment == rest {
            vec![]
        } else {
            rest[segment.len() + 1..].to_vec()
        };
    }
    let mut hit = None;
    for (index, prefix) in prefixes.iter().enumerate() {
        if external_absolute(prefix, true)?.as_deref() == Some(root) {
            hit = Some(index);
            break;
        }
    }
    if let Some(hit) = hit {
        for p in &prefixes[hit + 1..] {
            if sfs::symlink(p) {
                return Err(Failure::usage_bytes(joined(&[
                    "出力先の経路に symlink がある: ".as_bytes(),
                    raw(p.as_os_str()),
                ])));
            }
        }
    }
    let target = PathBuf::from(os(path));
    if sfs::symlink(&target) {
        return Err(Failure::usage_bytes(joined(&[
            "出力先が symlink になっている: ".as_bytes(),
            path,
        ])));
    }
    if patch && target.exists() {
        return Err(Failure::usage_bytes(joined(&[
            "--patch-out の指定先が既に存在する: ".as_bytes(),
            path,
        ])));
    }
    if !patch && target.exists() && !target.is_file() {
        return Err(Failure::usage_bytes(joined(&[
            "--out が通常ファイルでない: ".as_bytes(),
            path,
        ])));
    }
    tools::mkdir_parents(&parent).map_err(|d| tool_failure(&d, 668))?;
    let parent_path = PathBuf::from(os(&parent));
    let normalized = external_absolute(&parent_path, true)?
        .filter(|p| !p.as_os_str().is_empty())
        .ok_or_else(|| {
            Failure::usage_bytes(joined(&[
                "出力先の親ディレクトリを解決できない: ".as_bytes(),
                &parent,
            ]))
        })?;
    Ok(PathBuf::from(os(&joined(&[
        raw(normalized.as_os_str()),
        b"/",
        &base,
    ]))))
}
struct Publication<'a> {
    report: Option<Temp>,
    patch: Option<Temp>,
    patch_destination: Option<&'a Path>,
}
impl Drop for Publication<'_> {
    fn drop(&mut self) {
        // cleanup_publish (242): `rm -f -- "$PATCH_ABS"` when only the patch was moved.
        if let (Some(patch), Some(report), Some(destination)) =
            (&self.patch, &self.report, self.patch_destination)
            && !patch.0.join("patch.tmp").exists()
            && report.0.join("out.tmp").exists()
        {
            cleanup_step(242, (destination, false), || {
                tools::remove_file(destination).err().unwrap_or_default()
            });
        }
    }
}
/// `cp -- "$TMPD/NAME.raw" DEST`: the raw file is held in memory by Rust.
fn copy(ops: &mut impl Operations, from: &Path, data: &[u8], to: &Path, line: usize) -> Result<()> {
    ops.copy(from, data, to).map_err(|d| tool_failure(&d, line))
}
fn moved(
    ops: &mut impl Operations,
    from: &Path,
    to: &Path,
    message: &str,
) -> std::result::Result<(), (Vec<u8>, Failure)> {
    ops.rename(from, to)
        .map_err(|diagnostic| (diagnostic, Failure::internal(message)))
}
pub fn publish(s: &Snapshot, body: &[u8], ops: &mut impl Operations) -> Result<()> {
    let raw_report = s.temp.0.join("out.raw");
    let wanted = s.patch.as_ref().filter(|_| s.want_patch);
    let raw_patch = s.temp.0.join("patch.raw");
    let outdir = bytes::dirname(raw(s.out.as_os_str()));
    let mut publication = Publication {
        report: None,
        patch: None,
        patch_destination: s.patch.as_deref(),
    };
    publication.report = Some(temp(
        Some(Path::new(&os(&joined(&[
            &outdir,
            b"/.diff-snapshot.XXXXXX",
        ])))),
        1186,
    )?);
    let report_tmp = publication.report.as_ref().unwrap().0.join("out.tmp");
    copy(ops, &raw_report, body, &report_tmp, 1187)?;
    if let Some(destination) = wanted {
        let patchdir = bytes::dirname(raw(destination.as_os_str()));
        publication.patch = Some(temp(
            Some(Path::new(&os(&joined(&[
                &patchdir,
                b"/.diff-snapshot.XXXXXX",
            ])))),
            1190,
        )?);
        let patch_tmp = publication.patch.as_ref().unwrap().0.join("patch.tmp");
        copy(ops, &raw_patch, &s.patch_bytes, &patch_tmp, 1191)?;
        ops.sync(SyncPoint::BeforePatch)
            .map_err(|_| Failure::silent(20))?;
        s.checkpoint()?;
        if let Err((stderr, failure)) = moved(
            ops,
            &patch_tmp,
            destination,
            "パッチの公開に失敗した(何も公開していない)",
        ) {
            s.checkpoint()?;
            return Err(Failure::after(&stderr, failure));
        }
        ops.sync(SyncPoint::AfterPatch)
            .map_err(|_| Failure::silent(20))?;
        s.checkpoint()?;
    }
    ops.sync(SyncPoint::BeforeReport)
        .map_err(|_| Failure::silent(20))?;
    s.checkpoint()?;
    if let Err((stderr, failure)) = moved(ops, &report_tmp, &s.out, "出力の公開に失敗した")
    {
        s.checkpoint()?;
        let mut diagnostic = stderr;
        if let Some(destination) = wanted
            && let Err(d) = tools::remove_file(destination)
        {
            diagnostic.extend(d);
        }
        return Err(Failure::after(&diagnostic, failure));
    }
    ops.sync(SyncPoint::AfterReport)
        .map_err(|_| Failure::silent(20))?;
    s.checkpoint()?;
    ops.sync(SyncPoint::BeforeCleanup)
        .map_err(|_| Failure::silent(20))?;
    s.checkpoint()?;
    Ok(())
}
