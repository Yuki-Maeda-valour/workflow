//! ZIP の書き出し(旧 `write_xlsx` の 196 行 `wb.save(checked_out)`)。
//!
//! 旧版は openpyxl 3.1.5 の `save_workbook` で、CPython 3.10.11 の `zipfile.ZipFile`
//! (`'w'`・`ZIP_DEFLATED`・`allowZip64=True`)へ部分を順に入れる。Rust は次のとおりにする
//! (設計「ブックと XLSX」と、利用者の決定 Q1〜Q4)。
//!
//! - 部分の順と欄は旧版と同じ: 版 20・作成元 3(Unix)・旗 0・方式 8(DEFLATE)・追加の欄と注記は空・
//!   隙間なく並べる。外部属性はふつうの部分が `0o600 << 16`、シートの部分が旧版の一時ファイルの値
//!   `0o100600 << 16`。
//! - 中身は圧縮しない。DEFLATE の非圧縮ブロック(65535 バイトずつ)で書く(Q3)。
//! - 一時ファイルを使わず、メモリ上の部分をそのまま書く。シートの時刻も書いた時刻にする(Q4)。
//! - 時刻は書いた時刻の現地時刻(`localtime_r`)を 2 秒単位で切り捨てる。
//! - ZIP64: 大きさと部分の数だけで決まる規則([`part_needs_zip64`]・[`count_needs_zip64`])は
//!   zipfile と同じ。位置と圧縮後の大きさで決まる欄は、Rust の大きさと位置に zipfile と同じ規則を
//!   当てて書く(旧版との一致は Q2 で比較の前提の外)。
//!
//! 書き込みの失敗(Q1)は、旧版の溜め(`BufferedRandom`)と zipfile の書き方に合わせて扱う。
//! - 各部分のローカルヘッダと中身は溜めに入れ、部分を閉じる所(`_ZipWriteFile.close` の `seek`。
//!   1180 行)で書き出す。書けなかった分は溜めに残る。
//! - 終わりの記録は `ZipFile.close` の `_write_end_record` の `flush`(1937 行)で書き出す。
//!   失敗したら、`finally` の `_fpclose` が溜めをもう 1 度書き出す。その失敗は前の失敗を
//!   `__context__` に持つ。
//! - 例外がモジュールの外へ出たときは、呼出履歴を消すときに `ZipFile.__del__` が閉じる
//!   ([`ZipFile::finalize`])。その失敗は `Exception ignored in: <function ZipFile.__del__ at 0x…>`。
//! - 例外は、失敗した所に当たる旧版の関数の並び([`frames`])で出す。最初の書き出し(`app.xml` を
//!   閉じる所)で失敗した場合は、旧版と同じ行になる。
//!
//! 出典と許諾: 部分の名前・順・欄の値は openpyxl 3.1.5(MIT ライセンス。Copyright (c) 2010 openpyxl)
//! の出力に合わせた。許諾の全文は同じディレクトリの `NOTICE.md`。

use super::errors::{self, ExcType, Filename, Frame, PyException, types};
use super::paths::PyPath;
use super::pyio::{PyStr, Runtime};
use super::workbook::{CoreTimes, Part, Workbook};
use dev_workflow_platform::r4e::{self, CivilTime};
use std::ffi::OsStr;
use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::OpenOptionsExt;

/// `zipfile.ZIP64_LIMIT`(`(1 << 31) - 1`)。
pub const ZIP64_LIMIT: u64 = (1 << 31) - 1;
/// `zipfile.ZIP_FILECOUNT_LIMIT`(`(1 << 16) - 1`)。
pub const ZIP_FILECOUNT_LIMIT: u64 = (1 << 16) - 1;
/// `zipfile.ZIP64_VERSION`。
const ZIP64_VERSION: u8 = 45;
/// `zipfile.DEFAULT_VERSION`(作成元の版と、展開に要る版の初めの値)。
const DEFAULT_VERSION: u8 = 20;
/// `ZipInfo.create_system`(Windows 以外は 3)。
const CREATE_SYSTEM: u8 = 3;
/// `zipfile.ZIP_DEFLATED`。
const ZIP_DEFLATED: u16 = 8;
/// `writestr` の外部属性(`0o600 << 16`)。
const PART_ATTRIBUTES: u32 = 0o600 << 16;
/// シートの外部属性(旧版の一時ファイル `0o100600` の `st_mode << 16`)。
const SHEET_ATTRIBUTES: u32 = 0o100600 << 16;
/// DEFLATE の非圧縮ブロックの中身の上限。
const STORED_BLOCK: usize = 65535;

/// `struct.error`(`struct.pack` の値が型に入らない)。
pub const STRUCT_ERROR: ExcType = ExcType {
    module: "struct",
    name: "error",
    bases: &["Exception", "BaseException"],
};

// ---------------------------------------------------------------- ZIP64 の規則(大きさと数だけ)

/// `ZipFile._open_to_write` の `zip64`(`allowZip64=True`・`force_zip64=False`):
/// `zinfo.file_size * 1.05 > ZIP64_LIMIT`。Python と同じく、大きさを倍精度の浮動小数点へ丸めてから
/// 1.05 を掛け、`ZIP64_LIMIT`(倍精度でちょうど表せる)と比べる。
pub fn part_needs_zip64(file_size: u64) -> bool {
    file_size as f64 * 1.05 > ZIP64_LIMIT as f64
}

/// `ZipFile._write_end_record` の部分の数の判定(`centDirCount > ZIP_FILECOUNT_LIMIT`)。
pub fn count_needs_zip64(count: u64) -> bool {
    count > ZIP_FILECOUNT_LIMIT
}

// ---------------------------------------------------------------- CRC-32 と DEFLATE

const CRC_TABLE: [u32; 256] = {
    let mut table = [0u32; 256];
    let mut index = 0;
    while index < 256 {
        let mut value = index as u32;
        let mut bit = 0;
        while bit < 8 {
            value = if value & 1 != 0 {
                0xEDB8_8320 ^ (value >> 1)
            } else {
                value >> 1
            };
            bit += 1;
        }
        table[index] = value;
        index += 1;
    }
    table
};

/// CRC-32(`zlib.crc32` と同じ値)。
pub fn crc32(data: &[u8]) -> u32 {
    let mut crc = !0u32;
    for &byte in data {
        crc = CRC_TABLE[((crc ^ u32::from(byte)) & 0xff) as usize] ^ (crc >> 8);
    }
    !crc
}

/// DEFLATE の非圧縮ブロックの並びの大きさ(空の中身も最後のブロックを 1 つ持つ)。
pub fn stored_len(size: usize) -> usize {
    size + 5 * size.div_ceil(STORED_BLOCK).max(1)
}

/// `data` を DEFLATE の非圧縮ブロック(65535 バイトずつ。最後のブロックに BFINAL)で `out` へ足す。
pub fn push_stored(out: &mut Vec<u8>, data: &[u8]) {
    let mut blocks = data.chunks(STORED_BLOCK).peekable();
    if blocks.peek().is_none() {
        out.extend_from_slice(&[1, 0, 0, 0xff, 0xff]);
        return;
    }
    while let Some(block) = blocks.next() {
        let length = block.len() as u16;
        out.push(u8::from(blocks.peek().is_none()));
        out.extend_from_slice(&length.to_le_bytes());
        out.extend_from_slice(&(!length).to_le_bytes());
        out.extend_from_slice(block);
    }
}

// ---------------------------------------------------------------- 時刻

/// 時刻を求める部品(製品は [`SystemClock`]。試験は決めた値)。
pub trait Clock {
    /// `time.time()` を秒へ切り捨てた値(UNIX 時刻)。
    fn now(&self) -> i64;
    /// `time.localtime(seconds)`。
    fn local(&self, seconds: i64) -> io::Result<CivilTime>;
}

/// 製品の時刻(OS の時計と `localtime_r`)。
pub struct SystemClock;

impl Clock for SystemClock {
    fn now(&self) -> i64 {
        unix_seconds(std::time::SystemTime::now())
    }

    fn local(&self, seconds: i64) -> io::Result<CivilTime> {
        r4e::local_time(seconds)
    }
}

/// UNIX 時刻の秒(1970 年より前も、負の方向へ切り捨てる)。
fn unix_seconds(now: std::time::SystemTime) -> i64 {
    match now.duration_since(std::time::UNIX_EPOCH) {
        Ok(elapsed) => elapsed.as_secs() as i64,
        Err(before) => {
            let before = before.duration();
            -(before.as_secs() as i64) - i64::from(before.subsec_nanos() > 0)
        }
    }
}

