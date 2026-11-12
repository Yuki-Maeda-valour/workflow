# Rust の土台と共有の約束

このフォルダには、段階 0a(#442)の土台、段階 1a(#448)の保存先解決、C0a(#453)の共通入出力、C0b(#456)の上限と保持実体の検査、C0c(#458)の単一子の実行と回収を実装しています。
R4Sのうちloop-state用YAML・機密指定・globの純粋部品は、#454の調査を基に#462で追加しました。
`resolve-task-dir` は段階 1b で配布済みです。C0a・C0b・C0c・R4S・R4Aのtask-digest・R4Bのorigin判定とGit設定の確認値・R4Rのタスク本文の復元の部品は開発用ソースだけにあり、配布は段階 1b・プラグイン v4.53.0 のままです。C0b/C0c/R4S/task-digest/R4B/R4Rは既存のCLIへ未接続です。
現在の配布状況は [README.md](../README.md#e-rust-版を試す段階-1b) を参照してください。
内部の版は `0.1.0` です。配布プラグインの版とは別に管理します。
`probe --json` は実行環境とビルド情報を JSON で返します。
`probe --self-sha256` は自己の実行ファイル全体の SHA256 を返します。
どちらも末尾改行付きの標準出力と終了 0 を返します。
不正な引数は終了 2、自己のファイルを読めない場合などの実行時エラーは終了 1 です。
既存のCLIは実行時に外部コマンド・ネットワーク・設定ファイルを使わず、状態ファイルを作りません。

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
- `crates/core`: 終了コード・入出力処理・処理量と時間の上限・検査付き読取り・単一子実行・loop-stateの純粋部品・ビルド情報を持ちます。
- `crates/platform`: OS 固有の処理を置く境界です。C0bの保持実体と属性検査、C0cの直接子とパイプの操作、R4Rのタスク本文の復元が使うOSの操作(`restore_taskmd`)を持ちます。
- `crates/yaml-parser`: R4S用の固定したYAML解析部品です。上流の版と局所修正は [UPSTREAM.md](crates/yaml-parser/UPSTREAM.md)にあります。
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

C0bのファイル保護と上限、C0cの単一子実行は下の節に記します。子孫全体の監督は後続のR4Qの責務です。
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
- 期限は、処理が明示的に時刻を確認する方式です。停止中のOS呼出しを期限で中断する保証はありません。C0cもこの限界を持ちます。

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

## 単一子の実行と回収(C0c)

[Issue #458](https://github.com/Yuki-Maeda-valour/workflow/issues/458)の部品は、直接起動した子1個だけを実行・停止・回収します。
子孫全体の停止や不在を保証するAPIではありません。配布物・既存の呼び出し元・旧Python・版・CHANGELOGは変更していません。

`dev_workflow_core::process` の入口は次の形です。

```rust
pub fn run_child<E>(
    command: &mut Command,
    input: ChildInput<'_>,
    budget: &mut Budget,
    deadlines: ChildDeadlines,
    cancel: &AtomicBool,
    consumer: impl FnMut(OutputStream, &[u8], &mut Budget) -> Result<(), E>,
) -> Result<ChildOutcome, RunFailure<E>>
```

| 型・引数 | 約束 |
|---|---|
| `Command` | `std::process::Command`。呼出元が引数・環境・作業フォルダを決める。3本の標準入出力だけをパイプへ上書きする |
| `ChildInput` | 借用する固定 `bytes: &[u8]` と `broken_pipe: BrokenPipePolicy`。空入力と全送信後はstdinを閉じる |
| `BrokenPipePolicy` | 未送信bytesがあるときのEPIPEや子終了を、`Reject`は `InputClosed`、`Close`は入力閉鎖後の続行とする |
| `ChildDeadlines` | `execution`・`cleanup` は呼出元が作る絶対時刻 `Instant`。`cleanup < execution` を起動前に拒否する |
| `cancel` | 呼出元の `AtomicBool` がtrueなら取消。OSの信号を捕捉する設定ではない |
| `OutputStream`・`ChildStream` | 前者は `Stdout`/`Stderr`、後者は入出力失敗の `Stdin`/`Stdout`/`Stderr` を区別する |
| `ChildStatus` | `Exited(ExitCode)` と `Signaled(i32)`。0〜255の数値と実際の信号番号を変換せず分ける |
| `ChildOutcome` | 回収した `status`、`input_written: usize`、`input_closed_early: bool`。未送信分を送信済みとしない |
| `RunCause<E>` | `InvalidDeadlines`・`Cancelled`・`Deadline`・`Spawn`・`Setup`・`Poll`・`InputClosed`・`Io { stream, error }`・`Wait`・`Consumer(E)` |
| `RunFailure<E>` | `cause`、任意の `status`、入力の送信量と早期閉鎖、`kill_error`・`wait_error`、`cleanup_expired`、`pending_child: Option<Child>` |

### 入出力・予算・期限

- consumer(受け取ったバイト列を処理する呼出元の関数)へ非空の出力だけを渡します。C0aの `CommandIo` へ直接書き、途中の `flush` も使えます。失敗は `Consumer(E)` として返します。
- 同じC0bの `&mut Budget` を渡します。共通部は項目数・総量・1ファイルの量を加算しません。stdout/stderr別の上限、計数順、出力の保持と破棄はconsumerが決めます。受理した途中出力と消費量は失敗後も戻しません。
- 起動前の順序は不正な期限→取消→実行残時間です。実行期限は `Budget::deadline()` と `execution` の早い方です。下位で予算・期限を作り直したり延長したりしません。
- 3本を待たずに読み書きできる設定にします。`poll`の待ちは残時間と20msの短い方です。1周回にstdin・stdout・stderrを各1操作、最大65536bytesずつ進めます。
- 各出力内のバイト順序を保ちます。stdoutとstderrをまたぐ順序は定めません。子の回収と全パイプの終了を確認してから成功を返します。
- 数値0以外や信号終了でも、入出力と回収が完了すれば `Ok` になり得ます。用途別の成否と最終の終了コードは呼出元が決めます。

### 失敗時の停止と未回収の所有権

```text
実行失敗 -> 親側3パイプを閉じる -> 未回収なら終了確認
  生存を確認 -> 直接子へKILLを1回 -> 固定のcleanup期限内で回収
  非再試行のwait失敗 / 回収期限切れ -> 最初の原因と未回収Childを返す
```

- 回収済みの子へ信号を送りません。最初の原因と、後の `kill_error`/`wait_error` を分けて保持します。KILL失敗後に終了を確認できても、成功へ変えません。
- cleanup期限が既に切れていても、初回の非待機確認、必要なKILL、直後の非待機確認だけは行います。待機後・再試行前は絶対期限を確認します。回収不能なら `cleanup_expired=true` と未回収の所有権を返します。
- `try_wait` は `EINTR`/`EAGAIN` だけを期限内で再試行します。それ以外の失敗後は、残るパイプの閉鎖以外のOS操作を追加しません。通常実行中なら `cause=Wait` と `wait_error` に同じエラー情報を残します。cleanup中なら元の原因を保ちます。
- `pending_child` は親側のパイプを閉じた `std::process::Child` です。呼出元が保持し、明示的に追加回収してください。型の破棄だけで回収済みにはなりません。非再試行のwait失敗で返す `cleanup_expired` は、その時点で期限へ達したかを示します。
- 孫が出力パイプを保持すると、元の実行期限で失敗します。直接子の終了状態は保持します。パイプを閉じて生きる孫は、直接子の成功後も残り得ます。
- 期限と取消は処理の合間に確認します。consumerの停止・panic、停止中のOS呼出し、実行順の遅延を強制中断しません。親死亡後の停止・回収も保証しません。
- 親のSIGTERM/SIGINT/SIGHUPの捕捉・再送、他の回収処理やSIGCHLDの自動回収との競合は対象外です。プロセスグループ(信号をまとめて送れるプロセスの集まり)と子孫全体の監督はR4Qへ残します。
- YAML解析・機密指定・状態保存・走査へは接続していません。取得済みbytesを扱うR4Sの純粋部品は下に記します。#454の用途別の入出力上限・検査順序は後続の呼出元が受け持ちます。

設計判断は [design.md](../docs/design.md#単一子の実行と回収c0c)、専用比較と未確認条件は [testing.md](../docs/testing.md#単一子の実行と回収の検査c0c)にあります。

## loop-state用の純粋部品(R4S)

`dev_workflow_core::loop_state`は、渡された入力だけでYAML解析・機密指定・glob(パスのパターン)照合を行います。
ファイル・Git・環境・時刻を読み取らず、外部Pythonを起動しません。

| 公開API | 入力と結果 |
|---|---|
| `CodepointString::try_from_codepoints(Vec<u32>)` | Unicodeの文字番号を保持。0〜0x10FFFF以外は`InvalidCodepoint { index, value }` |
| `CodepointString::as_codepoints()` | 保持した`&[u32]`を返す。`From<&str>`も使える |
| `LegacyIntLimit::from_python_value(i64)` | 0、または640〜2,147,483,647を受ける。無効値は`InvalidLegacyIntLimit { value }` |
| `profile_patterns(raw: &[u8], int_limit: LegacyIntLimit)` | 取得済みUTF-8 bytesを解析し、`Result<Vec<CodepointString>, ProfileError>`を返す |
| `canonical_secret_patterns(&PatternInput)` | 指定の型とglobを検査し、同じ結果型で既定3件と初出順の指定を返す |
| `default_secret_patterns()` | `.env`・`.env.*`・`.dev.vars`を順に返す |
| `secret_path(&CodepointString, &[CodepointString])` | パスが指定のいずれかに一致するかを`bool`で返す |

- `CodepointString`の内部は非公開の`Vec<u32>`です。surrogate(UTF-16の代用符号の値)も1個ずつ保持します。空列やNULは文字型では受け、機密指定の検査で判断します。JSONを通して2個を1文字へ合成しません。
- `PatternInput`は`NotList`または`List(Vec<PatternItem>)`です。要素は`Text(CodepointString)`と`Other`に分けます。全要素の型検査を終えてからglobを検査します。
- `ProfileError`は解析・構築失敗の`UnreadableYaml`、配列や文字列の型違いの`NotStringList`、不正な指定の`InvalidGlob`です。`message()`は順に`secret_paths を安全に読めない`、`secret_paths が文字列配列でない`、`secret_paths に不正な glob がある`を返します。
- `LegacyIntLimit`の0は制限なしです。呼出元が旧Pythonの整数変換設定を渡します。環境を読む既定構築や、新しい4300桁制限はありません。
- `profile_patterns`は全ノードの明示重複を構築前に検査します。続いて旧SafeLoaderの構築順序とmergeを再現します。SafeLoaderが構築しない子まで一律に拒否しません。
- `secret_path`は指定の正規化や機密指定の検査を加えません。大小文字はCPython 3.10・Unicode 13.0.0の固定表を使い、LFと`/`の扱いも旧版のままです。
- 唯一の承認済み差は、Pythonの再帰上限だけによる拒否です。有限な入力をRustの反復処理では受け付けます。新しい深さ・容量上限は設けません。scalarの自己参照や未知の失敗は許可しません。
- ファイルの不在判定、C0b/C0cへの接続、S2Lの全7コマンド・状態保存・走査、R4A/B・R4Q、他用途の移植は含みません。公開CLI・配布物・呼出元・版・`CHANGELOG.md`も変更していません。

調査元は [Issue #454](https://github.com/Yuki-Maeda-valour/workflow/issues/454)、実装契約は [Issue #462](https://github.com/Yuki-Maeda-valour/workflow/issues/462)です。
構文・順序・globの規則と承認の範囲は [design.md](../docs/design.md#loop-state用の純粋部品r4s)、必須比較と未確認条件は [testing.md](../docs/testing.md#loop-state用の純粋部品の検査r4s)にあります。

## 本文の正規化と確認用の値(R4A task-digest)

#460で、旧`task-digest.py`と同じ結果を返す専用部品を実装しました。
製品CLIの登録・help・配布・既存の呼び出し元には接続していません。

- `crates/cli/src/commands/task_digest/normalize.rs`の`body`が正規化本文を返し、`digest`が`sha256:`とSHA-256の先頭16桁を返します。
- 同じ専用モジュールの`run`は`CommandIo`から入力を借り、`CommandOutput`を返します。`execute_to`はC0aを入力専用に使い、実出力は`output::emit_fd_to`へ渡します。共通の出力処理へ替えないでください。
- `platform::task_digest::RegularPath::inspect`はリンクを辿って通常ファイルを確認します。`open`は待たずに開いて同じfdの種類を調べ、`RegularFile::read_chunk`はその実体だけを読みます。
- `stdout_columns`はstdoutの端末幅を調べます。`is_linux_full`は実際のwriterと同じfdの種類・デバイス番号を調べます。パスを開き直しません。
- `COLUMNS`はPythonの整数変換の桁数上限を含めて判定します。有効な`PYTHONINTMAXSTRDIGITS`の0または640以上を反映し、既定は4300です。
- 特殊な入出力3条件だけ、終了1と空stdoutを維持して旧例外の詳細を固定診断へ変えます。通常の出力失敗は終了120、stderr書込み失敗はバッファなし時1を保ちます。
- [専用テスト](crates/cli/tests/task_digest.rs)だけが専用部品を参照します。`cargo test --locked`の通常起動でPython比較と自己検査へ必ず進みます。

正規化規則、ファイルの保証範囲、限定差の厳密な条件は [design.md](../docs/design.md#本文の正規化と確認用の値r4a-task-digest)にあります。
件数と手元Linux実測、4機種の必須経路、未確認条件は [testing.md](../docs/testing.md#task-digestの比較r4a)にあります。

## origin判定とGit設定の確認値(R4B)

#461で旧Python2本に対応する専用モジュールを実装しました。製品CLIの登録・help・配布・既存呼出元は変えていません。

- `crates/cli/src/commands/origin_repo::run`は、`--dir`を受けて7項目のorigin判定を返します。Gitは無期限、`ssh -G -- git@github.com`だけ10秒です。接続は行いません。
- `crates/cli/src/commands/git_config_digest::run`は、設定の所在・種類・リンク字面・順序付き項目のASCII JSONからSHA-256を作ります。各Gitは10秒です。
- `--expect`一致は無出力・終了0、不一致は既存診断・終了1、不正形式と読取り失敗は終了2です。
- `crates/cli/src/commands/r4b_support/`は専用の引数・文字化・JSON・Git引数を持ちます。ファイル操作とGitへ渡す引数・環境の生バイトは保ちます。
- `platform::r4b_process::capture(&mut Command, Option<Duration>)`は、`Captured { code, stdout, stderr }`または`Failure::{Timeout, Io}`を返します。信号終了は負の番号です。
- stdinは`/dev/null`です。時間切れでは直接子だけをkill/waitし、親のパイプを閉じます。孫のEOFや読取りスレッドを待ちません。子孫全体の停止を保証せず、C0cへ一律に接続しません。
- `platform::r4b_process::metadata`・`read_link`が生パスで不在・種類・リンク字面を調べます。C0bのリンク拒否・実体保持・再検査は追加しません。
- 有効な`COLUMNS`を優先します。無効・0・負値・未設定なら`stdout_columns`で実端末幅を調べ、取得失敗時だけ80へ戻ります。
- `crates/cli/examples/r4b_contract.rs`が`args_os()`からコマンド名を除いて各`run`へ渡します。最終出力はC0aの`emit`へ渡します。`R4B_OUTPUT_FAULT`はこの検査入口だけの故障注入です。

承認済みの差は [設計のR4B節](../docs/design.md#r4bに限る承認済みの差)に限ります。
originのGitの不正UTF-8は空stdout・終了1・固定診断、SSHの不正UTF-8は従来の拒否JSONです。
書き込み/flush故障はC0a・終了1で、途中出力と診断先故障を分けます。
Pythonの文字コード設定による引数・表示幅・パス材料等の解釈差はUTF-8＋`surrogateescape`へ固定し、標準出力と標準エラーは常にUTF-8です。
originのGit/SSH出力は厳密なUTF-8のままで、旧版の通常比較を緩めません。

[必須の実行手順と検証範囲](../docs/testing.md#r4bの比較)に従ってください。
通常の`cargo test --locked`だけではexampleの単体検査と旧版比較は完了しません。

## タスク本文の復元の内部部品(R4R)

#473で旧helper `restore-taskmd.py` と同じ結果を返す部品を実装しました。製品CLIの登録表・help・配布・既存呼出元(`implement-guard.sh` など)は変えていません。
段階3の `implement-guard restore-taskmd`(S3I-A #474)が同じプロセスで呼ぶ予定です。

- `crates/platform/src/restore_taskmd.rs` は、保持した親の fd を使う `openat`・`fstatat`・`mkdirat`・`unlinkat`・`renameat` などを、1回だけ呼ぶ安全な関数で公開します。`EINTR` を再試行するかは呼出元が決めます。
- 同じファイルは、TERM・INT・HUP の最初の1つだけを記録する処理、3つの信号の mask、`SIGXFSZ` の無視、子への信号、OSの乱数と `/dev/urandom` への切り替え、`/dev/null` の `O_RDWR` の1回の open、環境の `LC_CTYPE` の書き換えを持ちます。
- 子の起動は `spawn(program, args, devnull, ChildKind)` です。`ChildKind::Version` は stdout だけをパイプにします。`ChildKind::Copy` は2本の fd を同じ番号で継がせ、`RLIMIT_FSIZE` を soft・hard とも N にします。
  - 子の中では fd を閉じません。`close_range` → 親で列挙した fd → `sysconf(_SC_OPEN_MAX)` までの番号の順に、exec の時に閉じる印を付けます。最後に、親で用意した argv で `execv` を呼びます。
  - 子の準備と exec の失敗は、起動の失敗として返します。`SIGCHLD` を無視した起動で std が panic しても、表示を出さずに起動の失敗として返します。
- `crates/cli/src/commands/restore_taskmd/` は、引数(`args.rs`)・`--cp` の解決(`realpath.rs`)・本体(`operation.rs`)・子の状態(`child.rs`)・応答と書き出し(`response.rs`)を持ちます。
  - `mod.rs` の先頭に、旧helperの関数との対応表と、`EINTR` の扱いの表があります。cli 側に `unsafe` はありません。
- S3I-A が使う入口は次のとおりです。
  - `start()` は、引数の検査の前に `SIGXFSZ` を無視し、`LC_CTYPE` を書き換えます。ほかのスレッドが無い時に1回だけ呼びます。
  - `parse_request(&[OsString]) -> Result<Request, ArgumentError>`、`Restore::new(Request)`(300秒の期限の起点)、`Restore::run() -> (u8, Vec<u8>)`、`checkpoint_before_output()`、`handlers_installed()` です。
  - 書き出しの1段目 `emit(&mut Output<W>, Option<&mut Restore>, u8, &[u8]) -> (u8, Emitted)` は、TERM・INT・HUP を止めて応答を確定し、書き込みと明示の flush を行い、mask を戻します。
  - `Emitted` は、書けた・溜めた物が残った・直接の書き込みで失敗した・mask を設定できない、を分けます。
  - 2段目は、`exit_flush` が Python の終了時の (1)(2) の flush を行い、(2) の失敗を返します。呼出元は `unraisable_lines` の2行を `write_stderr_lines` で出し、終了値を120にします。その後に `exit_close` が (3) 処理を既定へ戻すことと、(4) 閉じる時の2回の flush を行います。
  - 呼出元は、継承可能な fd を増やしてから呼びません。TERM・INT・HUP を止めた mask のままでも呼びません。
- `crates/cli/examples/r4r_contract.rs` は検査専用入口です。`args_os()` からプログラム名を除いた argv を、旧helperと同じ形で受けます。故障を注入する環境変数や、時間を縮める入口はありません。
- Rust の単体テストは、Linuxで57件(Macは26件)です。`cargo test --locked -p dev-workflow-cli --example r4r_contract` だけで流れます。Linuxだけの31件は `crates/cli/src/commands/restore_taskmd/tests.rs` の `#[cfg(target_os = "linux")]` の件です。旧テストのうち内部を差し替える46件の対応先は、`native/ci/r4r_test_map.json` にあります。

認めた差は、2026-11-12 02:41の決定の`__CF_USER_TEXT_ENCODING`の1件だけです。Macの旧Pythonが子の環境に足す変数を、比較から外します。Rustはこの変数を足しません。
契約と、R4Rでは比べない4件は [設計のR4R節](../docs/design.md#タスク本文の復元の内部部品r4r)にあります。
[比較の手順と件数](../docs/testing.md#r4rの比較)に従ってください。通常の`cargo test --locked`だけでは、exampleの単体検査と旧版比較は完了しません。

## 診断の reftable の読み取り(R4D-1)

#481 で、旧 Python の `diagnostic-reftable.py` と診断の予算の型に対応する内部部品を実装しました。reftable は、Git が参照(ブランチ名などと、その指す先)を表の形のファイルに保存する形式です。table はその 1 つのファイル、stack は `tables.list` に並んだ table を古い順に重ねた全体です。
製品 CLI の登録・help・配布・既存の呼出元は変えていません。呼出元はまだありません(R4D-3 で使います)。契約は [設計のR4D-1節](../docs/design.md#診断の-reftable-の読み取りr4d-1) にあります。

- `crates/cli/src/commands/diagnostic_git/budget.rs`
  - `Budget`: `new()`(期限 300 秒)・`check()`・`charge(Kind, u64)`・`remaining()`・`stop_flag()`・`request_stop()`・`used(Kind)`。
  - `Kind`: 9 種類の量。`name()`・`limit_reason()`・`default_limit()`・`from_name()` を持ちます。
  - `Failure { reason, code }`: 理由の字面と終了値です。
- `crates/cli/src/commands/diagnostic_git/reftable.rs`
  - `read_stack(path: &OsStr, hash_format: &str, budget: &mut Budget) -> Result<Stack, ReadError>`: パスは相対パスも受けます。ハッシュの形式は `sha1` か `sha256` です。
  - `Stack`: 旧版の dict と同じ並びで、`iter()`・`get()`・`len()` を持ちます。`Entry { name, oid, target }` の名前と指す先は bytes です。
  - `ReadError`: `Reftable(Failure)`・`Budget(Failure)`・`Other("ValueError")`(パスに NUL を含む)の 3 種類です。
- `crates/platform/src/fs.rs` に足した部品
  - `lstat_path(path)`: 最後の段だけリンクを辿らずに調べます。
  - `PlainFile`: `snapshot()`・`read_at(offset, buf)`・`close()` を持ちます。
  - `HeldDir::open_path`・`snapshot`・`open_plain`・`close`: `close` は閉じる失敗を返します。
  - 自作の `unsafe` は `close_owned` の 1 か所です。

`diagnostic_git` は `commands/mod.rs` に登録していません。検査は `tests/diagnostic_reftable.rs` が `#[path]` で読み込みます。

必須の比較は [テストの手順](../docs/testing.md#r4d-1-の-reftable-の比較) に従ってください。
比較のテストには `#[ignore]` の印を付けたので、通常の `cargo test --locked` では流れません(結果は `ignored`)。比較は、次のコマンドか `build.py r4d1` で流します。

```sh
(cd native && cargo test --locked -p dev-workflow-cli --test diagnostic_reftable -- --ignored --exact rust_matches_the_legacy_reftable_reader_on_every_input)
```

比較は、reftable を作れる git(2.45 以上)と Python 3.10 系・root 以外を使います。

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
`Cargo.lock` は共有します。直接依存は `sha2`・`serde`・`serde_json`・C0bの `rustix`・R4Rの `libc 0.2.191`(`platform` だけ)と、R4Sのローカル部品 `workflow-yaml-parser 0.1.0`です。
`rustix` は `=1.1.6` に固定し、`platform` だけで `fs`・C0cの `event`・task-digestの`termios`と既定の `std` を使います。子の起動・KILL・回収は `std::process` です。`rustix` の `process`・ネットワーク機能は有効にしません。
R4Rも子を `std::process::Command` で起動します。ただし、子だけの `RLIMIT_FSIZE`・余分な fd を継がせない印・子の信号の既定と mask・旧版と同じ `execv` は、exec の前に子の中で動く処理が要ります。そのため `platform` の `restore_taskmd` だけが `pre_exec` と `libc` を使います。最初の信号を記録する処理と mask、Mac の地域設定の判定、環境の `LC_CTYPE` の書き換えも、同じファイルの `unsafe` に置きます。`rustix` の採用の根拠と、R4Rが `libc` と `unsafe` を使う理由は、[設計の記録](../docs/design.md#c0b-で採用した-os-呼出しの部品)にあります。
R4D-1(#481)は、閉じる失敗を返すために `platform` で `try_close` 機能も有効にしました。自作の `unsafe` は `platform/src/fs.rs` の `close_owned` の 1 か所です。理由は [設計のR4D-1節](../docs/design.md#診断の-reftable-の読み取りr4d-1) にあります。
YAML部品は `saphyr-parser 0.1.2`の局所修正版です。`arraydeque =0.5.1`・`thiserror =2.0.21`を固定し、ライセンス原文を保持します。[出典と更新手順](crates/yaml-parser/UPSTREAM.md)に従い、自動で上流の新版へ替えないでください。
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

`cargo test --locked` は、C0bの実ファイル比較、C0cの実子比較、R4Sとtask-digestの固定期待・旧Python・Rustの比較も実行します。`python3`がPython 3.10系であり、root以外の利用者であることが必須です。
事前にリポジトリ直下で、同じPythonを使い `python3 -m pip install -r requirements-dev.txt` を実行してください。R4SはUnicode 13.0.0とPyYAML 6.0.3を要求します。
専用比較を省略して合格にはしません。Mac実機と最低OSでのC0b/C0c/R4Sの動作は未確認です。R4SのLinux実装検証と、未実行のPR用必須CIは [検査記録](../docs/testing.md#loop-state用の純粋部品の検査r4s)で区別しています。

release の設定は `codegen-units=1`・`opt-level=3`・`lto="fat"`・`strip="symbols"`・`debug=0`・`incremental=false` です。
panic の設定は既定のままです。Linux のリンカーは Rust の musl 向けの既定を使います。
`.cargo/config.toml` と `.cargo/rustc-wrapper.sh` がソース・依存・出力先の絶対パスを固定名へ置き換えます。
wrapper は bash 3.2 以上で動きます。Cargo が渡す `CARGO_BIN_EXE_dev-workflow` もコンパイラへ引き継ぎます。
CI(変更ごとの自動検査)から `RUSTFLAGS` や最適化設定を渡す必要はありません。
外部の設定や環境変数でビルドの設定を上書きしないでください。
再現性の比較は同じ道具と設定を使い、別々の空の `CARGO_TARGET_DIR` で作った最終実行ファイルに `cmp` を使います。

テストでは、一時リポジトリの `native/` の外のファイルを変更し、計算値が不変であることを確認します。
追跡した本文・パス・Git のモードの変更では計算値が変わることも確認します。
