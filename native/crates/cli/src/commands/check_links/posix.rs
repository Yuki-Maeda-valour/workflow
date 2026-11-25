//! 本家 3.10.11 の posixpath の isabs・join・split・normpath(文字列だけの部分)。
//! 引数は surrogateescape で符号化したバイト列。区切りは ASCII なので文字列と同じ結果になる。

/// `posixpath.isabs`。
pub fn isabs(path: &[u8]) -> bool {
    path.first() == Some(&b'/')
}

/// `posixpath.join(a, *rest)`。`/` で始まる部品があれば、それより前を捨てる。
pub fn join(first: &[u8], rest: &[&[u8]]) -> Vec<u8> {
    let mut path = first.to_vec();
    for part in rest {
        if part.first() == Some(&b'/') {
            path = part.to_vec();
        } else if path.is_empty() || path.last() == Some(&b'/') {
            path.extend_from_slice(part);
        } else {
            path.push(b'/');
            path.extend_from_slice(part);
        }
    }
    path
}

/// `posixpath.split`。頭の末尾の `/` は、頭が `/` だけでなければ削る。
pub fn split(path: &[u8]) -> (Vec<u8>, Vec<u8>) {
    let at = path
        .iter()
        .rposition(|&byte| byte == b'/')
        .map_or(0, |index| index + 1);
    let (head, tail) = path.split_at(at);
    let head = if head.iter().any(|&byte| byte != b'/') {
        let end = head
            .iter()
            .rposition(|&byte| byte != b'/')
            .map_or(0, |index| index + 1);
        &head[..end]
    } else {
        head
    };
    (head.to_vec(), tail.to_vec())
}

/// `posixpath.normpath`。先頭の `/` はちょうど 2 個なら保ち、字面の `..` を畳む。
pub fn normpath(path: &[u8]) -> Vec<u8> {
    if path.is_empty() {
        return b".".to_vec();
    }
    let initial = if !path.starts_with(b"/") {
        0
    } else if path.starts_with(b"//") && !path.starts_with(b"///") {
        2
    } else {
        1
    };
    let mut components: Vec<&[u8]> = Vec::new();
    for component in path.split(|&byte| byte == b'/') {
        if component.is_empty() || component == b"." {
            continue;
        }
        if component != b".."
            || (initial == 0 && components.is_empty())
            || components.last() == Some(&&b".."[..])
        {
            components.push(component);
        } else {
            components.pop();
        }
    }
    let mut result = vec![b'/'; initial];
    result.extend(components.join(&b'/'));
    if result.is_empty() {
        result.push(b'.');
    }
    result
}