/// DOS の日付と時刻(`dosdate`・`dostime`)。`dosdate` が 16 ビットに入らない(2108 年以降)なら
/// `None`(旧版は `FileHeader` の `struct.pack` で `struct.error`)。
fn dos_fields(when: &CivilTime) -> (Option<u16>, u16) {
    let date = ((when.year - 1980) << 9) | (i64::from(when.month) << 5) | i64::from(when.day);
    let time =
        (u16::from(when.hour) << 11) | (u16::from(when.minute) << 5) | u16::from(when.second / 2);
    (u16::try_from(date).ok(), time)
}

// ---------------------------------------------------------------- 部分の記録

/// 中央ディレクトリに載せる 1 つの部分(`ZipInfo`)。
#[derive(Clone, Debug, PartialEq, Eq)]
struct Entry {
    name: Vec<u8>,
    flag_bits: u16,
    date: u16,
    time: u16,
    crc: u32,
    compress_size: u64,
    file_size: u64,
    header_offset: u64,
    external_attr: u32,
    extract_version: u8,
    create_version: u8,
}

impl Entry {
    /// `ZipInfo.FileHeader(zip64)`(大きさと CRC は書き終えた後の値)。展開に要る版と作成元の版を
    /// 上げることがある(旧版も `self` を書き換える)。
    fn local_header(&mut self, zip64: bool) -> Vec<u8> {
        let mut extra = Vec::new();
        let (mut compress_size, mut file_size) = (self.compress_size, self.file_size);
        let mut min_version = 0;
        if zip64 {
            extra.extend_from_slice(&1u16.to_le_bytes());
            extra.extend_from_slice(&16u16.to_le_bytes());
            extra.extend_from_slice(&file_size.to_le_bytes());
            extra.extend_from_slice(&compress_size.to_le_bytes());
        }
        if file_size > ZIP64_LIMIT || compress_size > ZIP64_LIMIT {
            // zip64 でなければ大きさは 1.05 倍の判定で ZIP64_LIMIT を超えない(非圧縮ブロックの
            // 増分を足しても超えない)ので、旧版の `LargeZipFile`・`RuntimeError` には当たらない。
            debug_assert!(zip64);
            file_size = 0xffff_ffff;
            compress_size = 0xffff_ffff;
            min_version = ZIP64_VERSION;
        }
        self.extract_version = self.extract_version.max(min_version);
        self.create_version = self.create_version.max(min_version);
        let mut header = Vec::with_capacity(30 + self.name.len() + extra.len());
        header.extend_from_slice(b"PK\x03\x04");
        header.push(self.extract_version);
        header.push(0);
        header.extend_from_slice(&self.flag_bits.to_le_bytes());
        header.extend_from_slice(&ZIP_DEFLATED.to_le_bytes());
        header.extend_from_slice(&self.time.to_le_bytes());
        header.extend_from_slice(&self.date.to_le_bytes());
        header.extend_from_slice(&self.crc.to_le_bytes());
        header.extend_from_slice(&(compress_size as u32).to_le_bytes());
        header.extend_from_slice(&(file_size as u32).to_le_bytes());
        header.extend_from_slice(&(self.name.len() as u16).to_le_bytes());
        header.extend_from_slice(&(extra.len() as u16).to_le_bytes());
        header.extend_from_slice(&self.name);
        header.extend_from_slice(&extra);
        header
    }

    /// `_write_end_record` の中央ディレクトリの 1 項目。
    fn central_record(&self) -> Vec<u8> {
        let mut values = Vec::new();
        let (mut compress_size, mut file_size) = (self.compress_size, self.file_size);
        if file_size > ZIP64_LIMIT || compress_size > ZIP64_LIMIT {
            values.push(file_size);
            values.push(compress_size);
            file_size = 0xffff_ffff;
            compress_size = 0xffff_ffff;
        }
        let mut header_offset = self.header_offset;
        if header_offset > ZIP64_LIMIT {
            values.push(header_offset);
            header_offset = 0xffff_ffff;
        }
        let mut extra = Vec::new();
        let mut min_version = 0;
        if !values.is_empty() {
            extra.extend_from_slice(&1u16.to_le_bytes());
            extra.extend_from_slice(&(8 * values.len() as u16).to_le_bytes());
            for value in &values {
                extra.extend_from_slice(&value.to_le_bytes());
            }
            min_version = ZIP64_VERSION;
        }
        let mut record = Vec::with_capacity(46 + self.name.len() + extra.len());
        record.extend_from_slice(b"PK\x01\x02");
        record.push(self.create_version.max(min_version));
        record.push(CREATE_SYSTEM);
        record.push(self.extract_version.max(min_version));
        record.push(0);
        record.extend_from_slice(&self.flag_bits.to_le_bytes());
        record.extend_from_slice(&ZIP_DEFLATED.to_le_bytes());
        record.extend_from_slice(&self.time.to_le_bytes());
        record.extend_from_slice(&self.date.to_le_bytes());
        record.extend_from_slice(&self.crc.to_le_bytes());
        record.extend_from_slice(&(compress_size as u32).to_le_bytes());
        record.extend_from_slice(&(file_size as u32).to_le_bytes());
        record.extend_from_slice(&(self.name.len() as u16).to_le_bytes());
        record.extend_from_slice(&(extra.len() as u16).to_le_bytes());
        record.extend_from_slice(&0u16.to_le_bytes());
        record.extend_from_slice(&0u16.to_le_bytes());
        record.extend_from_slice(&0u16.to_le_bytes());
        record.extend_from_slice(&self.external_attr.to_le_bytes());
        record.extend_from_slice(&(header_offset as u32).to_le_bytes());
        record.extend_from_slice(&self.name);
        record.extend_from_slice(&extra);
        record
    }
}

/// 中央ディレクトリと、(要れば ZIP64 の終わりの記録と位置の記録と)終わりの記録。
/// `start_dir` は中央ディレクトリの位置。
fn end_records(filelist: &[Entry], start_dir: u64) -> Vec<u8> {
    let mut out = Vec::new();
    for entry in filelist {
        out.extend_from_slice(&entry.central_record());
    }
    let mut count = filelist.len() as u64;
    let mut size = out.len() as u64;
    let mut offset = start_dir;
    let pos2 = start_dir + size;
    if count_needs_zip64(count) || offset > ZIP64_LIMIT || size > ZIP64_LIMIT {
        out.extend_from_slice(b"PK\x06\x06");
        out.extend_from_slice(&44u64.to_le_bytes());
        out.extend_from_slice(&45u16.to_le_bytes());
        out.extend_from_slice(&45u16.to_le_bytes());
        out.extend_from_slice(&0u32.to_le_bytes());
        out.extend_from_slice(&0u32.to_le_bytes());
        out.extend_from_slice(&count.to_le_bytes());
        out.extend_from_slice(&count.to_le_bytes());
        out.extend_from_slice(&size.to_le_bytes());
        out.extend_from_slice(&offset.to_le_bytes());
        out.extend_from_slice(b"PK\x06\x07");
        out.extend_from_slice(&0u32.to_le_bytes());
        out.extend_from_slice(&pos2.to_le_bytes());
        out.extend_from_slice(&1u32.to_le_bytes());
        count = count.min(0xffff);
        size = size.min(0xffff_ffff);
        offset = offset.min(0xffff_ffff);
    }
    out.extend_from_slice(b"PK\x05\x06");
    out.extend_from_slice(&0u16.to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes());
    out.extend_from_slice(&(count as u16).to_le_bytes());
    out.extend_from_slice(&(count as u16).to_le_bytes());
    out.extend_from_slice(&(size as u32).to_le_bytes());
    out.extend_from_slice(&(offset as u32).to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes());
    out
}

// ---------------------------------------------------------------- 書き込み先

/// 書き込み先(`write` を 1 回と、閉じる)。製品は開いたファイル、試験は結果を決めた偽物。
pub trait Sink: Sized {
    /// 1 回だけ書く。途中までしか書けなければ、書けたバイト数を返す。
    fn write_once(&mut self, data: &[u8]) -> io::Result<usize>;
    /// 閉じる(`FileIO.close` の `close(2)`)。誤りは errno つきで返す(捨てない)。
    fn close(self) -> io::Result<()>;
}

impl Sink for File {
    fn write_once(&mut self, data: &[u8]) -> io::Result<usize> {
        Write::write(self, data)
    }

    fn close(self) -> io::Result<()> {
        r4e::close_checked(self)
    }
}

/// `io.open(file, 'w+b')` → だめなら `io.open(file, 'wb')`(`ZipFile.__init__` の 1249〜1257 行)。
/// どちらもモード `0o666`(umask を除く)で作り、既存のファイルは同じ実体を切り詰める。両方とも
/// 失敗したら、後の誤り(ファイル名つきの `OSError`)。
pub fn create(dest: &PyPath) -> Result<File, PyException> {
    let path = dest.to_bytes();
    let open = |read: bool| {
        OpenOptions::new()
            .read(read)
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o666)
            .open(OsStr::from_bytes(&path))
    };
    open(true).or_else(|_| open(false)).map_err(|error| {
        PyException::os_with_filename(
            error.raw_os_error().unwrap_or(errors::EIO),
            Filename::Str(PyStr::from_os_bytes(&path)),
        )
    })
}

