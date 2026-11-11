// CPython 3.10 call-order adaptation; see CPYTHON-LICENSE in this directory.
//! CPython3.10's tree/walk call context. This counts virtual calls and their
//! temporary builtins at the point of evaluation, never the Rust stack.
use super::{
    error::{LegacyException, Result},
    json::Text,
};
use std::collections::VecDeque;

#[derive(Clone, Copy)]
pub struct Calls(pub usize);
impl Calls {
    pub fn enter(self) -> Result<Self> {
        let next = Self(self.0 + 1);
        next.check("maximum recursion depth exceeded")?;
        Ok(next)
    }
    fn check(self, reason: &'static str) -> Result<()> {
        if self.0 > 1000 {
            Err(LegacyException::Python {
                kind: "RecursionError",
                reason: reason.into(),
                location: "walk.recursion",
            })
        } else {
            Ok(())
        }
    }
    pub fn builtin(self) -> Result<()> {
        Self(self.0 + 1).check("maximum recursion depth exceeded while calling a Python object")
    }
    pub fn compare(self) -> Result<()> {
        Self(self.0 + 1).check("maximum recursion depth exceeded in comparison")
    }
    pub fn check_instance(self) -> Result<()> {
        Self(self.0 + 1).check("maximum recursion depth exceeded in __instancecheck__")
    }
    pub fn tick(self) -> Result<()> {
        self.enter()?
            .builtin()
            .map_err(|e| e.trace("tick", 59, "if time.monotonic() > self.deadline:"))
    }
    pub fn same(self) -> Result<()> {
        let same = self.enter()?;
        // tuple equality enters comparison, then compares each numeric member.
        same.compare().and_then(|_|Calls(same.0+1).compare())
            .map_err(|e|e.trace("same",91,"return (a.st_dev, a.st_ino, a.st_mode, a.st_size, a.st_mtime_ns, a.st_ctime_ns) == ("))
    }
}
#[derive(Default)]
pub struct RegexCache {
    entries: VecDeque<Vec<u32>>,
}
impl RegexCache {
    pub fn secret(&mut self, calls: Calls, rel: &Text, patterns: &[Text]) -> Result<bool> {
        let calls = calls.enter()?;
        for raw in patterns {
            calls
                .builtin()
                .map_err(|e| e.trace("secret_path", 183, r#"pattern = raw.strip("/")"#))?;
            let raw_codes = raw.as_codepoints();
            let first = raw_codes
                .iter()
                .position(|c| *c != 47)
                .unwrap_or(raw_codes.len());
            let last = raw_codes
                .iter()
                .rposition(|c| *c != 47)
                .map_or(first, |i| i + 1);
            let pattern = &raw_codes[first..last];
            if pattern.is_empty() {
                continue;
            }
            let mut regex = Vec::new();
            let mut at = 0;
            while at < pattern.len() {
                calls
                    .builtin()
                    .and_then(|_| calls.compare())
                    .map_err(|e| e.trace("secret_path", 187, "while i < len(pattern):"))?;
                calls.builtin().map_err(|e| {
                    e.trace(
                        "secret_path",
                        191,
                        r#"if pattern.startswith("**/", i): out += "(?:.*/)?"; i += 3"#,
                    )
                })?;
                let tail = &pattern[at..];
                let token = if tail.starts_with(&[42, 42, 47]) {
                    at += 3;
                    "(?:.*/)?"
                } else if tail.starts_with(&[42, 42]) {
                    at += 2;
                    ".*"
                } else if tail[0] == 42 {
                    at += 1;
                    "[^/]*"
                } else if tail[0] == 63 {
                    at += 1;
                    "[^/]"
                } else {
                    let escape = calls.enter().map_err(|e| {
                        e.trace(
                            "secret_path",
                            195,
                            "else: out += re.escape(pattern[i]); i += 1",
                        )
                    })?;
                    for (line, source) in [
                        (273, "if isinstance(pattern, str):"),
                        (274, "return pattern.translate(_special_chars_map)"),
                    ] {
                        escape.builtin().map_err(|e| {
                            e.external("re.py", "escape", line, source).trace(
                                "secret_path",
                                195,
                                "else: out += re.escape(pattern[i]); i += 1",
                            )
                        })?;
                    }
                    let c = pattern[at];
                    at += 1;
                    if b"()[]{}?*+-|^$\\.&~# \t\n\r\x0b\x0c".contains(&(c as u8)) && c < 128 {
                        regex.push(92);
                    }
                    regex.push(c);
                    continue;
                };
                regex.extend(token.chars().map(u32::from));
            }
            let prefix = if raw_codes.first() == Some(&47) {
                "^"
            } else {
                "(?:^|.*/)"
            };
            let suffix = if raw_codes.last() == Some(&47) {
                "(?:/.*)?$"
            } else {
                "$"
            };
            let mut full = prefix.chars().map(u32::from).collect::<Vec<_>>();
            full.extend(regex);
            full.extend(suffix.chars().map(u32::from));
            self.match_regex(calls, &full).map_err(|e| {
                e.trace(
                    "secret_path",
                    198,
                    "if re.match(prefix + out + suffix, rel, re.IGNORECASE):",
                )
            })?;
            if dev_workflow_core::loop_state::secret_path(rel, std::slice::from_ref(raw)) {
                return Ok(true);
            }
        }
        Ok(false)
    }
    fn match_regex(&mut self, secret: Calls, regex: &[u32]) -> Result<()> {
        let matched = secret.enter()?;
        let compiled = matched.enter().map_err(|e| {
            e.external(
                "re.py",
                "match",
                190,
                "return _compile(pattern, flags).match(string)",
            )
        })?;
        // re._compile checks isinstance(flags, RegexFlag) before its cache.
        compiled.builtin().map_err(|e| {
            e.external("re.py", "_compile", 290, "if isinstance(flags, RegexFlag):")
                .external(
                    "re.py",
                    "match",
                    190,
                    "return _compile(pattern, flags).match(string)",
                )
        })?;
        compiled.enter()?.enter().map_err(|e| {
            e.external("types.py", "__get__", 183, "return self.fget(instance)")
                .external("re.py", "_compile", 291, "flags = flags.value")
                .external(
                    "re.py",
                    "match",
                    190,
                    "return _compile(pattern, flags).match(string)",
                )
        })?;
        if !self.entries.iter().any(|entry| entry == regex) {
            super::walk_regex::miss(compiled, regex).map_err(|e| {
                e.external(
                    "re.py",
                    "match",
                    190,
                    "return _compile(pattern, flags).match(string)",
                )
            })?;
            if self.entries.len() == 512 {
                self.entries.pop_front();
            }
            self.entries.push_back(regex.to_vec());
        }
        matched.builtin().map_err(|e| {
            e.external(
                "re.py",
                "match",
                190,
                "return _compile(pattern, flags).match(string)",
            )
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn cache_and_temporary_calls_preserve_cpython_boundaries() {
        for (pattern, accepted) in [("/a", 991), ("a", 989), ("/**/x", 989), ("/?*a/", 989)] {
            let patterns = [Text::from(pattern)];
            let rel = Text::from("not-matched");
            let mut cache = RegexCache::default();
            assert!(
                !cache.secret(Calls(accepted - 1), &rel, &patterns).unwrap(),
                "REGEX_MISS_ACCEPT {pattern}"
            );
            let mut empty = RegexCache::default();
            let error = empty.secret(Calls(accepted), &rel, &patterns).unwrap_err();
            let rendered = super::super::error::render(&error);
            assert!(
                rendered
                    .ends_with("RecursionError: maximum recursion depth exceeded in comparison\n"),
                "REGEX_MISS_REJECT {pattern}: {rendered}"
            );
            assert!(empty.entries.is_empty(), "FAILED_REGEX_NOT_CACHED");
            assert!(
                !cache.secret(Calls(995), &rel, &patterns).unwrap(),
                "REGEX_HIT_ACCEPT"
            );
            let error = cache.secret(Calls(996), &rel, &patterns).unwrap_err();
            let rendered = super::super::error::render(&error);
            assert!(
                rendered.contains("in __get__\n    return self.fget(instance)\n")
                    && rendered.ends_with("RecursionError: maximum recursion depth exceeded\n"),
                "REGEX_HIT_REJECT: {rendered}"
            );
        }
        compare_old_helper_contexts();
        let mut empty = RegexCache::default();
        assert!(!empty.secret(Calls(999), &Text::from("d"), &[]).unwrap());
        assert!(empty.entries.is_empty());
        // Python's regex cache is FIFO: a hit does not move its insertion.
        let rel = Text::from("unmatched");
        let mut fifo = RegexCache::default();
        for i in 0..512 {
            fifo.secret(Calls(4), &rel, &[Text::from(format!("/p{i}").as_str())])
                .unwrap();
        }
        let oldest = fifo.entries[0].clone();
        fifo.secret(Calls(4), &rel, &[Text::from("/p0")]).unwrap();
        assert_eq!(fifo.entries[0], oldest);
        fifo.secret(Calls(4), &rel, &[Text::from("/p512")]).unwrap();
        assert_eq!(fifo.entries.len(), 512);
        assert!(!fifo.entries.contains(&oldest));
        let before = fifo.entries.clone();
        fifo.secret(Calls(4), &rel, &[Text::from("/p512"), Text::from("/p512")])
            .unwrap();
        assert_eq!(fifo.entries, before);
        let mut short = RegexCache::default();
        assert!(
            short
                .secret(
                    Calls(4),
                    &Text::from("d"),
                    &[Text::from("d"), Text::from("not-reached")]
                )
                .unwrap()
        );
        assert_eq!(short.entries.len(), 1);
        assert!(Calls(997).same().is_ok());
        assert!(
            super::super::error::render(&Calls(998).same().unwrap_err())
                .ends_with("in comparison\n")
        );
        assert!(Calls(998).tick().is_ok());
        assert!(
            super::super::error::render(&Calls(999).tick().unwrap_err())
                .contains("in tick\n    if time.monotonic() > self.deadline:\n")
        );
    }
    fn compare_old_helper_contexts() {
        use super::super::{
            error::LegacyException as E,
            json::{self, PyValue as V},
        };
        let script = r#"import importlib.util,json,linecache,pathlib,re,sys,sysconfig,yaml
assert sys.version_info[:2]==(3,10) and yaml.__version__=='6.0.3'
s=importlib.util.spec_from_file_location('old_s2l',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
stdlib=pathlib.Path(sysconfig.get_path('stdlib'));records=[]
def descend(n,pattern):
    if n:return descend(n-1,pattern)
    return m.secret_path('unmatched',[pattern])
sys.setrecursionlimit(80)
for pattern in ['/a','a','/**/x','/?*a/','/(a)','/*','/**','/?','/x/','/ſİK','/\\']:
    for hit in [False,True]:
        for depth in range(69,80):
            re.purge()
            if hit:m.secret_path('unmatched',[pattern])
            frames=[];reason='';ok=True
            try:descend(depth-3,pattern)
            except RecursionError as error:
                ok=False;reason=str(error);tb=error.__traceback__
                while tb is not None and tb.tb_frame.f_code.co_name!='secret_path':tb=tb.tb_next
                while tb is not None:
                    code=tb.tb_frame.f_code;file=pathlib.Path(code.co_filename)
                    module=''if file==pathlib.Path(sys.argv[1])else str(file.relative_to(stdlib))
                    frames.append({'module':module,'function':code.co_name,'line':tb.tb_lineno,'source':linecache.getline(str(file),tb.tb_lineno).strip()});tb=tb.tb_next
            records.append({'pattern':pattern,'hit':hit,'depth':depth,'ok':ok,'reason':reason,'frames':frames})
print(json.dumps(records))
"#;
        use dev_workflow_core::{
            budget::{Budget, BudgetLimits},
            process::{
                BrokenPipePolicy, ChildDeadlines, ChildInput, ChildStatus, OutputStream, run_child,
            },
        };
        use std::{
            sync::atomic::AtomicBool,
            time::{Duration, Instant},
        };
        let mut command = std::process::Command::new("python3");
        command
            .args([
                "-B",
                "-c",
                script,
                &super::super::error::legacy_source_path(),
            ])
            .env("PYTHONUTF8", "1")
            .env("PYTHONIOENCODING", "utf-8:surrogateescape");
        let now = Instant::now();
        let end = now + Duration::from_secs(5);
        let cleanup = end + Duration::from_secs(1);
        let mut budget = Budget::new(
            BudgetLimits {
                items: 100,
                total_bytes: 2_000_000,
                file_bytes: 2_000_000,
            },
            cleanup,
        );
        let mut stdout = Vec::new();
        let mut stderr = Vec::new();
        let result = run_child(
            &mut command,
            ChildInput {
                bytes: &[],
                broken_pipe: BrokenPipePolicy::Reject,
            },
            &mut budget,
            ChildDeadlines {
                execution: end,
                cleanup,
            },
            &AtomicBool::new(false),
            |stream, raw, budget| {
                budget.add_bytes(raw.len() as u64)?;
                if stream == OutputStream::Stdout {
                    stdout.extend_from_slice(raw);
                } else {
                    stderr.extend_from_slice(raw);
                }
                Ok::<(), dev_workflow_core::budget::BudgetError>(())
            },
        );
        let outcome = match result {
            Ok(outcome) => outcome,
            Err(mut failure) => {
                if let Some(mut child) = failure.pending_child.take() {
                    let _ = child.kill();
                    let _ = child.wait();
                }
                panic!("WALK_OLD_CONTEXT_CHILD: {failure:?}")
            }
        };
        assert_eq!(
            outcome.status,
            ChildStatus::Exited(dev_workflow_core::ExitCode::new(0)),
            "WALK_OLD_CONTEXT: {}",
            String::from_utf8_lossy(&stderr)
        );
        let observations = json::load(&stdout, 4300).unwrap();
        assert_eq!(observations.list().unwrap().len(), 242);
        for old in observations.list().unwrap() {
            let pattern = old.get("pattern").string().unwrap().clone();
            let mut cache = RegexCache::default();
            let rel = Text::from("unmatched");
            if old.get("hit").truth() {
                cache
                    .secret(Calls(4), &rel, std::slice::from_ref(&pattern))
                    .unwrap();
            }
            let depth = json::dump(old.get("depth"))
                .trim()
                .parse::<usize>()
                .unwrap();
            // The helper uses limit80. Shift both absolute entry and limit by
            // 920; the virtual calls themselves are identical to limit1000.
            let result = cache.secret(Calls(depth + 919), &rel, &[pattern]);
            assert_eq!(
                result.is_ok(),
                old.get("ok").truth(),
                "WALK_CONTEXT_ACCEPT: {}",
                json::dump(old)
            );
            if let Err(error) = result {
                let (reason, frames) = match error {
                    E::Traced { error, frames } => {
                        let E::Python { reason, .. } = *error else {
                            panic!("unexpected walk error")
                        };
                        (reason, frames)
                    }
                    E::Python { reason, .. } => (reason, vec![]),
                    other => panic!("unexpected walk error {other:?}"),
                };
                assert_eq!(
                    V::from(reason),
                    *old.get("reason"),
                    "WALK_CONTEXT_REASON: {}",
                    json::dump(old)
                );
                let frames = V::List(
                    frames
                        .into_iter()
                        .rev()
                        .map(|frame| {
                            V::object([
                                ("module", V::from(frame.module.unwrap_or(""))),
                                ("function", V::from(frame.function)),
                                ("line", V::from(i128::from(frame.line))),
                                ("source", V::from(frame.source)),
                            ])
                        })
                        .collect(),
                );
                assert_eq!(
                    frames,
                    *old.get("frames"),
                    "WALK_CONTEXT_FRAMES: {}",
                    json::dump(old)
                );
            }
        }
    }
}
