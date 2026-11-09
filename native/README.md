# Rust の土台と共有の約束

このフォルダには、段階 0a(#442)の土台、段階 1a(#448)の保存先解決、C0a(#453)の共通入出力、C0b(#456)の上限と保持実体の検査を実装しています。
`resolve-task-dir` は段階 1b で配布済みです。C0a・C0bは開発用ソースだけにあり、配布は段階 1b・プラグイン v4.53.0 のままです。C0cは未着手です。
現在の配布状況は [README.md](../README.md#e-rust-版を試す段階-1b) を参照してください。
内部の版は `0.1.0` です。配布プラグインの版とは別に管理します。
`probe --json` は実行環境とビルド情報を JSON で返します。
`probe --self-sha256` は自己の実行ファイル全体の SHA256 を返します。
どちらも末尾改行付きの標準出力と終了 0 を返します。
不正な引数は終了 2、自己のファイルを読めない場合などの実行時エラーは終了 1 です。
実行時に外部コマンド・ネットワーク・設定ファイルを使わず、状態ファイルを作りません。

## タスク保存先の解決(段階 1a)

`dev-workflow resolve-task-dir --project-root ROOT [--task-dir PATH]` で保存先を解決します。

- `--project-root` は必須です。`--task-dir` の省略と空文字は区別します。
- 成功は JSON の標準出力と終了 0、解決失敗は JSON の標準出力と終了 1、候補複数は JSON の標準出力と終了 2 です。これらの標準エラーは空です。
- 引数の検査は Python 3.10 の旧版に合わせます。ファイルの作成・変更、タスク本文の読み取りは行いません。
- 一覧を読めても子の情報を取得できない権限条件では、旧版の未処理の `PermissionError` と Rust の JSON エラーの差を認めます。
- 非 UTF-8 のパスを `utf-8:strict` で出力すると、旧版は JSON 出力時に `UnicodeEncodeError` で終了します。Rust は元のバイトを保ちます。この差を認める条件と出力先は [設計の記録](../docs/design.md#タスク保存先の解決段階-1a)にあります。
- 旧版との通常の比較、権限と非 UTF-8 の専用検査は [検査手順](../docs/testing.md#タスク保存先の比較段階-1a)にあります。

## 部品とコマンドの登録

- `crates/cli`: 引数を `OsString` のまま受け取る入口です。
- `crates/core`: 終了コード・入出力処理・処理量と時間の上限・検査付き読取り・ビルド情報を持ちます。
- `crates/platform`: OS 固有の処理を置く境界です。C0bで、開いたフォルダとファイルの保持・属性検査を追加しました。
- `platform` 以外の自作 crate(Rust の部品単位)は `#![forbid(unsafe_code)]` を使います。
- `platform` は `#![deny(unsafe_op_in_unsafe_fn)]` を使います。公開する関数は安全な関数に限ります。

`crates/cli/src/commands/mod.rs` の `COMMANDS: &[Command]` が名前と関数の固定表です。
`Command` は `name: &'static str` と `run: fn(&[OsString], &mut CommandIo<'_>) -> CommandResult` を持ちます。
関数へ渡す引数にはコマンド名を含めません。同じ実行期間の入出力も渡します。
登録済みの `probe` と `resolve-task-dir` は、短い変換関数から既存の本体を呼びます。
`resolve-task-dir` は `commands/resolve_task_dir/` に置きます。今後の機能も `commands/<名前>/mod.rs` に追加します。
`probe.rs` は段階 0 の試しのコマンドとして単独ファイルに置きます。

`--version` と `--help` は `main.rs` が受け持ちます。
引数なし、未知のコマンド、ほかの引数を伴う全体のオプションは終了 2 です。
コマンド固有の引数検査は各関数が受け持ちます。

## 終了コードと入出力(C0a)

`dev_workflow_core` は次の型と関数を公開します。

| 名前 | 約束 |
|---|---|
| `ExitCode` | `new(u8)` と `as_u8()` で0〜255を保持・返却する。`SUCCESS`・`RUNTIME`・`USAGE` は0・1・2 |
| `CommandOutput` | 最終の `stdout: Vec<u8>`・`stderr: Vec<u8>` と `code: ExitCode`。`stdout(...)` は標準出力と終了0を作る |
| `CommandResult` | `Result<CommandOutput, CommandError>`。`Ok` でも任意のu8終了値と最終出力を返せる |
| `CommandError` | `Runtime(String)` と `Usage(String)`。`runtime(...)`・`usage(...)` で作れる |
| `CommandIo::read_input(&mut [u8])` | 指定バッファへ1回だけ読み、`io::Result<usize>` を返す。先読み・再試行・文字変換をしない |
| `CommandIo::write_stdout(&[u8])`・`write_stderr(&[u8])` | 生バイトを `write_all` し、同じ出力先の `flush` 成功後に `Ok(())` を返す |
| `execute(command)` | 実際の標準入出力を借用し、`execute_to` を呼ぶ。CLIが使う入口 |
| `execute_to(input, stdout, stderr, command)` | `&mut dyn Read` と2本の `&mut dyn Write` を借用する。処理へ `&mut CommandIo` を渡し、最終の `ExitCode` を返す |
| `emit(result)` | 標準出力・標準エラーへ書き、`ExitCode` を返す |
| `emit_to(result, stdout, stderr)` | 書き込み先を指定する同じ処理。テストにも使う |

```text
main -> execute -> 登録表の関数(args, io)
                    |-- 途中出力 -> flush -> 入力待ち
                    `-- CommandResult -> emit_to -> ExitCode
```

- `CommandIo` は借用した入出力だけを使います。ファイルやプロセスは開きません。
- 入力の短読・EOF・エラーはそのまま返します。`Interrupted` も再試行しません。入力エラーを保持して終了1にせず、コマンドが終了値を選びます。
- 途中出力は呼出順に行います。別パイプの読取り側が観測する順序までは保証しません。最終出力は既存の標準出力→標準エラーの順です。
- `CommandOutput` のバイト列には接頭辞や改行を追加しません。無出力の停止や固定の文言も、そのまま表せます。
- `CommandError` は標準エラーへ `dev-workflow: <理由>\n` の形で書きます。
- `resolve-task-dir` の解決失敗は `CommandOutput` で JSON の標準出力を返します。既存の `probe` の出力と終了値は変えません。
- 書き込みと `flush` はどちらも確認します。`BrokenPipe` も終了 1 です。
- 最初の出力失敗を保持します。後続の通常出力は書込みも `flush` もせずエラーを返します。コマンドが失敗を無視しても、実行入口は最終出力を止めて終了1にします。
- 標準出力の失敗では、標準エラーへ `dev-workflow: 標準出力に書けません: <理由>\n` を一度だけ試みます。標準エラーの失敗では追加診断を出しません。既に出たバイト列は取り消せません。
- 数値の終了値をシグナル終了と同一視しません。シグナル変換、時間や処理量の上限、ファイル保護、子プロセス監督はC0aに含みません。失敗を無視したコマンドの強制中断もしません。

C0bのファイル保護と上限は下の節に記します。C0cの子プロセス監督は未着手です。
分割と依存順は [設計の記録](../docs/design.md#共通の終了値と入出力c0a)、実プロセスでの確認は [検査手順](../docs/testing.md#共通の終了値と入出力の検査c0a)にあります。
#448の権限・非UTF-8出力の差は、保存先の専用比較だけに適用します。

`build_info` の定数は `NATIVE_VERSION`・`SOURCE_INPUT_SHA256`・`RUSTC` です。
`RUSTC` は `rustc --version` の末尾改行を除いた値です。
`--version` は `dev-workflow 0.1.0 (source_input_sha256: <小文字64桁>)\n` を返します。
`--help` は `--version`・`--help`・`probe --json`・`probe --self-sha256`・`resolve-task-dir` の 5 つの使い方を表示します。

## 処理量と時間の上限・保持実体の検査(C0b)

[Issue #456](https://github.com/Yuki-Maeda-valour/workflow/issues/456)で、共通部品を実装しました。
既存のCLIへは接続していません。YAML解析、機密指定の解釈、状態の走査、子プロセス監督は含みません。

### 呼出元が持つ上限と期限

`dev_workflow_core::budget` は次の型を公開します。

| 名前 | 約束 |
|---|---|
| `BudgetLimits` | `items`・`total_bytes`・`file_bytes` を `u64` で渡す。既定値は持たない |
| `Budget::new(limits, deadline)` | 呼出元の絶対期限 `Instant` と上限を保存し、累積値を0から始める |
| `items()`・`bytes()`・`deadline()`・`limits()` | 現在の値を返す。累積値のリセットや期限の延長は公開しない |
| `check_time()` | 現在時刻が期限を超えると拒否する。期限との等値は受け付ける |
| `remaining()` | 正の残時間を返す。期限との等値と超過は拒否する |
| `add_items(n)`・`add_bytes(n)` | 指定値を加えてから、それぞれの上限を検査する。超過した消費量も戻さない |
| `check_file_bytes(n)` | 1ファイルの上限だけを検査する。累積値は増やさない |
| `BudgetError` | `Deadline`・`Items`・`FileBytes`・`TotalBytes`・`Overflow` を区別する |

- 加算と1ファイル上限の検査は、自動では時刻を検査しません。呼出元が必要な順で組み合わせます。
- 数値が収まらない加算は `Overflow` です。値を折り返さず、それ以後は項目数とバイト数の両方の加算を拒否します。
- 読取りへは同じ `&mut Budget` を渡します。複数ファイルの途中で累積値と期限を作り直しません。
- 期限は、処理が明示的に時刻を確認する方式です。停止中のOS呼出しを期限で中断する保証はありません。処理全体の強制停止はC0c以降の責務です。

### 同じフォルダとファイルを保持する型

`dev_workflow_platform::fs` は、開いた実体を指すOSの識別子(fd)を型の内側に保持します。
所有する型の破棄で閉じます。識別子の取出し・複製や読取り位置の変更は公開しません。

| 名前 | 約束 |
|---|---|
| `FileSnapshot` | `dev`・`ino`・`mode`・`size` と、秒・ナノ秒に分けた `mtime`・`ctime` を保持する |
| `SnapshotMatch::Full` | 実体を識別する `dev`・`ino`、種類と権限の `mode`、容量、更新時刻、属性変更時刻をすべて比べる |
| `SnapshotMatch::DirectoryPath` | `dev`・`ino`・種類・権限を比べる。子の増減で変わるフォルダの容量と時刻は比べない |
| `HeldDir::open_absolute(path)` | 絶対パスの各段をリンクを辿らず開き、直前の属性と `DirectoryPath` で比べる |
| `HeldDir::metadata(name)` | 保持した親を基準に、リンクを辿らず子の属性を取得する |
| `HeldDir::open_dir(name, expected, comparison)` | 同じ親から子フォルダを開き、指定した比較項目で直前値を検査する |
| `HeldDir::verify_entry(name, expected, comparison)` | 保持した親の下の名前を再検査する。必要な呼出元が明示的に使う |
| `HeldFile::open_at(parent, name, expected)` | 同じ親から通常ファイルを開く。直前値と開いた実体を `Full` で比べる |
| `HeldFile::snapshot()`・`verify_unchanged()` | 同じ保持実体の属性を取得する・開いた時点から変わらないことを検査する |
| `HeldFile::begin_read()` | 本文を読む前に使用開始を記録し、排他的な `ReadSession` を1回だけ作る |
| `ReadSession` | `read_chunk(&mut [u8])`・`opened_snapshot()`・`verify_unchanged()` を公開する。本文はこの型だけが読める |
| `FsError` | `Io`・`InvalidInput`・`NotRegular`・`NotDirectory`・`Changed`・`AlreadyRead` を区別する |

- パスと名前は非UTF-8のバイトを保ちます。子の名前は空・`/`・NULを拒否します。
- `open_absolute` は `.`・`..` を消さず、実体を順に辿ります。パスの正規化や指定ルートへの閉じ込めは行いません。
- 開くときは `NOFOLLOW`・`NONBLOCK`・`CLOEXEC` を指定します。FIFOで書き手を待ちません。
- `FsError::Io` はOSのエラー種別と数値を保ちます。`is_not_found()` が真になるのは `ENOENT` だけです。`ENOTDIR`・`EACCES` などを不在へまとめません。
- 使用開始後は、未読のままセッションを破棄しても、空読取り・EOF・失敗の後でも `AlreadyRead` です。
- 親のパスが差し替わっても、保持した旧親から旧本文を読めます。最後に親のパスを必ず再検査する条件は追加しません。

### 検査付き読取り

`dev_workflow_core::fs::read_verified(&mut HeldFile, &mut Budget)` は `Result<Vec<u8>, ReadError>` を返します。
`ReadError` は `Fs(FsError)` と `Budget(BudgetError)` を区別します。

```text
保持した親 -> 子の属性 -> リンクを辿らず開く -> Full で比較
                                               |
使用開始を記録 -> 宣言サイズの上限 -> 65536 バイトずつ読む
                                               |
非空の読取り -> 期限 -> 総バイト加算 -> 1ファイル上限
                                               |
EOF -> 同じ実体を Full で再検査 -> 本文だけを返す
```

- 最初に使用開始を記録します。宣言サイズや期限などで最初の呼出しが拒否されても、再呼出しは `AlreadyRead` です。OSの追加呼出しも予算の追加消費もありません。
- 毎回65536バイトを要求します。残り予算に合わせて短くしません。短読後もEOFまで続けます。
- 非空の読取りごとに、期限確認→実際の総バイト加算→今回のファイルの累積バイト上限の順で検査します。EOFでは加算しません。
- 項目数の加算、宣言サイズによる総量の予約、実読取り量と宣言サイズの一致判定は行いません。
- `Interrupted` は期限を確認して再試行します。それ以外の読取り失敗は返します。
- EOFの後は同じ実体の属性を再検査します。途中で失敗した場合は本文を返さず、消費量も戻しません。

#454の既定値・不在の扱い・YAML解析前の閉鎖など、後続の呼出元へ残す責務は [設計の記録](../docs/design.md#処理量と時間の上限保持実体の検査c0b)にあります。
旧版との53組の比較、新APIの1件、比較器の自己テスト19件と必須の実行経路は [検査手順](../docs/testing.md#処理量と保持実体の検査c0b)にあります。

## ソース入力の SHA256

Task 2.2 の Python の計算も、次のバイト列をそのまま使います。
実装は `crates/core/source_input.rs` です。実行ファイルからは呼びません。

1. `CARGO_MANIFEST_DIR` から `git rev-parse --show-toplevel` でリポジトリ直下を求めます。
2. その直下で `git ls-files -s -z -- native` を実行します。`-z` はパスの区切りを NUL にします。
3. stage が `0` の項目だけを認めます。Git のモードと相対パスを取り出します。
4. 相対パスは `native/` を含むリポジトリ直下からのパスです。UTF-8 のバイト列の昇順で並べます。
5. 本文は作業ツリーから読みます。index(ステージした内容の置き場)の本文とオブジェクト名はハッシュに入れません。
6. 以下の順で連結したバイト列の SHA256 を取り、小文字の16進数64桁にします。

```text
ASCII("dev-workflow-source-input-v1") || 0x00 || U64BE(ファイル数)
各ファイルをパス順に:
    U64BE(パスのバイト数) || UTF8(相対パス)
    U64BE(モードのバイト数) || ASCII(Git のモード)
    U64BE(本文のバイト数) || 本文
```

`U64BE(n)` は符号なし64ビット整数を8バイトのビッグエンディアン(上位のバイトから先に並べる方式)で表した値です。
`||` はバイト列の連結です。追加の改行や区切りはありません。モードは6文字なので長さは常に6です。
空の本文も長さ0を入れます。本文の改行や文字コードは変えません。

照合用の例は1ファイルだけの `native/a`・モード `100644`・本文 `b"hello\0\n"` です。
期待値は `64c7696a322f08896afd9e2f6e7d824dc1c780f16282b89d40fdacfbd79c3f06` です。

- モード `100644`・`100755` は通常ファイルの本文を読みます。
- モード `120000` はリンク先の名前のバイト列を読みます。リンク先の本文は読みません。
- Linux と macOS のパスを対象にします。パスが UTF-8 でない場合と制御文字を含む場合は拒否します。
- 重複パス、空要素、`.`・`..`、競合中の項目、上記以外のモードは拒否します。
- 親フォルダがリンクの場合と、Git のモードと実体の種類が違う場合も拒否します。
- git の不在、コマンドの失敗、空の一覧、本文の読み取り失敗ではビルドを止めます。
- 追跡していないファイルと `native/` の外のファイルは含めません。生成物は追跡しないでください。
- commit の SHA・時刻・CI の実行番号は含めません。ビルド情報の定数にも置きません。

Git の全呼び出しに `base-commit.md` の安全用のオプションを同じ順で付けます。
`GIT_NO_LAZY_FETCH=1` と `GIT_TERMINAL_PROMPT=0` も付けます。
`build.rs` は追跡ファイル全部と `git rev-parse --git-path index` が示すパスを `cargo:rerun-if-changed` に渡します。
相対パスはすべて同じリポジトリ直下を基準に解決します。

## ビルドと検証

Rust は `rust-toolchain.toml` で `1.99.1` に固定しています。
`Cargo.lock` は共有します。直接依存は `sha2`・`serde`・`serde_json` と、C0bで追加した `rustix` です。
`rustix` は `=1.1.6` に固定し、`platform` だけで `fs` と既定の `std` を使います。採用の根拠は [設計の記録](../docs/design.md#c0b-で採用した-os-呼出しの部品)にあります。
共有の `Cargo.toml`・`Cargo.lock`・CLI入口と登録表・coreの公開する型とテストは、タスクごとに変更担当を1名へ固定してください。

次のコマンドは `native/` で実行します。
新しいソースは先に Git の追跡へ加える必要があります。空の一覧ではビルドしません。

```sh
export CARGO_TARGET_DIR="$(mktemp -d)"
cargo fmt --check
cargo clippy --all-targets --locked -- -D warnings
cargo test --locked
cargo build --release --locked --target x86_64-unknown-linux-musl
```

`cargo test --locked` は、C0bの実ファイル比較も実行します。`python3` がPython 3.10系であり、root以外の利用者であることが必須です。
専用比較を省略して合格にはしません。Mac実機と最低OSでのC0bの動作は未確認です。

release の設定は `codegen-units=1`・`opt-level=3`・`lto="fat"`・`strip="symbols"`・`debug=0`・`incremental=false` です。
panic の設定は既定のままです。Linux のリンカーは Rust の musl 向けの既定を使います。
`.cargo/config.toml` と `.cargo/rustc-wrapper.sh` がソース・依存・出力先の絶対パスを固定名へ置き換えます。
wrapper は bash 3.2 以上で動きます。Cargo が渡す `CARGO_BIN_EXE_dev-workflow` もコンパイラへ引き継ぎます。
CI(変更ごとの自動検査)から `RUSTFLAGS` や最適化設定を渡す必要はありません。
外部の設定や環境変数でビルドの設定を上書きしないでください。
再現性の比較は同じ道具と設定を使い、別々の空の `CARGO_TARGET_DIR` で作った最終実行ファイルに `cmp` を使います。

テストでは、一時リポジトリの `native/` の外のファイルを変更し、計算値が不変であることを確認します。
追跡した本文・パス・Git のモードの変更では計算値が変わることも確認します。