// ---------------------------------------------------------------- ZipFile

/// 部分を入れる呼出しの種類。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Kind {
    /// `ZipFile.writestr`(シートのほかの部分)。
    Writestr,
    /// `ZipFile.write`(旧版は一時ファイルから入れるシート)。
    Write,
}

impl Kind {
    /// 時刻を現地時刻へ分ける所。
    fn localtime(self) -> &'static [Frame] {
        match self {
            Self::Writestr => &[frames::WRITESTR_LOCALTIME],
            Self::Write => &[frames::WRITE_FROM_FILE, frames::FROM_FILE_LOCALTIME],
        }
    }

    /// `ZipInfo(…)` の年の確かめ(1980 年より前)。
    fn zipinfo(self) -> &'static [Frame] {
        match self {
            Self::Writestr => &[frames::WRITESTR_ZIPINFO, frames::ZIPINFO_INIT_RAISES],
            Self::Write => &[
                frames::WRITE_FROM_FILE,
                frames::FROM_FILE_ZIPINFO,
                frames::ZIPINFO_INIT_RAISES,
            ],
        }
    }

    /// `_open_to_write` の最初のローカルヘッダ(`struct.pack`)。
    fn file_header(self) -> &'static [Frame] {
        match self {
            Self::Writestr => &[
                frames::WRITESTR_OPENS,
                frames::OPEN_TO_WRITE,
                frames::OPEN_TO_WRITE_HEADER,
                frames::FILE_HEADER_PACKS,
            ],
            Self::Write => &[
                frames::WRITE_OPENS,
                frames::OPEN_TO_WRITE,
                frames::OPEN_TO_WRITE_HEADER,
                frames::FILE_HEADER_PACKS,
            ],
        }
    }

    /// 部分を閉じて書き出す所(`_ZipWriteFile.close` の `seek`)。
    fn close(self) -> &'static [Frame] {
        match self {
            Self::Writestr => &[frames::WRITESTR_OPENS, frames::WRITE_FILE_CLOSE_SEEKS],
            Self::Write => &[frames::WRITE_OPENS, frames::WRITE_FILE_CLOSE_SEEKS],
        }
    }
}

/// 書き込み用の `zipfile.ZipFile` と、その `BufferedRandom` の書けていない分。
pub struct ZipFile<S: Sink> {
    /// `self.fp`(閉じたら `None`)。
    fp: Option<S>,
    /// 書き終えた部分(`self.filelist`)。
    filelist: Vec<Entry>,
    /// 次の部分か中央ディレクトリの位置(`self.start_dir`)。
    start_dir: u64,
    /// 溜めの書けていない分。次の書き出しで、ファイルの今の位置から書く。
    pending: Vec<u8>,
}

impl<S: Sink> ZipFile<S> {
    /// 開いた書き込み先から作る(`ZipFile.__init__` の 1267 行から後。位置は 0)。
    pub fn new(fp: S) -> Self {
        Self {
            fp: Some(fp),
            filelist: Vec::new(),
            start_dir: 0,
            pending: Vec::new(),
        }
    }

    /// 溜めの書けていない分を書き出す(`_bufferedwriter_flush_unlocked`)。1 回の `write` ごとに信号を
    /// 確かめる。`EINTR` は信号を確かめてから書き直す(`_Py_write`)。ほかの誤りは `OSError` で、
    /// 書けなかった分は溜めに残る。
    fn flush(&mut self, py: &mut Runtime) -> Result<(), PyException> {
        let Self { fp, pending, .. } = self;
        let Some(fp) = fp.as_mut() else {
            return Ok(());
        };
        while !pending.is_empty() {
            match fp.write_once(pending) {
                Ok(written) => {
                    pending.drain(..written.min(pending.len()));
                    py.check_signals()?;
                }
                Err(error) if error.kind() == io::ErrorKind::Interrupted => py.check_signals()?,
                Err(error) => {
                    return Err(PyException::os(error.raw_os_error().unwrap_or(errors::EIO)));
                }
            }
        }
        Ok(())
    }

    /// 1 つの部分を入れる(シートのほかは `writestr`、シートは `write`)。`when` は書いた時刻を
    /// 現地時刻へ分けた結果。失敗は、部分を入れる呼出し(`writestr`・`write`)より内側の段つき。
    fn write_part(
        &mut self,
        py: &mut Runtime,
        part: &Part,
        when: io::Result<CivilTime>,
    ) -> Result<(), PyException> {
        let kind = if part.worksheet {
            Kind::Write
        } else {
            Kind::Writestr
        };
        let when = when.map_err(|error| {
            PyException::os(error.raw_os_error().unwrap_or(errors::EIO))
                .called_from(kind.localtime())
        })?;
        if when.year < 1980 {
            return Err(PyException::new(
                types::VALUE_ERROR,
                "ZIP does not support timestamps before 1980",
            )
            .called_from(kind.zipinfo()));
        }
        let (date, time) = dos_fields(&when);
        let Some(date) = date else {
            return Err(PyException::new(
                STRUCT_ERROR,
                "ushort format requires 0 <= number <= (32767 *2 +1)",
            )
            .called_from(kind.file_header()));
        };
        let name = part.name.as_bytes().to_vec();
        let file_size = part.data.len() as u64;
        let mut entry = Entry {
            flag_bits: if name.is_ascii() { 0 } else { 0x800 },
            name,
            date,
            time,
            crc: crc32(&part.data),
            compress_size: stored_len(part.data.len()) as u64,
            file_size,
            header_offset: self.start_dir,
            external_attr: if part.worksheet {
                SHEET_ATTRIBUTES
            } else {
                PART_ATTRIBUTES
            },
            extract_version: DEFAULT_VERSION,
            create_version: DEFAULT_VERSION,
        };
        let header = entry.local_header(part_needs_zip64(file_size));
        self.pending
            .reserve(header.len() + entry.compress_size as usize);
        self.pending.extend_from_slice(&header);
        push_stored(&mut self.pending, &part.data);
        self.start_dir = entry.header_offset + header.len() as u64 + entry.compress_size;
        self.flush(py)
            .map_err(|error| error.called_from(kind.close()))?;
        self.filelist.push(entry);
        Ok(())
    }

    /// `ZipFile.close()`。失敗は `close` の段からの呼出履歴(呼出元が外側の段を足す)。
    ///
    /// `try:` で溜めを書き出し(`seek(self.start_dir)`)、終わりの記録を書いて `flush` する。
    /// `finally:` で閉じる。`_fpclose` → `fp.close()`(`buffered_close`)は、溜めをもう 1 度書き出し、
    /// その成否にかかわらず `FileIO.close` で `close(2)` をする。例外は次のとおり(旧版の実測)。
    /// - `try:` の中の例外を処理している間に起きた例外は、それを `__context__` に持つ。
    /// - `close(2)` が失敗したら、その `OSError` が `fp.close()` の行から出る。直前の書き出しも
    ///   失敗していれば、その例外(呼出履歴は無い)を `__context__` に持つ
    ///   (`_PyErr_ChainExceptions`)。
    /// - `close(2)` が成功したら、書き出しの失敗が `fp.close()` の行から出る。
    pub fn close(&mut self, py: &mut Runtime) -> Result<(), PyException> {
        if self.fp.is_none() {
            return Ok(());
        }
        let first = self
            .flush(py)
            .map_err(|error| error.called_from(&[frames::CLOSE_SEEKS]))
            .and_then(|()| {
                let records = end_records(&self.filelist, self.start_dir);
                self.pending.extend_from_slice(&records);
                self.flush(py).map_err(|error| {
                    error
                        .called_from(&[frames::CLOSE_WRITES_END_RECORD, frames::END_RECORD_FLUSHES])
                })
            });
        let flushed = self.flush(py);
        let closed = self.fp.take().map_or(Ok(()), Sink::close);
        self.pending = Vec::new();
        let handled = |error: PyException| match &first {
            Err(previous) => error.with_context(previous.clone()),
            Ok(()) => error,
        };
        let second = match closed {
            Ok(()) => flushed.map_err(handled),
            Err(error) => {
                let error = py.os_error(error.raw_os_error().unwrap_or(errors::EIO));
                Err(match flushed {
                    Err(flush_error) => error.with_context(handled(flush_error)),
                    Ok(()) => handled(error),
                })
            }
        };
        match second {
            Ok(()) => first,
            Err(second) => {
                Err(second.called_from(&[frames::CLOSE_FPCLOSE, frames::FPCLOSE_CLOSES]))
            }
        }
    }

