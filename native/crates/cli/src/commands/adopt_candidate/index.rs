#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum FileMode {
    Regular,
    Executable,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct IndexEntry {
    mode: FileMode,
    oid: Vec<u8>,
    path: Vec<u8>,
}

impl IndexEntry {
    #[cfg(test)]
    pub(crate) fn mode(&self) -> FileMode {
        self.mode
    }

    #[cfg(test)]
    pub(crate) fn oid(&self) -> &[u8] {
        &self.oid
    }

    #[cfg(test)]
    pub(crate) fn path(&self) -> &[u8] {
        &self.path
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum IndexError {
    Unavailable,
    NotUnique,
    NotRegularStageZero,
    Changed,
}

pub(crate) fn parse_stage_zero(
    command_succeeded: bool,
    stdout: &[u8],
    expected_path: &[u8],
) -> Result<IndexEntry, IndexError> {
    if !command_succeeded {
        return Err(IndexError::Unavailable);
    }
    let mut records = stdout.split(|byte| *byte == 0);
    let record = records.next().expect("split always returns one item");
    if records.next() != Some(b"".as_slice()) || records.next().is_some() {
        return Err(IndexError::Unavailable);
    }

    let mut pair = record.splitn(2, |byte| *byte == b'\t');
    let fields = pair.next().expect("splitn always returns one item");
    let Some(path) = pair.next() else {
        return Err(IndexError::NotUnique);
    };
    let values = fields.split(|byte| *byte == b' ').collect::<Vec<_>>();
    let [mode, oid, stage] = values.as_slice() else {
        return Err(IndexError::NotUnique);
    };

    let mode = match *mode {
        b"100644" => FileMode::Regular,
        b"100755" => FileMode::Executable,
        _ => return Err(IndexError::NotRegularStageZero),
    };
    if *stage != b"0"
        || path != expected_path
        || !(40..=64).contains(&oid.len())
        || !oid
            .iter()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
    {
        return Err(IndexError::NotRegularStageZero);
    }
    Ok(IndexEntry {
        mode,
        oid: oid.to_vec(),
        path: path.to_vec(),
    })
}

pub(crate) fn verify_index_unchanged(
    held: &IndexEntry,
    current: &IndexEntry,
) -> Result<(), IndexError> {
    if held.mode == current.mode && held.oid == current.oid {
        Ok(())
    } else {
        Err(IndexError::Changed)
    }
}
