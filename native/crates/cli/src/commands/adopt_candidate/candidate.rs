use sha2::{Digest, Sha256};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct FileIdentity {
    pub(crate) dev: i128,
    pub(crate) ino: i128,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum CandidateError {
    DigestMismatch,
    Changed,
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) struct HeldCandidate {
    body: Vec<u8>,
    identity: FileIdentity,
}

impl HeldCandidate {
    pub(crate) fn capture(
        body: Vec<u8>,
        identity: FileIdentity,
        expected_sha256: &[u8],
    ) -> Result<Self, CandidateError> {
        verify_candidate_sha(&body, expected_sha256)?;
        Ok(Self { body, identity })
    }

    pub(crate) fn verify_after(
        &self,
        body: &[u8],
        identity: FileIdentity,
    ) -> Result<(), CandidateError> {
        if self.body == body && self.identity == identity {
            Ok(())
        } else {
            Err(CandidateError::Changed)
        }
    }

    #[cfg(test)]
    pub(crate) fn body(&self) -> &[u8] {
        &self.body
    }

    pub(crate) fn identity(&self) -> FileIdentity {
        self.identity
    }
}

pub(crate) fn verify_candidate_sha(
    body: &[u8],
    expected_sha256: &[u8],
) -> Result<(), CandidateError> {
    if expected_sha256.len() != 64
        || !expected_sha256
            .iter()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
    {
        return Err(CandidateError::DigestMismatch);
    }
    let actual = format!("{:x}", Sha256::digest(body));
    if actual.as_bytes() == expected_sha256 {
        Ok(())
    } else {
        Err(CandidateError::DigestMismatch)
    }
}