    /// `ZipFile.__del__`(例外の呼出履歴を消すときに動く): 閉じていなければ閉じる。失敗は
    /// `Exception ignored in: <function ZipFile.__del__ at 0x…>` の形で標準エラーへ書く(番地は
    /// 実行ごとに変わる値。形式だけを比べる)。
    pub fn finalize(mut self, py: &mut Runtime) {
        if self.fp.is_none() {
            return;
        }
        if let Err(error) = self.close(py) {
            let error = error.called_from(&[frames::ZIPFILE_DEL]);
            py.write_unraisable(&del_repr(), &error);
        }
    }
}

/// `repr(ZipFile.__del__)`(番地は、この関数の場所)。
fn del_repr() -> PyStr {
    let address = del_repr as fn() -> PyStr as usize;
    PyStr::from(format!("<function ZipFile.__del__ at {address:#x}>"))
}

// ---------------------------------------------------------------- wb.save

/// `wb.save(checked_out)` が例外で終わったとき。
pub struct SaveError<S: Sink> {
    /// 例外(`<module>` からの呼出履歴つき)。
    pub exception: PyException,
    /// まだ閉じていない ZIP。例外の呼出履歴が持ち、消すときに `ZipFile.__del__` が閉じる
    /// ([`ZipFile::finalize`])。開けなかったときと、閉じた後は `None`。
    pub archive: Option<ZipFile<S>>,
}

/// `<module>` から `save_workbook` までの段(`wb.save(checked_out)` の中)。
const SAVE: &[Frame] = &[
    errors::frames::MODULE,
    errors::frames::MAIN_CALLS_WRITE_XLSX,
    frames::WRITE_XLSX_SAVES,
    frames::WORKBOOK_SAVE,
];

/// `ExcelWriter.write_data` までの段。
const WRITE_DATA: &[Frame] = &[
    errors::frames::MODULE,
    errors::frames::MAIN_CALLS_WRITE_XLSX,
    frames::WRITE_XLSX_SAVES,
    frames::WORKBOOK_SAVE,
    frames::SAVE_WORKBOOK_SAVES,
    frames::EXCEL_SAVE_WRITES_DATA,
];

/// `write_data` の中で、その部分を入れる呼出しの段。
fn part_frames(name: &str) -> &'static [Frame] {
    match name {
        "docProps/app.xml" => &[frames::WRITE_DATA_APP],
        "docProps/core.xml" => &[frames::WRITE_DATA_CORE],
        "xl/theme/theme1.xml" => &[frames::WRITE_DATA_THEME],
        "xl/styles.xml" => &[frames::WRITE_DATA_STYLES],
        "_rels/.rels" => &[frames::WRITE_DATA_ROOT_RELS],
        "xl/workbook.xml" => &[frames::WRITE_DATA_WORKBOOK],
        "xl/_rels/workbook.xml.rels" => &[frames::WRITE_DATA_WORKBOOK_RELS],
        "[Content_Types].xml" => &[frames::WRITE_DATA_MANIFEST, frames::MANIFEST_WRITE],
        _ => &[
            frames::WRITE_DATA_WORKSHEETS,
            frames::WRITE_WORKSHEETS,
            frames::WRITE_WORKSHEET,
        ],
    }
}

/// 旧版の `wb.save(checked_out)`: `ZipFile(…)` で開き、更新の時刻を取り、部分を旧版の順で入れて
/// 閉じる。`created` は `Workbook()` の時刻(UTC の秒)、`open` は出力先を開く処理。
pub fn save<S: Sink>(
    py: &mut Runtime,
    book: &Workbook,
    created: i64,
    open: impl FnOnce() -> Result<S, PyException>,
    clock: &dyn Clock,
) -> Result<(), SaveError<S>> {
    let failed =
        |exception: PyException, archive: Option<ZipFile<S>>| SaveError { exception, archive };
    let opening = [SAVE, &[frames::SAVE_WORKBOOK_OPENS]].concat();
    if let Err(error) = py.check_signals() {
        return Err(failed(error.called_from(&opening), None));
    }
    let mut archive = match open() {
        Ok(fp) => ZipFile::new(fp),
        Err(error) => {
            let error = error.called_from(&[frames::ZIPFILE_INIT_OPENS]);
            return Err(failed(error.called_from(&opening), None));
        }
    };
    let modified = clock.now();
    let parts = book.parts(CoreTimes { created, modified });
    for part in &parts {
        let stack = [WRITE_DATA, part_frames(&part.name)].concat();
        if let Err(error) = py.check_signals() {
            return Err(failed(error.called_from(&stack), Some(archive)));
        }
        let when = clock.local(clock.now());
        if let Err(error) = archive.write_part(py, part, when) {
            return Err(failed(error.called_from(&stack), Some(archive)));
        }
    }
    let closing = [
        SAVE,
        &[frames::SAVE_WORKBOOK_SAVES, frames::EXCEL_SAVE_CLOSES],
    ]
    .concat();
    if let Err(error) = py.check_signals() {
        return Err(failed(error.called_from(&closing), Some(archive)));
    }
    archive
        .close(py)
        .map_err(|error| failed(error.called_from(&closing), None))
}

// ---------------------------------------------------------------- 呼出履歴の段

/// 旧スクリプト・openpyxl 3.1.5・`zipfile.py`(本家 CPython 3.10.11)の段。
pub mod frames {
    use super::super::errors::{Frame, frames::SCRIPT};

    const EXCEL: &str = "openpyxl/writer/excel.py";
    const ZIPFILE: &str = "zipfile.py";

