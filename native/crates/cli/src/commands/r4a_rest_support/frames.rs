//! 旧版の標準ライブラリのフレーム(CPython 3.10.11 の `argparse.py`・`json/`)。
//!
//! ファイル名は導入先の置き場からの相対パス(設計の承認差)。行番号とソースの行は
//! 本家 3.10.11 のソースから取った値(比較器が本家のファイルの SHA-256 を確かめる)。
use super::exception::Frame;

pub const ARGPARSE_PARSE_ARGS_HELP: Frame = Frame {
    file: "argparse.py",
    line: 1833,
    func: "parse_args",
    source: Some("args, argv = self.parse_known_args(args, namespace)"),
};
pub const ARGPARSE_PARSE_ARGS_EXTRA: Frame = Frame {
    file: "argparse.py",
    line: 1836,
    func: "parse_args",
    source: Some("self.error(msg % ' '.join(argv))"),
};
pub const ARGPARSE_PARSE_KNOWN: Frame = Frame {
    file: "argparse.py",
    line: 1866,
    func: "parse_known_args",
    source: Some("namespace, args = self._parse_known_args(args, namespace)"),
};
pub const ARGPARSE_PARSE_KNOWN_ERROR: Frame = Frame {
    file: "argparse.py",
    line: 1869,
    func: "parse_known_args",
    source: Some("self.error(str(err))"),
};
pub const ARGPARSE_CLASSIFY: Frame = Frame {
    file: "argparse.py",
    line: 1910,
    func: "_parse_known_args",
    source: Some("option_tuple = self._parse_optional(arg_string)"),
};
pub const ARGPARSE_CONSUME_OPTIONAL_CALL: Frame = Frame {
    file: "argparse.py",
    line: 2079,
    func: "_parse_known_args",
    source: Some("start_index = consume_optional(start_index)"),
};
pub const ARGPARSE_TAKE_ACTION_CALL: Frame = Frame {
    file: "argparse.py",
    line: 2019,
    func: "consume_optional",
    source: Some("take_action(action, args, option_string)"),
};
pub const ARGPARSE_ACTION_CALL: Frame = Frame {
    file: "argparse.py",
    line: 1943,
    func: "take_action",
    source: Some("action(self, namespace, argument_values, option_string)"),
};
pub const ARGPARSE_HELP_ACTION: Frame = Frame {
    file: "argparse.py",
    line: 1106,
    func: "__call__",
    source: Some("parser.print_help()"),
};
pub const ARGPARSE_PRINT_HELP: Frame = Frame {
    file: "argparse.py",
    line: 2567,
    func: "print_help",
    source: Some("self._print_message(self.format_help(), file)"),
};
pub const ARGPARSE_PRINT_USAGE: Frame = Frame {
    file: "argparse.py",
    line: 2562,
    func: "print_usage",
    source: Some("self._print_message(self.format_usage(), file)"),
};
pub const ARGPARSE_PARSE_OPTIONAL_AMBIGUOUS: Frame = Frame {
    file: "argparse.py",
    line: 2230,
    func: "_parse_optional",
    source: Some("self.error(msg % args)"),
};
pub const ARGPARSE_ERROR_USAGE: Frame = Frame {
    file: "argparse.py",
    line: 2592,
    func: "error",
    source: Some("self.print_usage(_sys.stderr)"),
};
pub const ARGPARSE_ERROR_EXIT: Frame = Frame {
    file: "argparse.py",
    line: 2594,
    func: "error",
    source: Some("self.exit(2, _('%(prog)s: error: %(message)s\\n') % args)"),
};
pub const ARGPARSE_EXIT: Frame = Frame {
    file: "argparse.py",
    line: 2580,
    func: "exit",
    source: Some("self._print_message(message, _sys.stderr)"),
};
pub const ARGPARSE_PRINT_MESSAGE: Frame = Frame {
    file: "argparse.py",
    line: 2573,
    func: "_print_message",
    source: Some("file.write(message)"),
};
pub const JSON_LOADS_NOT_STR: Frame = Frame {
    file: "json/__init__.py",
    line: 339,
    func: "loads",
    source: Some("raise TypeError(f'the JSON object must be str, bytes or bytearray, '"),
};
pub const JSON_LOADS_BOM: Frame = Frame {
    file: "json/__init__.py",
    line: 335,
    func: "loads",
    source: Some("raise JSONDecodeError(\"Unexpected UTF-8 BOM (decode using utf-8-sig)\","),
};
pub const JSON_LOADS_DECODE: Frame = Frame {
    file: "json/__init__.py",
    line: 346,
    func: "loads",
    source: Some("return _default_decoder.decode(s)"),
};
pub const JSON_DECODE_RAW: Frame = Frame {
    file: "json/decoder.py",
    line: 337,
    func: "decode",
    source: Some("obj, end = self.raw_decode(s, idx=_w(s, 0).end())"),
};
pub const JSON_DECODE_EXTRA: Frame = Frame {
    file: "json/decoder.py",
    line: 340,
    func: "decode",
    source: Some("raise JSONDecodeError(\"Extra data\", s, end)"),
};
pub const JSON_RAW_SCAN: Frame = Frame {
    file: "json/decoder.py",
    line: 353,
    func: "raw_decode",
    source: Some("obj, end = self.scan_once(s, idx)"),
};
pub const JSON_RAW_EXPECTING: Frame = Frame {
    file: "json/decoder.py",
    line: 355,
    func: "raw_decode",
    source: Some("raise JSONDecodeError(\"Expecting value\", s, err.value) from None"),
};
pub const PATHLIB_NEW: Frame = Frame {
    file: "pathlib.py",
    line: 960,
    func: "__new__",
    source: Some("self = cls._from_parts(args)"),
};
pub const PATHLIB_FROM_PARTS: Frame = Frame {
    file: "pathlib.py",
    line: 594,
    func: "_from_parts",
    source: Some("drv, root, parts = self._parse_args(args)"),
};
pub const PATHLIB_PARSE_ARGS: Frame = Frame {
    file: "pathlib.py",
    line: 578,
    func: "_parse_args",
    source: Some("a = os.fspath(a)"),
};
pub const PATHLIB_EXISTS: Frame = Frame {
    file: "pathlib.py",
    line: 1290,
    func: "exists",
    source: Some("self.stat()"),
};
pub const PATHLIB_STAT: Frame = Frame {
    file: "pathlib.py",
    line: 1097,
    func: "stat",
    source: Some("return self._accessor.stat(self, follow_symlinks=follow_symlinks)"),
};
pub const ARGPARSE_CONSUME_MATCH: Frame = Frame {
    file: "argparse.py",
    line: 2009,
    func: "consume_optional",
    source: Some("arg_count = match_argument(action, selected_patterns)"),
};
pub const ARGPARSE_MATCH_RAISE: Frame = Frame {
    file: "argparse.py",
    line: 2174,
    func: "_match_argument",
    source: Some("raise ArgumentError(action, msg)"),
};
pub const ARGPARSE_RAISE_BUNDLED: Frame = Frame {
    file: "argparse.py",
    line: 1987,
    func: "consume_optional",
    source: Some("raise ArgumentError(action, msg % explicit_arg)"),
};
pub const ARGPARSE_RAISE_LONG: Frame = Frame {
    file: "argparse.py",
    line: 2001,
    func: "consume_optional",
    source: Some("raise ArgumentError(action, msg % explicit_arg)"),
};
pub const PATHLIB_IS_DIR: Frame = Frame {
    file: "pathlib.py",
    line: 1305,
    func: "is_dir",
    source: Some("return S_ISDIR(self.stat().st_mode)"),
};
pub const PATHLIB_IS_SYMLINK: Frame = Frame {
    file: "pathlib.py",
    line: 1358,
    func: "is_symlink",
    source: Some("return S_ISLNK(self.lstat().st_mode)"),
};
pub const PATHLIB_LSTAT: Frame = Frame {
    file: "pathlib.py",
    line: 1222,
    func: "lstat",
    source: Some("return self.stat(follow_symlinks=False)"),
};
