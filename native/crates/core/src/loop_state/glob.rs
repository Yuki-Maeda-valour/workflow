use super::casefold::fold;
#[derive(Clone, Copy)]
enum Token {
    Literal(u32),
    One,
    Star,
    GlobStar,
    Directories,
    Descendants,
}

/// Boolean NFA rows avoid recursive backtracking and retain Python's LF rules.
pub(super) fn matches(path: &[u32], raw: &[u32]) -> bool {
    let start = raw.iter().position(|&c| c != 47).unwrap_or(raw.len());
    let end = raw.iter().rposition(|&c| c != 47).map_or(0, |i| i + 1);
    if start >= end {
        return false;
    }
    let pattern = &raw[start..end];
    let mut tokens = Vec::new();
    if raw.first() != Some(&47) {
        tokens.push(Token::Directories);
    }
    let mut i = 0;
    while i < pattern.len() {
        if pattern[i..].starts_with(&[42, 42, 47]) {
            tokens.push(Token::Directories);
            i += 3;
        } else if pattern[i..].starts_with(&[42, 42]) {
            tokens.push(Token::GlobStar);
            i += 2;
        } else {
            tokens.push(match pattern[i] {
                42 => Token::Star,
                63 => Token::One,
                c => Token::Literal(c),
            });
            i += 1;
        }
    }
    if raw.last() == Some(&47) {
        tokens.push(Token::Descendants);
    }
    let mut row = vec![false; path.len() + 1];
    row[0] = true;
    for token in tokens {
        let mut next = vec![false; row.len()];
        let mut active = false;
        for pos in 0..=path.len() {
            match token {
                Token::Literal(c) => {
                    if pos < path.len() && row[pos] && fold(c) == fold(path[pos]) {
                        next[pos + 1] = true;
                    }
                }
                Token::One => {
                    if pos < path.len() && row[pos] && path[pos] != 47 {
                        next[pos + 1] = true;
                    }
                }
                Token::Star | Token::GlobStar => {
                    active |= row[pos];
                    next[pos] = active;
                    if pos < path.len()
                        && path[pos] == if matches!(token, Token::Star) { 47 } else { 10 }
                    {
                        active = false;
                    }
                }
                Token::Directories => {
                    next[pos] |= row[pos];
                    active |= row[pos];
                    if pos < path.len() {
                        if active && path[pos] == 47 {
                            next[pos + 1] = true;
                        }
                        if path[pos] == 10 {
                            active = false;
                        }
                    }
                }
                Token::Descendants => {
                    next[pos] |= row[pos] || active;
                    if pos < path.len() {
                        active = (active && path[pos] != 10) || (row[pos] && path[pos] == 47);
                    }
                }
            }
        }
        row = next;
    }
    row[path.len()] || (path.last() == Some(&10) && row[path.len() - 1])
}