    pub const WRITE_XLSX_SAVES: Frame =
        Frame::new(SCRIPT, 196, "write_xlsx", "    wb.save(checked_out)");
    pub const MAIN_PRINTS_ERROR: Frame = Frame::new(
        SCRIPT,
        235,
        "main",
        "        print(f\"ERROR 出力を拒否: {exc}\", file=sys.stderr)",
    );
    pub const WORKBOOK_SAVE: Frame = Frame::new(
        "openpyxl/workbook/workbook.py",
        386,
        "save",
        "        save_workbook(self, filename)",
    );
    pub const SAVE_WORKBOOK_OPENS: Frame = Frame::new(
        EXCEL,
        291,
        "save_workbook",
        "    archive = ZipFile(filename, 'w', ZIP_DEFLATED, allowZip64=True)",
    );
    pub const SAVE_WORKBOOK_SAVES: Frame =
        Frame::new(EXCEL, 294, "save_workbook", "    writer.save()");
    pub const EXCEL_SAVE_WRITES_DATA: Frame =
        Frame::new(EXCEL, 275, "save", "        self.write_data()");
    pub const EXCEL_SAVE_CLOSES: Frame =
        Frame::new(EXCEL, 276, "save", "        self._archive.close()");
    pub const WRITE_DATA_APP: Frame = Frame::new(
        EXCEL,
        60,
        "write_data",
        "        archive.writestr(ARC_APP, tostring(props.to_tree()))",
    );
    pub const WRITE_DATA_CORE: Frame = Frame::new(
        EXCEL,
        62,
        "write_data",
        "        archive.writestr(ARC_CORE, tostring(self.workbook.properties.to_tree()))",
    );
    pub const WRITE_DATA_THEME: Frame = Frame::new(
        EXCEL,
        66,
        "write_data",
        "            archive.writestr(ARC_THEME, theme_xml)",
    );
    pub const WRITE_DATA_WORKSHEETS: Frame =
        Frame::new(EXCEL, 77, "write_data", "        self._write_worksheets()");
    pub const WRITE_WORKSHEETS: Frame = Frame::new(
        EXCEL,
        215,
        "_write_worksheets",
        "            self.write_worksheet(ws)",
    );
    pub const WRITE_WORKSHEET: Frame = Frame::new(
        EXCEL,
        203,
        "write_worksheet",
        "        self._archive.write(writer.out, ws.path[1:])",
    );
    pub const WRITE_DATA_STYLES: Frame = Frame::new(
        EXCEL,
        85,
        "write_data",
        "        archive.writestr(ARC_STYLE, tostring(stylesheet))",
    );
    pub const WRITE_DATA_ROOT_RELS: Frame = Frame::new(
        EXCEL,
        88,
        "write_data",
        "        archive.writestr(ARC_ROOT_RELS, writer.write_root_rels())",
    );
    pub const WRITE_DATA_WORKBOOK: Frame = Frame::new(
        EXCEL,
        89,
        "write_data",
        "        archive.writestr(ARC_WORKBOOK, writer.write())",
    );
    pub const WRITE_DATA_WORKBOOK_RELS: Frame = Frame::new(
        EXCEL,
        90,
        "write_data",
        "        archive.writestr(ARC_WORKBOOK_RELS, writer.write_rels())",
    );
    pub const WRITE_DATA_MANIFEST: Frame = Frame::new(
        EXCEL,
        94,
        "write_data",
        "        self.manifest._write(archive, self.workbook)",
    );
    pub const MANIFEST_WRITE: Frame = Frame::new(
        "openpyxl/packaging/manifest.py",
        166,
        "_write",
        "        archive.writestr(self.path, tostring(self.to_tree()))",
    );
    pub const ZIPFILE_INIT_OPENS: Frame = Frame::new(
        ZIPFILE,
        1251,
        "__init__",
        "                    self.fp = io.open(file, filemode)",
    );
    pub const ZIPINFO_INIT_RAISES: Frame = Frame::new(
        ZIPFILE,
        364,
        "__init__",
        "            raise ValueError('ZIP does not support timestamps before 1980')",
    );
    pub const FILE_HEADER_PACKS: Frame = Frame::new(
        ZIPFILE,
        450,
        "FileHeader",
        "        header = struct.pack(structFileHeader, stringFileHeader,",
    );
    pub const FROM_FILE_LOCALTIME: Frame = Frame::new(
        ZIPFILE,
        506,
        "from_file",
        "        mtime = time.localtime(st.st_mtime)",
    );
    pub const FROM_FILE_ZIPINFO: Frame = Frame::new(
        ZIPFILE,
        520,
        "from_file",
        "        zinfo = cls(arcname, date_time)",
    );
    pub const WRITE_FILE_CLOSE_SEEKS: Frame = Frame::new(
        ZIPFILE,
        1180,
        "close",
        "                self._fileobj.seek(self._zinfo.header_offset)",
    );
    pub const OPEN_TO_WRITE: Frame = Frame::new(
        ZIPFILE,
        1519,
        "open",
        "            return self._open_to_write(zinfo, force_zip64=force_zip64)",
    );
    pub const OPEN_TO_WRITE_HEADER: Frame = Frame::new(
        ZIPFILE,
        1614,
        "_open_to_write",
        "        self.fp.write(zinfo.FileHeader(zip64))",
    );
    pub const WRITE_FROM_FILE: Frame = Frame::new(
        ZIPFILE,
        1741,
        "write",
        "        zinfo = ZipInfo.from_file(filename, arcname,",
    );
    pub const WRITE_OPENS: Frame = Frame::new(
        ZIPFILE,
        1775,
        "write",
        "            with open(filename, \"rb\") as src, self.open(zinfo, 'w') as dest:",
    );
    pub const WRITESTR_ZIPINFO: Frame = Frame::new(
        ZIPFILE,
        1788,
        "writestr",
        "            zinfo = ZipInfo(filename=zinfo_or_arcname,",
    );
    pub const WRITESTR_LOCALTIME: Frame = Frame::new(
        ZIPFILE,
        1789,
        "writestr",
        "                            date_time=time.localtime(time.time())[:6])",
    );
    pub const WRITESTR_OPENS: Frame = Frame::new(
        ZIPFILE,
        1816,
        "writestr",
        "            with self.open(zinfo, mode='w') as dest:",
    );
    pub const ZIPFILE_DEL: Frame = Frame::new(ZIPFILE, 1821, "__del__", "        self.close()");
    pub const CLOSE_SEEKS: Frame = Frame::new(
        ZIPFILE,
        1838,
        "close",
        "                        self.fp.seek(self.start_dir)",
    );
    pub const CLOSE_WRITES_END_RECORD: Frame = Frame::new(
        ZIPFILE,
        1839,
        "close",
        "                    self._write_end_record()",
    );
    pub const CLOSE_FPCLOSE: Frame =
        Frame::new(ZIPFILE, 1843, "close", "            self._fpclose(fp)");
    pub const END_RECORD_FLUSHES: Frame = Frame::new(
        ZIPFILE,
        1937,
        "_write_end_record",
        "        self.fp.flush()",
    );
    pub const FPCLOSE_CLOSES: Frame =
        Frame::new(ZIPFILE, 1943, "_fpclose", "            fp.close()");

    /// 表のすべての段(実物と照らす対象)。
    pub const ALL: &[Frame] = &[
        WRITE_XLSX_SAVES,
        MAIN_PRINTS_ERROR,
        WORKBOOK_SAVE,
        SAVE_WORKBOOK_OPENS,
        SAVE_WORKBOOK_SAVES,
        EXCEL_SAVE_WRITES_DATA,
        EXCEL_SAVE_CLOSES,
        WRITE_DATA_APP,
        WRITE_DATA_CORE,
        WRITE_DATA_THEME,
        WRITE_DATA_WORKSHEETS,
        WRITE_WORKSHEETS,
        WRITE_WORKSHEET,
        WRITE_DATA_STYLES,
        WRITE_DATA_ROOT_RELS,
        WRITE_DATA_WORKBOOK,
        WRITE_DATA_WORKBOOK_RELS,
        WRITE_DATA_MANIFEST,
        MANIFEST_WRITE,
        ZIPFILE_INIT_OPENS,
        ZIPINFO_INIT_RAISES,
        FILE_HEADER_PACKS,
        FROM_FILE_LOCALTIME,
        FROM_FILE_ZIPINFO,
        WRITE_FILE_CLOSE_SEEKS,
        OPEN_TO_WRITE,
        OPEN_TO_WRITE_HEADER,
        WRITE_FROM_FILE,
        WRITE_OPENS,
        WRITESTR_ZIPINFO,
        WRITESTR_LOCALTIME,
        WRITESTR_OPENS,
        ZIPFILE_DEL,
        CLOSE_SEEKS,
        CLOSE_WRITES_END_RECORD,
        CLOSE_FPCLOSE,
        END_RECORD_FLUSHES,
        FPCLOSE_CLOSES,
    ];
}

#[cfg(test)]
mod tests {
    use super::super::markdown::Table;
    use super::super::pyio::Stream;
    use super::super::pyio::fake::FakeOs;
    use super::*;
    use std::cell::RefCell;
    use std::rc::Rc;

    /// 書けるバイト数の上限(RLIMIT_FSIZE のような)と誤りを決めた書き込み先。
    #[derive(Clone, Default)]
    struct FakeFile(Rc<RefCell<FileState>>);

    #[derive(Default)]
    struct FileState {
        data: Vec<u8>,
        limit: Option<usize>,
        errno: i32,
        calls: usize,
        /// 閉じるとき(close(2))の errno。
        close_errno: Option<i32>,
        /// 閉じる間に SIGINT が届く(EINTR の試験)。
        interrupt_on_close: Option<FakeOs>,
        closed: usize,
    }

    impl FakeFile {
        fn limited(limit: usize, errno: i32) -> Self {
            let file = Self::default();
            file.0.borrow_mut().limit = Some(limit);
            file.0.borrow_mut().errno = errno;
            file
        }

        fn data(&self) -> Vec<u8> {
            self.0.borrow().data.clone()
        }

        fn failing_close(self, errno: i32) -> Self {
            self.0.borrow_mut().close_errno = Some(errno);
            self
        }
    }

    impl Sink for FakeFile {
        fn write_once(&mut self, data: &[u8]) -> io::Result<usize> {
            let mut state = self.0.borrow_mut();
            state.calls += 1;
            let room = match state.limit {
                Some(limit) if state.data.len() >= limit => {
                    return Err(io::Error::from_raw_os_error(state.errno));
                }
                Some(limit) => limit - state.data.len(),
                None => usize::MAX,
            };
            let count = data.len().min(room);
            state.data.extend_from_slice(&data[..count]);
            Ok(count)
        }

        fn close(self) -> io::Result<()> {
            let mut state = self.0.borrow_mut();
            state.closed += 1;
            if let Some(os) = &state.interrupt_on_close {
                os.0.borrow_mut().pending = true;
            }
            match state.close_errno {
                Some(errno) => Err(io::Error::from_raw_os_error(errno)),
                None => Ok(()),
            }
        }
    }

    /// 決めた時刻。
    struct FixedClock {
        now: i64,
        local: Result<CivilTime, i32>,
    }

    impl FixedClock {
        fn at(year: i64) -> Self {
            Self {
                now: 1_790_000_000,
                local: Ok(CivilTime {
                    year,
                    month: 11,
                    day: 12,
                    hour: 5,
                    minute: 6,
                    second: 7,
                }),
            }
        }
    }

    impl Clock for FixedClock {
        fn now(&self) -> i64 {
            self.now
        }

        fn local(&self, _seconds: i64) -> io::Result<CivilTime> {
            self.local.map_err(io::Error::from_raw_os_error)
        }
    }

    fn runtime() -> (Runtime, FakeOs) {
        let state = r4e::startup_state().expect("起動前の記録がある");
        let os = FakeOs::new();
        let runtime = Runtime::start_with(&state, &|_| None, Box::new(os.clone())).unwrap();
        (runtime, os)
    }

    fn book(runtime: &mut Runtime) -> Workbook {
        let table = Table {
            name: PyStr::from("three 三行"),
            rows: [["a", "b"], ["1", "2"], ["3", "4"]]
                .iter()
                .map(|row| row.iter().map(|cell| PyStr::from(*cell)).collect())
                .collect(),
        };
        Workbook::build(runtime, &[table]).unwrap()
    }

    fn places(error: &PyException) -> Vec<(&'static str, u32)> {
        error
            .traceback
            .iter()
            .map(|frame| (frame.file, frame.line))
            .collect()
    }

    const HEAD: [(&str, u32); 4] = [
        ("md_tables_to_xlsx.py", 241),
        ("md_tables_to_xlsx.py", 233),
        ("md_tables_to_xlsx.py", 196),
        ("openpyxl/workbook/workbook.py", 386),
    ];

    fn with_head(rest: &[(&'static str, u32)]) -> Vec<(&'static str, u32)> {
        [&HEAD[..], rest].concat()
    }

    fn u16_at(data: &[u8], at: usize) -> u16 {
        u16::from_le_bytes([data[at], data[at + 1]])
    }

    fn u32_at(data: &[u8], at: usize) -> u32 {
        u32::from_le_bytes(data[at..at + 4].try_into().unwrap())
    }

    /// 非圧縮ブロックの並びを展開する(最後のブロックで終わり、余りが無いこと)。
    fn inflate_stored(mut data: &[u8]) -> Vec<u8> {
        let mut out = Vec::new();
        loop {
            let final_block = data[0] == 1;
            assert!(data[0] <= 1, "非圧縮ブロックの見出し");
            let length = usize::from(u16_at(data, 1));
            assert_eq!(u16_at(data, 3), !(length as u16));
            out.extend_from_slice(&data[5..5 + length]);
            data = &data[5 + length..];
            if final_block {
                assert!(data.is_empty());
                return out;
            }
        }
    }

    #[test]
    fn crc32_matches_zlib() {
        assert_eq!(crc32(b""), 0);
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
        assert_eq!(
            crc32(b"The quick brown fox jumps over the lazy dog"),
            0x414F_A339
        );
    }

    #[test]
    fn stored_blocks_split_at_65535_bytes() {
        let mut out = Vec::new();
        push_stored(&mut out, b"");
        assert_eq!(out, [1, 0, 0, 0xff, 0xff]);
        assert_eq!(stored_len(0), 5);
        let mut out = Vec::new();
        push_stored(&mut out, b"abc");
        assert_eq!(out, [1, 3, 0, 0xfc, 0xff, b'a', b'b', b'c']);
        for size in [65534, 65535, 65536, 131_070, 131_071] {
            let data = (0..size).map(|index| index as u8).collect::<Vec<_>>();
            let mut out = Vec::new();
            push_stored(&mut out, &data);
            assert_eq!(out.len(), stored_len(size), "{size}");
            assert_eq!(inflate_stored(&out), data, "{size}");
        }
        assert_eq!(stored_len(65535), 65540);
        assert_eq!(stored_len(65536), 65546);
    }

    #[test]
    fn the_part_zip64_rule_uses_the_size_times_1_05() {
        // zipfile.py:1605 の `zinfo.file_size * 1.05 > ZIP64_LIMIT`。整数の大きさでは積が ZIP64_LIMIT と
        // ちょうど等しくならないので、等しい所は「超えない最大の値」と「超える最小の値」の間にある。
        let before = 2_045_222_520u64;
        let first = 2_045_222_521u64;
        assert!(before as f64 * 1.05 < ZIP64_LIMIT as f64);
        assert!(first as f64 * 1.05 > ZIP64_LIMIT as f64);
        assert!(!part_needs_zip64(0));
        assert!(!part_needs_zip64(before));
        assert!(part_needs_zip64(first));
        assert!(part_needs_zip64(first + 1));
        assert!(part_needs_zip64(ZIP64_LIMIT));
        assert!(part_needs_zip64(u64::MAX));
    }

    #[test]
    fn the_count_zip64_rule_starts_after_65535_parts() {
        assert!(!count_needs_zip64(65534));
        assert!(!count_needs_zip64(65535));
        assert!(count_needs_zip64(65536));
        let entry = |offset| Entry {
            name: b"a".to_vec(),
            flag_bits: 0,
            date: 0,
            time: 0,
            crc: 0,
            compress_size: 5,
            file_size: 0,
            header_offset: offset,
            external_attr: PART_ATTRIBUTES,
            extract_version: DEFAULT_VERSION,
            create_version: DEFAULT_VERSION,
        };
        let records = end_records(&vec![entry(0); 65535], 1000);
        assert_eq!(records.len(), 65535 * 47 + 22);
        assert_eq!(
            &records[records.len() - 22..records.len() - 18],
            b"PK\x05\x06"
        );
        assert_eq!(u16_at(&records, records.len() - 14), 65535);
        let records = end_records(&vec![entry(0); 65536], 1000);
        let end = records.len() - 22;
        assert_eq!(records.len(), 65536 * 47 + 56 + 20 + 22);
        assert_eq!(&records[end - 76..end - 72], b"PK\x06\x06");
        assert_eq!(&records[end - 20..end - 16], b"PK\x06\x07");
        // 位置の記録は ZIP64 の終わりの記録の位置(中央ディレクトリの後)。
        assert_eq!(
            u64::from_le_bytes(records[end - 16 + 4..end - 16 + 12].try_into().unwrap()),
            1000 + 65536 * 47
        );
        assert_eq!(u16_at(&records, end + 8), 0xffff);
        assert_eq!(u16_at(&records, end + 10), 0xffff);
        // 中央ディレクトリの位置と大きさの規則(Rust の位置に zipfile と同じ規則を当てる)。
        let records = end_records(&[entry(0)], ZIP64_LIMIT + 1);
        let end = records.len() - 22;
        assert_eq!(&records[end - 76..end - 72], b"PK\x06\x06");
        // 終わりの記録の位置の欄は min(位置, 0xffffffff)(zipfile と同じ)。
        assert_eq!(u64::from(u32_at(&records, end + 16)), ZIP64_LIMIT + 1);
        assert_eq!(u16_at(&records, end + 8), 1);
        let records = end_records(&[entry(0)], ZIP64_LIMIT);
        assert_eq!(records.len(), 47 + 22);
    }

    #[test]
    fn large_sizes_and_offsets_get_zip64_fields() {
        let mut entry = Entry {
            name: b"xl/worksheets/sheet1.xml".to_vec(),
            flag_bits: 0,
            date: 0x5b6c,
            time: 0x28c3,
            crc: 0x1234_5678,
            compress_size: stored_len(2_045_222_521) as u64,
            file_size: 2_045_222_521,
            header_offset: 0,
            external_attr: SHEET_ATTRIBUTES,
            extract_version: DEFAULT_VERSION,
            create_version: DEFAULT_VERSION,
        };
        // 1.05 倍で zip64 になるが、大きさは ZIP64_LIMIT 以下: 追加の欄に大きさを書き、版は 20 のまま。
        let header = entry.local_header(part_needs_zip64(entry.file_size));
        assert_eq!(header.len(), 30 + 24 + 20);
        assert_eq!(header[4], 20);
        assert_eq!(u32_at(&header, 22), 2_045_222_521);
        assert_eq!(u16_at(&header, 28), 20);
        assert_eq!(&header[54..58], &[1, 0, 16, 0]);
        let record = entry.central_record();
        assert_eq!(record.len(), 46 + 24);
        assert_eq!((record[4], record[5], record[6]), (20, 3, 20));
        assert_eq!(u32_at(&record, 38), SHEET_ATTRIBUTES);
        // 大きさが ZIP64_LIMIT を超える: どちらの欄も 0xffffffff、版は 45。
        entry.file_size = ZIP64_LIMIT + 1;
        entry.compress_size = stored_len((ZIP64_LIMIT + 1) as usize) as u64;
        let header = entry.local_header(true);
        assert_eq!(header[4], 45);
        assert_eq!(u32_at(&header, 18), 0xffff_ffff);
        assert_eq!(u32_at(&header, 22), 0xffff_ffff);
        let record = entry.central_record();
        assert_eq!((record[4], record[6]), (45, 45));
        assert_eq!(u16_at(&record, 30), 20);
        assert_eq!(&record[70..74], &[1, 0, 16, 0]);
        // 位置だけが ZIP64_LIMIT を超える: 中央ディレクトリの追加の欄に位置だけ、版は 45。
        let mut entry = Entry {
            file_size: 10,
            compress_size: 15,
            header_offset: ZIP64_LIMIT + 1,
            extract_version: DEFAULT_VERSION,
            create_version: DEFAULT_VERSION,
            ..entry
        };
        let header = entry.local_header(false);
        assert_eq!((header[4], header.len()), (20, 30 + 24));
        let record = entry.central_record();
        assert_eq!((record[4], record[6]), (45, 45));
        assert_eq!(u32_at(&record, 42), 0xffff_ffff);
        assert_eq!(&record[70..74], &[1, 0, 8, 0]);
        assert_eq!(
            u64::from_le_bytes(record[74..82].try_into().unwrap()),
            ZIP64_LIMIT + 1
        );
    }

    #[test]
    fn dos_fields_round_seconds_down_and_overflow_after_2107() {
        let when = CivilTime {
            year: 2026,
            month: 11,
            day: 12,
            hour: 5,
            minute: 6,
            second: 7,
        };
        assert_eq!(
            dos_fields(&when),
            (Some((46 << 9) | (11 << 5) | 12), (5 << 11) | (6 << 5) | 3)
        );
        let last = CivilTime {
            year: 2107,
            month: 12,
            day: 31,
            ..when
        };
        assert_eq!(dos_fields(&last).0, Some(65439));
        assert_eq!(dos_fields(&CivilTime { year: 2108, ..when }).0, None);
    }

    #[test]
    fn a_saved_book_has_the_legacy_layout() {
        let (mut py, os) = runtime();
        let book = book(&mut py);
        let file = FakeFile::default();
        let opened = file.clone();
        save(
            &mut py,
            &book,
            1_790_000_000,
            || Ok(opened),
            &FixedClock::at(2026),
        )
        .unwrap_or_else(|failure| panic!("{:?}", failure.exception));
        assert_eq!(os.written(Stream::Stderr), "");
        let data = file.data();
        let parts = book.parts(CoreTimes {
            created: 1_790_000_000,
            modified: 1_790_000_000,
        });
        let mut at = 0;
        for part in &parts {
            assert_eq!(&data[at..at + 4], b"PK\x03\x04", "{}", part.name);
            assert_eq!(
                (data[at + 4], data[at + 5], u16_at(&data, at + 6)),
                (20, 0, 0)
            );
            assert_eq!(u16_at(&data, at + 8), 8);
            assert_eq!(u16_at(&data, at + 12), (46 << 9) | (11 << 5) | 12);
            assert_eq!(u32_at(&data, at + 14), crc32(&part.data));
            let compressed = u32_at(&data, at + 18) as usize;
            assert_eq!(u32_at(&data, at + 22) as usize, part.data.len());
            let name_len = usize::from(u16_at(&data, at + 26));
            assert_eq!(u16_at(&data, at + 28), 0);
            assert_eq!(&data[at + 30..at + 30 + name_len], part.name.as_bytes());
            let body = at + 30 + name_len;
            assert_eq!(inflate_stored(&data[body..body + compressed]), part.data);
            at = body + compressed;
        }
        let central = at;
        for (index, part) in parts.iter().enumerate() {
            assert_eq!(&data[at..at + 4], b"PK\x01\x02");
            assert_eq!((data[at + 4], data[at + 5], data[at + 6]), (20, 3, 20));
            let attributes = u32_at(&data, at + 38);
            let expected = if index == 3 {
                0o100600 << 16
            } else {
                0o600 << 16
            };
            assert_eq!(attributes, expected, "{}", part.name);
            let name_len = usize::from(u16_at(&data, at + 28));
            assert_eq!(u16_at(&data, at + 30), 0);
            assert_eq!(u16_at(&data, at + 32), 0);
            at += 46 + name_len;
        }
        assert_eq!(&data[at..at + 4], b"PK\x05\x06");
        assert_eq!(u16_at(&data, at + 10), 9);
        assert_eq!(u32_at(&data, at + 12) as usize, at - central);
        assert_eq!(u32_at(&data, at + 16) as usize, central);
        assert_eq!(data.len(), at + 22);
    }

    #[test]
    fn the_first_write_failure_matches_the_legacy_lines() {
        // RLIMIT_FSIZE=0: 最初の書き出しは app.xml を閉じる所(1180 行)。
        let (mut py, os) = runtime();
        let book = book(&mut py);
        let file = FakeFile::limited(0, 27);
        let opened = file.clone();
        let failure =
            save(&mut py, &book, 0, || Ok(opened), &FixedClock::at(2026)).expect_err("失敗する");
        assert_eq!(
            places(&failure.exception),
            with_head(&[
                ("openpyxl/writer/excel.py", 294),
                ("openpyxl/writer/excel.py", 275),
                ("openpyxl/writer/excel.py", 60),
                ("zipfile.py", 1816),
                ("zipfile.py", 1180),
            ])
        );
        assert_eq!(
            failure.exception.type_line().to_string_lossy(),
            "OSError: [Errno 27] File too large"
        );
        failure.archive.expect("閉じていない").finalize(&mut py);
        let stderr = os.written(Stream::Stderr);
        let (header, rest) = stderr.split_once('\n').unwrap();
        let address = header
            .strip_prefix("Exception ignored in: <function ZipFile.__del__ at 0x")
            .and_then(|tail| tail.strip_suffix('>'))
            .unwrap();
        assert!(
            address.bytes().all(|byte| byte.is_ascii_hexdigit()),
            "{header}"
        );
        assert_eq!(
            rest,
            "Traceback (most recent call last):\n  File \"zipfile.py\", line 1821, in __del__\n    self.close()\n  File \"zipfile.py\", line 1843, in close\n    self._fpclose(fp)\n  File \"zipfile.py\", line 1943, in _fpclose\n    fp.close()\nOSError: [Errno 27] File too large\n"
        );
        assert!(file.data().is_empty());
    }

    #[test]
    fn a_failure_in_a_later_part_names_that_part() {
        let (mut py, os) = runtime();
        let book = book(&mut py);
        // app.xml と core.xml は書けて、シートの途中で ENOSPC。
        let file = FakeFile::limited(10_965, 28);
        let opened = file.clone();
        let failure = save(&mut py, &book, 0, || Ok(opened), &FixedClock::at(2026))
            .err()
            .unwrap();
        assert_eq!(
            places(&failure.exception),
            with_head(&[
                ("openpyxl/writer/excel.py", 294),
                ("openpyxl/writer/excel.py", 275),
                ("openpyxl/writer/excel.py", 77),
                ("openpyxl/writer/excel.py", 215),
                ("openpyxl/writer/excel.py", 203),
                ("zipfile.py", 1775),
                ("zipfile.py", 1180),
            ])
        );
        assert_eq!(
            failure.exception.type_line().to_string_lossy(),
            "OSError: [Errno 28] No space left on device"
        );
        assert_eq!(file.data().len(), 10_965);
        failure.archive.unwrap().finalize(&mut py);
        assert!(
            os.written(Stream::Stderr)
                .ends_with("    fp.close()\nOSError: [Errno 28] No space left on device\n")
        );
    }

    #[test]
    fn a_failure_in_the_end_records_chains_the_close_failure() {
        let (mut py, os) = runtime();
        let book = book(&mut py);
        let whole = {
            let file = FakeFile::default();
            let opened = file.clone();
            save(&mut py, &book, 0, || Ok(opened), &FixedClock::at(2026))
                .unwrap_or_else(|failure| panic!("{:?}", failure.exception));
            file.data().len()
        };
        let file = FakeFile::limited(whole - 30, 27);
        let opened = file.clone();
        let failure = save(&mut py, &book, 0, || Ok(opened), &FixedClock::at(2026))
            .err()
            .unwrap();
        assert!(failure.archive.is_none(), "閉じた後は __del__ が何もしない");
        assert_eq!(
            places(&failure.exception),
            with_head(&[
                ("openpyxl/writer/excel.py", 294),
                ("openpyxl/writer/excel.py", 276),
                ("zipfile.py", 1843),
                ("zipfile.py", 1943),
            ])
        );
        let context = failure.exception.context.as_ref().expect("前の例外");
        assert_eq!(
            places(context),
            [("zipfile.py", 1839), ("zipfile.py", 1937)]
        );
        assert_eq!(os.written(Stream::Stderr), "");
    }

    /// 例外の連鎖(`__context__` を辿る)の、各例外の最後の行と呼出履歴の場所。
    fn chain(error: &PyException) -> Vec<(String, Vec<(&'static str, u32)>)> {
        let mut items = Vec::new();
        let mut current = Some(error);
        while let Some(exception) = current {
            items.push((exception.type_line().to_string_lossy(), places(exception)));
            current = exception.context.as_ref();
        }
        items
    }

    const FPCLOSE: [(&str, u32); 4] = [
        ("openpyxl/writer/excel.py", 294),
        ("openpyxl/writer/excel.py", 276),
        ("zipfile.py", 1843),
        ("zipfile.py", 1943),
    ];

    #[test]
    fn a_close_failure_raises_from_fp_close_like_the_legacy() {
        let (mut py, os) = runtime();
        let book = book(&mut py);
        // 書き終えた後の close(2) だけが失敗する: fp.close() の行からの OSError(連鎖なし)。
        let file = FakeFile::default().failing_close(5);
        let opened = file.clone();
        let failure = save(&mut py, &book, 0, || Ok(opened), &FixedClock::at(2026))
            .err()
            .unwrap();
        assert!(failure.archive.is_none());
        assert_eq!(
            chain(&failure.exception),
            [(
                "OSError: [Errno 5] Input/output error".to_owned(),
                with_head(&FPCLOSE)
            )]
        );
        assert_eq!(file.0.borrow().closed, 1);
        let complete = file.data();
        assert_eq!(
            &complete[complete.len() - 22..complete.len() - 18],
            b"PK\x05\x06"
        );

        // 終わりの記録の書き出しも、fp.close() の書き出しも、close(2) も失敗する:
        // close(2) の OSError → 呼出履歴の無い書き出しの例外 → 終わりの記録の例外、の連鎖。
        let file = FakeFile::limited(complete.len() - 30, 27).failing_close(5);
        let opened = file.clone();
        let failure = save(&mut py, &book, 0, || Ok(opened), &FixedClock::at(2026))
            .err()
            .unwrap();
        assert_eq!(
            chain(&failure.exception),
            [
                (
                    "OSError: [Errno 5] Input/output error".to_owned(),
                    with_head(&FPCLOSE)
                ),
                ("OSError: [Errno 27] File too large".to_owned(), vec![]),
                (
                    "OSError: [Errno 27] File too large".to_owned(),
                    vec![("zipfile.py", 1839), ("zipfile.py", 1937)]
                ),
            ]
        );

        // EINTR: 届いた SIGINT があれば KeyboardInterrupt、無ければ InterruptedError。
        let file = FakeFile::default().failing_close(errors::EINTR);
        let opened = file.clone();
        let failure = save(&mut py, &book, 0, || Ok(opened), &FixedClock::at(2026))
            .err()
            .unwrap();
        assert_eq!(
            failure.exception.type_line().to_string_lossy(),
            "InterruptedError: [Errno 4] Interrupted system call"
        );
        let file = FakeFile::default().failing_close(errors::EINTR);
        file.0.borrow_mut().interrupt_on_close = Some(os.clone());
        let opened = file.clone();
        let failure = save(&mut py, &book, 0, || Ok(opened), &FixedClock::at(2026))
            .err()
            .unwrap();
        assert_eq!(
            chain(&failure.exception),
            [("KeyboardInterrupt".to_owned(), with_head(&FPCLOSE))]
        );
        assert_eq!(os.written(Stream::Stderr), "");
    }

    #[test]
    fn a_close_failure_in_zipfile_del_shows_only_the_last_exception() {
        // 部分の書き込みで失敗し、__del__ の close でも書き出しと close(2) が失敗する。
        // Exception ignored の表示は、連鎖を出さず、close(2) の例外だけ(旧版の実測)。
        let (mut py, os) = runtime();
        let book = book(&mut py);
        let file = FakeFile::limited(0, 27).failing_close(5);
        let opened = file.clone();
        let failure = save(&mut py, &book, 0, || Ok(opened), &FixedClock::at(2026))
            .err()
            .unwrap();
        failure.archive.expect("閉じていない").finalize(&mut py);
        let stderr = os.written(Stream::Stderr);
        let (_, rest) = stderr.split_once('\n').unwrap();
        assert_eq!(
            rest,
            "Traceback (most recent call last):\n  File \"zipfile.py\", line 1821, in __del__\n    self.close()\n  File \"zipfile.py\", line 1843, in close\n    self._fpclose(fp)\n  File \"zipfile.py\", line 1943, in _fpclose\n    fp.close()\nOSError: [Errno 5] Input/output error\n"
        );
        assert_eq!(file.0.borrow().closed, 1);
    }

    #[test]
    fn opening_failures_keep_the_later_error_and_the_path() {
        let missing = PyPath::parse(b"/nonexistent-r4e-zip/dir/book.xlsx");
        let error = create(&missing).unwrap_err();
        assert_eq!(
            error.type_line().to_string_lossy(),
            "FileNotFoundError: [Errno 2] No such file or directory: '/nonexistent-r4e-zip/dir/book.xlsx'"
        );
        let (mut py, _os) = runtime();
        let book = book(&mut py);
        let failure = save::<FakeFile>(
            &mut py,
            &book,
            0,
            || Err(PyException::os(13)),
            &FixedClock::at(2026),
        )
        .err()
        .unwrap();
        assert!(failure.archive.is_none());
        assert_eq!(
            places(&failure.exception),
            with_head(&[("openpyxl/writer/excel.py", 291), ("zipfile.py", 1251)])
        );
    }

    #[test]
    fn clock_edges_raise_the_zipfile_errors() {
        let (mut py, os) = runtime();
        let book = book(&mut py);
        let run = |py: &mut Runtime, clock: FixedClock| {
            let file = FakeFile::default();
            let opened = file.clone();
            let failure = save(py, &book, 0, || Ok(opened), &clock).err().unwrap();
            let line = failure.exception.type_line().to_string_lossy();
            let places = places(&failure.exception);
            failure.archive.unwrap().finalize(py);
            (line, places, file.data())
        };
        let (line, places, data) = run(&mut py, FixedClock::at(1979));
        assert_eq!(
            line,
            "ValueError: ZIP does not support timestamps before 1980"
        );
        assert_eq!(
            &places[places.len() - 3..],
            [
                ("openpyxl/writer/excel.py", 60),
                ("zipfile.py", 1788),
                ("zipfile.py", 364),
            ]
        );
        // 部分の無い ZIP として閉じる(旧版の __del__ と同じ)。
        assert_eq!(data, end_records(&[], 0));
        let (line, places, _) = run(&mut py, FixedClock::at(2108));
        assert_eq!(
            line,
            "struct.error: ushort format requires 0 <= number <= (32767 *2 +1)"
        );
        assert_eq!(
            &places[places.len() - 5..],
            [
                ("openpyxl/writer/excel.py", 60),
                ("zipfile.py", 1816),
                ("zipfile.py", 1519),
                ("zipfile.py", 1614),
                ("zipfile.py", 450),
            ]
        );
        let mut clock = FixedClock::at(2026);
        clock.local = Err(75);
        let (line, places, _) = run(&mut py, clock);
        assert_eq!(
            line,
            "OSError: [Errno 75] Value too large for defined data type"
        );
        assert_eq!(
            &places[places.len() - 2..],
            [("openpyxl/writer/excel.py", 60), ("zipfile.py", 1789),]
        );
        assert_eq!(os.written(Stream::Stderr), "");
        // シートの部分は ZipFile.write の段(旧版は一時ファイルの時刻を from_file で読む)。
        assert_eq!(Kind::Write.zipinfo().len(), 3);
        assert_eq!(Kind::Write.localtime()[1].line, 506);
        assert_eq!(Kind::Write.file_header()[0].line, 1775);
    }

    #[test]
    fn a_keyboard_interrupt_leaves_a_valid_zip_of_the_written_parts() {
        let (mut py, os) = runtime();
        let book = book(&mut py);
        os.0.borrow_mut().pending = true;
        let file = FakeFile::default();
        let opened = file.clone();
        let failure = save(&mut py, &book, 0, || Ok(opened), &FixedClock::at(2026))
            .err()
            .unwrap();
        assert_eq!(
            failure.exception.type_line().to_string_lossy(),
            "KeyboardInterrupt"
        );
        assert_eq!(
            places(&failure.exception).last(),
            Some(&("openpyxl/writer/excel.py", 291))
        );
        assert_eq!(file.0.borrow().calls, 0);
        let file = FakeFile::default();
        let opened = file.clone();
        let mut archive = ZipFile::new(opened);
        let parts = book.parts(CoreTimes {
            created: 0,
            modified: 0,
        });
        archive
            .write_part(&mut py, &parts[0], FixedClock::at(2026).local(0))
            .unwrap();
        os.0.borrow_mut().pending = true;
        let error = archive
            .write_part(&mut py, &parts[1], FixedClock::at(2026).local(0))
            .unwrap_err();
        assert_eq!(error.type_line().to_string_lossy(), "KeyboardInterrupt");
        archive.finalize(&mut py);
        assert_eq!(os.written(Stream::Stderr), "");
        let data = file.data();
        // app.xml・core.xml(書いたが閉じる前に中断した)・app.xml だけの中央ディレクトリ。
        let end = data.len() - 22;
        assert_eq!(&data[end..end + 4], b"PK\x05\x06");
        assert_eq!(u16_at(&data, end + 10), 1);
    }
}
