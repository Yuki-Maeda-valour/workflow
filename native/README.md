# Rust の土台と共有の約束

このフォルダには、段階 0a(#442)の土台、段階 1a(#448)の保存先解決、C0a(#453)の共通入出力、C0b(#456)の上限と保持実体の検査、C0c(#458)の単一子の実行と回収を実装しています。
R4Sのうちloop-state用YAML・機密指定・globの純粋部品は、#454の調査を基に#462で追加しました。
S3I-A(#474)の `implement-guard.sh` のGitを呼ばない3コマンドと記録の読み取りも、開発用ソースだけにあります(未登録・未配布)。
`resolve-task-dir` は段階 1b で配布済みです。C0a・C0b・C0c・R4S・R4Aのtask-digest・R4Bのorigin判定とGit設定の確認値・R4Rのタスク本文の復元の部品・S2Lのloop-stateの状態観測7コマンド・R4Gの保護記録の探索とreviewsの配置と内蔵のGit読取りの部品は開発用ソースだけにあり、配布は段階 1b・プラグイン v4.53.0 のままです。C0b/C0c/R4S/task-digest/R4B/R4R/S2L/R4Gは既存のCLIへ未接続です。
R4U(#480)の文書のリンク検査の部品も開発用ソースだけにあり、既存のCLIへ未接続・未配布です。
R4Aの残り5本(#471)の解析補助も開発用ソースだけにあり、既存のCLIへ未接続・未配布です。
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
- `crates/platform`: OS 固有の処理を置く境界です。C0bの保持実体と属性検査、C0cの直接子とパイプの操作、R4Rのタスク本文の復元が使うOSの操作(`restore_taskmd`)、S2Lの保持入力・保存・直接子の接続(`loop_state`)と起動時の信号の初期化(`loop_state_signal`)、Linuxのテスト用のopenの監視(`loop_state_watch`)を持ちます。
  - S3I-Aの検査専用入口が使うOSの操作(`implement_guard`)も持ちます。
  - R4Gの起動前の状態の記録・fd相対の読取り・旧版と同じ子の実行(`r4g`)も持ちます。
- `crates/yaml-parser`: R4S用の固定したYAML解析部品です。上流の版と局所修正は [UPSTREAM.md](crates/yaml-parser/UPSTREAM.md)にあります。
- `platform` 以外の自作 crate(Rust の部品単位)は `#![forbid(unsafe_code)]` を使います。
- `platform` は `#![deny(unsafe_op_in_unsafe_fn)]` を使います。公開する関数は安全な関数に限ります。
  - 例外は、R4U の `check_links::capture_before_main` と、R4G の `r4g::startup::record_before_std`(`#[doc(hidden)]`)と、S2E の `environment_guard::capture_inherited_sigpipe`(`#[doc(hidden)]`)です。いずれも main の前に static から呼ぶ形でだけ使います([R4U の節](#r4ucheck_linksの文書のリンク検査))。

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

## loop-stateの状態観測7コマンド(S2L)

#468で、旧`loop-state.py`の7コマンドに対応する専用モジュールを実装しました。製品CLIの登録・help・配布・`loop.sh`などの既存呼出元は変えていません。

- 未登録で試す入口は`crates/cli/examples/loop_state_fixture.rs`だけです。`args_os()`の最初の要素(実行ファイルの名前)を除いて、`commands/loop_state/mod.rs`の`main`へ渡し、その終了値で終わります。
- 製品の`dev-workflow`は、引き続き`loop-state`を未登録のコマンドとして拒否します。

| 置き場 | 主な部品と役目 |
|---|---|
| `crates/cli/src/commands/loop_state/mod.rs` | 7コマンドの振り分けと、`Stop`(終了20)・捕まえない例外(終了1)の出し分け |
| 同`args.rs`・`runtime.rs`・`output.rs` | 旧`argparse`の解析とhelp。文字コードをUTF-8と`surrogateescape`へ固定する`LegacyRuntime`。書き込みの途中と終了時のflushの失敗を分ける出力 |
| 同`json.rs`・`error.rs` | `PyValue`・`PyInteger`(文字番号・任意精度整数・非有限数を保つJSONの値)。`LegacyException`(例外の型・理由・因果・フレームと、旧版と同じ形の呼出履歴) |
| 同`state.rs`・`legacy_file.rs`・`budget.rs` | 状態の読取りと保存、保持した親からのopen。`TextIOWrapper`と`BufferedWriter`の区切り。旧`Budget`の計数順 |
| 同`snapshot.rs`・`preflight.rs`・`config.rs`・`scan.rs` | 状態の観測の順序、管理先・候補・出典の事前の検査、設定のグラフ、属性を先に調べる走査と機密指定 |
| 同`control.rs`・`compare.rs` | `stop-control`・`derive-removed-worktree`と、`compare`の差の診断 |
| 同`process.rs`・`interrupt.rs`・`legacy_tempfile.rs` | `run_git`と`index_snapshot`、SIGINTの後始末、`onbranch`の一時の置き場(`tempfile.mkdtemp`と同じ候補と名前) |
| 同`realpath.rs`・`walk_recursion.rs`・`walk_regex.rs` | `posixpath.realpath`と、走査・glob照合の旧CPythonの呼出しの深さ |
| `crates/platform/src/loop_state.rs` | `LoopChild`(stdoutだけパイプの直接の子)、`MemoryFile`(memfd)、`InheritedInput`・`LoopArg`・`MemoryPart`、保持した親からの列挙・リンク・保存 |
| `crates/platform/src/loop_state_signal.rs` | 起動時の信号の初期化(SIGINT・SIGXFSZ)と、起動の失敗の診断に使う主スレッドの識別子 |
| `crates/platform/src/loop_state_watch.rs` | Linuxの実際のopenの監視。テストが持つフォルダだけを見る |
| `crates/core/src/loop_state/legacy_recursion.rs`・`legacy_parser.rs`・`legacy_scanner.rs` | `LegacyRecursionContext`と`profile_patterns_for_loop`。PyYAMLのcompose・構築とCPython 3.10の呼出しの深さを数える |

- fdの番号・所有権・読取り位置は、`platform`の外へ出しません。`/proc/self/fd/<番号>`の組み立ても`platform`の中に閉じます。
- `crates/platform/src/fs.rs`へ足したのは、`pub(crate)`のfdの借用と、openとfstatの後に旧版の比較の呼出しを挟む`pub(crate)`の入口です。既存のC0b・C0cのAPIの動作は変えていません。
- 保持したフォルダの属性は、R4D-1(#481)が足した`HeldDir::snapshot`で取ります。fstatがEINTRで中断されたら呼び直します(旧版の`os.fstat`も呼び直します)。
- 既存の`core::loop_state::profile_patterns`(R4S)の結果は変えていません。S2Lは`profile_patterns_for_loop`を使います。
- 単体テストは`*_tests.rs`と各モジュールの`tests`にあります。`check_loop_state_units.py`が必須の名前と件数で照らします。

契約・拒否の順序・承認された差は [design.md](../docs/design.md#loop-stateの状態観測7コマンドs2l)にあります。
[比較の手順・件数・時間](../docs/testing.md#s2lの比較)に従ってください。通常の`cargo test --locked`だけでは旧版との比較は完了しません。

## R4U(check_links)の文書のリンク検査

#480で、旧版 `plugins/dev-workflow/skills/update-doc/scripts/check_links.py` と同じ結果を返す専用の部品を実装しました。
製品CLIの登録表・help・配布・既存の呼び出し元は変えていません。未登録・未配布です。
製品の実行物(`dev-workflow`)には入りません。検査用の実行物だけが `#[path]` で読み込みます。

### 入口

- `crates/cli/src/commands/check_links::run(args: &[OsString]) -> ExitCode` が入口です。
  - `python3 check_links.py <引数…>` で旧版を起動したときと同じ動作をして、終了値を返します。`args` にプログラム名は含めません。
  - 出力は、実行の途中で、Python と同じ動きをする書き手(`pyio`)が書きます。C0a の `CommandIo`・`execute_to`・`emit_to` は使いません。
  - SIGINT で中断したときは、既定の動作に戻して自分へ SIGINT を送ります。それでも終わらなければ130を返します。
  - main の前の控え(下の `capture_before_main`)が無いときは、何も書かずに panic します(呼出元の誤り)。
- 同じフォルダの部品は次のとおりです。

| モジュール | 受け持ち |
|---|---|
| `legacy` | 旧版の `main`・`iter_md_files` を同じ順で行う |
| `fsops` | pathlib・posixpath を通す OS の呼び出し(`is_dir`・`is_file`・`exists`・`rglob`・`read_bytes`・`resolve`) |
| `matcher` | 2種類の照合(`links`・`code_paths`・`line_number`・`link_target`・`code_skipped`)。一般的な正規表現は使わない |
| `path`・`posix` | `PurePosixPath` の整え方・`parent`・`parents`・並べ方と、posixpath の `isabs`・`join`・`split`・`normpath` |
| `text` | `errors="replace"` の UTF-8 の読み取りと改行のそろえ方(`decode`) |
| `oserror` | errno から選ぶ例外の種類と、Linux の glibc の文の固定表 |
| `traceback`・`display` | 呼出履歴の定型(固定名と本家 3.10.11 の行番号)と、呼出履歴・`Exception ignored`・起動の失敗の診断の書き方 |
| `pyio` | `sys.stdout`・`sys.stderr` に当たる書き手(`TextIOWrapper` と `BufferedWriter` と同じ溜め方) |
| `startup` | 起動時の環境変数の読み方(`PYTHONHASHSEED`・`PYTHONINTMAXSTRDIGITS`・`PYTHONUNBUFFERED`・`PYTHONOPTIMIZE`) |

- `crates/cli/src/commands/resolve_task_dir/bytes.rs`(surrogateescape の符号位置と repr)と `crates/cli/src/commands/resolve_task_dir/unicode.rs`(`str.isprintable` の表)を `#[path]` で読み込みます。元のファイルは変えていません。

### platform の部品

`crates/platform/src/check_links.rs` は、本家 3.10.11 の旧版と同じ OS の呼び出しを、1回ずつ行います。

- ファイル: `stat`・`lstat`・`readlink`・`getcwd`・`scandir`・`open_read`(`ReadFile::meta`・`isatty`・`tell`・`read`)・`fstat_std`・`isatty_std`・`os_strerror`(Mac だけが使う)・`write`。
  - パスは、文字列を連結した完全なパスで受けます。ディレクトリの fd から辿る呼び出し・リンクの拒否・量の上限は加えません。
  - 開く・読む・書くは EINTR を自動で繰り返しません。呼出元が、Python と同じ所で SIGINT の印を見て、繰り返すかを決めます。
  - 大きさと回数も旧版に合わせます。`getcwd` は1024バイトずつ広げます。`readlink` は MAXPATHLEN(Linux 4096・Mac 1024)で1回だけ呼びます。一覧は、Linux が glibc の opendir と同じ大きさの `getdents64`、Mac が libc の `opendir`・`readdir` です。
  - `write` は libc の `write` を1回だけ呼びます。stdout と stderr への書き込みは、すべてこれを通します。
- 信号: `ignore_sigpipe_and_sigxfsz`・`install_sigint`・`take_interrupt`・`interrupt_pending`・`finish_sigint`・`exit_by_sigint`・`thread_ident`(`pthread_self` の値)。
  - `install_sigint` は、起動時に SIGINT が既定の動作のときだけ、印を立てる処理を `SA_RESTART` なしで置きます。
- main の前の控え: `capture_before_main`・`captured_before_main`・`sigpipe_ignored_at_start`・`std_valid_at_start`・`restore_start_sigpipe`。
  - `capture_before_main` は、親から受け継いだ SIGPIPE の扱いと、fd 0〜2 が使えるか(Python の `is_valid_fd` と同じ方法)を読むだけです。扱いも fd も errno も変えません。
  - platform が公開する関数のうち、`unsafe` なのはこの関数と、R4G の `r4g::startup::record_before_std` と、S2E の `environment_guard::capture_inherited_sigpipe` だけです(いずれも `pub unsafe extern "C" fn`)。直接は呼ばず、main の前に static から呼びます。
- `rustix` に無い `sigaction`・`kill`・`getpid`・`pthread_self` などと libc の `write` は、このファイルの中で `unsafe extern "C"` として宣言し、安全な関数で包みます。`libc` の crate は使いません。

### main の前に呼ぶ static

入口を持つ実行物は、main の前に `capture_before_main` を呼ぶ static を置きます。今は検査用の実行物 `crates/cli/tests/check_links.rs` だけが置いています。
Rust の起動の処理は、main の前に SIGPIPE を無視にし、閉じた fd 0〜2 へ `/dev/null` を開きます。その前に控えるためです。
製品へつなぐときは、製品の入口にも同じ static が要ります(2026-11-12 04:20 の決定)。08:55 の決定は製品の入口(`main.rs`)を変えないと定めるので、つなぐときに照らし、止まって聞きます([設計の R4U 節](../docs/design.md#r4ucheck_linksの文書のリンク検査))。

```rust
#[cfg(target_os = "linux")]
#[allow(unsafe_code)]
#[used]
#[unsafe(link_section = ".init_array")]
static CAPTURE_BEFORE_MAIN: unsafe extern "C" fn() = capture_before_main;

#[cfg(target_os = "macos")]
#[allow(unsafe_code)]
#[used]
#[unsafe(link_section = "__DATA,__mod_init_func,mod_init_funcs")]
static CAPTURE_BEFORE_MAIN: unsafe extern "C" fn() = capture_before_main;
```

### 検査用の実行物

- `crates/cli/tests/check_links.rs`(`harness = false`)は、引数で動きを変えます。
  - `--check-links-fixture <引数…>`: 実際の標準入出力で、検査対象として `run` を呼びます。
  - `--check-links-matcher <生成入力の JSON>`: 2種類の照合を全件に当てた結果を JSON で出します。
  - `--r4u-compare`(`cargo test --locked -p dev-workflow-cli --test check_links -- --r4u-compare`): `native/ci/check_r4u.py --fixture <自分>` と `native/ci/test_r4u.py` を同時に起動します。どちらかの失敗・未実行・時間切れ(全体900秒)でテストを落とします。
  - 引数なし(`--r4u-compare` を付けない `cargo test --locked`。CI の `build`): 比較を流さず、`R4U の比較は流していない: ` で始まる1行(流し方を添える)を出して終了0にします。この1行は比較の合格に数えません。
  - ほかの引数は受け付けません。
- `crates/cli/tests/check_links_units.rs` は、文字列だけの部品を固定期待(`native/fixtures/r4u/` の `units.json`・`linux.json`・`matcher-corpus.json`)と比べます。18件です。

契約と全コマンド共通の決定の当て方は [設計の R4U 節](../docs/design.md#r4ucheck_linksの文書のリンク検査)にあります。
[比較の流し方と件数](../docs/testing.md#check_links-の比較r4u)に従ってください。流し方の要点は、[ビルドと検証](#ビルドと検証)の R4U の段落にあります。
## Markdown の表の XLSX 出力(R4E)

#482で、旧スクリプト `md_tables_to_xlsx.py`(export-doc が使う、Markdown の表を XLSX にするスクリプト)と同じ結果を返す専用モジュールを実装しました。
製品CLIの登録表・help・`launch.sh`・配布・既存呼出元(export-doc の `SKILL.md`)は変えていません。製品の `dev-workflow md-tables-to-xlsx` は「未知のコマンド」で終了2のままです。製品への接続は、後の配布の段(D4)で行います。

`crates/cli/src/commands/md_tables_to_xlsx/` のファイルと役割は次のとおりです。

| ファイル | 役割 |
|---|---|
| `mod.rs` | 入口 `run(&[OsString], &StartupState) -> ExitCode` と、旧版の `main()` の順(引数 → 入力 → 出力先の事前検査 → ブック → 親の作成 → ZIP → `OK` の行)。届いた SIGINT を確かめる所の一覧も、ここにあります |
| `args.rs` | Python 3.10 の argparse と同じ解析・誤りの出し方・`--help`。折り返しの幅は `COLUMNS` → 標準出力の端末の幅 → 80 から 2 を引き、textwrap の規則で折り返します |
| `markdown.rs` | 入力の `is_file()`・UTF-8 の厳密な読み取り・行の分割・表の取り出し。Python の `re` の意味を、正規表現の部品(regex の crate)を使わない手書きの照合で保ちます |
| `sheet_name.rs` | 旧版の `unique_sheet_name` と、openpyxl 3.1.5 の `avoid_duplicate_name`(大文字小文字を区別しない重複の番号) |
| `paths.rs` | 出力先の事前検査(`output_paths`・`physical_directory`・`posixpath.realpath`・`Path.resolve` など)・Python の再帰の上限の深さ・親の作成(`Path.mkdir`) |
| `workbook.rs` | ブックの組み立て。セルの型・32767 文字の切り詰め・制御文字と 18279 列の誤り・列幅・シート名の `UserWarning` |
| `xml.rs` | 各部分の XML のバイト(属性と文字のエスケープ・サロゲートの `&#NNNNN;`)。`app.xml` の文と書式は openpyxl から転記しました |
| `zip.rs` | ZIP の書き出し(CRC-32・DEFLATE の非圧縮ブロック・中央ディレクトリ・ZIP64 の規則)・出力先の開き方・書き込みの失敗と `ZipFile.__del__` の呼出履歴 |
| `pyio.rs` | Python の標準出力と標準エラーの溜め方・`print`・起動を変える環境変数・例外の表示・終了処理 |
| `errors.rs` | 例外の型と文・`repr`・errno の文の表(Linux は glibc の文、Mac は OS の文)・呼出履歴の段の表(固定名・行番号・関数・ソースの行) |
| `unicode13.rs` | 生成物です。手で直しません。CPython 3.10.11(Unicode 13.0.0)の小文字化・語末のシグマの判定・`re.I` で同じと見なす文字の組・空白の表で、`native/fixtures/r4e/generate_unicode13.py` が作ります |
| `theme1.xml` | 既定テーマのバイト(10140 バイト)。`xml.rs` が `include_bytes!` で埋め込みます |
| `NOTICE.md` | openpyxl 3.1.5(MIT ライセンス)から転記した部分と、許諾の全文 |

- `printable`(`repr` のエスケープ)と `decimal`(10 進数字の値)は、R4B の `r4b_support/unicode.rs` を `#[path]` で取り込み、読み取りだけで使います。
- 新しい crate は足していません。XLSX と ZIP は自前で作ります。

`crates/platform/src/r4e.rs` は、R4E 専用の OS の部品です。Linux と Mac の、64 ビットの x86_64 と aarch64 だけで組みます。

- C の型・関数・定数は `libc` の crate のものを使います。`libc` 0.2.191 に Unix 向けの宣言が無い `tzset` だけを、手で宣言します。

- 起動前の状態: `record_startup_state()`(`main` より前に呼ばれる一覧からだけ呼ぶ)と `startup_state() -> Option<StartupState>`。
  - `StartupState` は、fd 0〜2 の起動の時点の状態(開いていたか・種類、Python 3.10 がその fd を使うか)と、継いだ `SIGPIPE`・`SIGINT` の扱いを持ちます。
  - 記録が無いときは `None` で、既定値を作りません。
- fd: `fd_kind`・`is_terminal`(Linux は glibc と同じく `tcgetattr` で判定)・`terminal_columns`・`stdout_terminal_columns`・`write_once`(`write` を 1 回だけ)・`close_checked`(`close(2)` の誤りを返す)・`block_size`。
- 信号: `ignore_sigxfsz`・`set_sigpipe_ignored`・`install_python_sigint -> SigintSetup`・`sigint_pending`・`take_sigint`・`sigint_count`・`restore_default_sigint`・`resend_sigint_by_default`。
  - SIGINT の扱いは、起動時に既定のときだけ置きます。`SA_RESTART` を付けないので、書き込みなどの待ちは `EINTR` で戻ります。
- 時刻と実行中の thread: `local_time`(`localtime_r`)・`utc_time`・`current_thread_id`(`pthread_self`)。
- 現在のディレクトリ: `current_dir`。Linux は glibc の `getcwd` と同じ手順で、4096 バイトを超える現在地も得ます。Mac は OS の `getcwd` を使います。

unsafe の置き場は、次の 2 つだけです。

- platform の `r4e.rs` の中。公開するのは安全な関数だけです。
- `crates/cli/examples/r4e_contract.rs` の、起動前の関数の一覧の項目(1 つの静的変数 `RECORD_STARTUP_STATE`)。
  - Linux は `.init_array`、Mac は `__DATA,__mod_init_func` に置きます。項目を置く属性(`link_section`)が unsafe の属性なので、example の先頭は `#![deny(unsafe_code)]` にし、この 1 つだけに `#[allow(unsafe_code)]` を付けます。
  - `md_tables_to_xlsx` の木は `#![forbid(unsafe_code)]` です。platform の木は一覧の項目を持たない(テストの実行ファイルを除く)ので、製品の `dev-workflow` の起動前の関数は増えません。

`crates/cli/examples/r4e_contract.rs` は検査専用の入口です。

- `md_tables_to_xlsx` を `#[path]` で取り込み、`args_os()` からプログラム名を除いた argv を、旧版と同じ形で受けて `run` へ渡します。出力は Python と同じ出し方で、C0a の `emit` は使いません。
- 起動前の記録が無ければ、`r4e_contract: 起動前の記録がありません` を出して終了70にします(旧版が返さない値です)。
- 検査専用の隠れた環境変数が 2 つあります。どちらも、値があるときだけ働き、本体を動かさずに終わります。
  - `R4E_CONTRACT_STARTUP_PROBE`: 起動前の記録と今の fd の種類を、`r4e-startup <要約> now=<種類>` の 1 行で出します。値は `1`(標準出力)・`stderr`(標準エラー)・`file:<パス>`(そのファイル)です。
  - `R4E_CONTRACT_FRAMES`: 呼出履歴の段の表を、JSON の行で標準出力へ出します。比較器が旧版の環境の実物と照らすのに使います。
  - 故障を注入する環境変数や、時間を縮める入口はありません。
- Rust の単体テストは、Linux x86_64 で 114 件です(Mac の件数は未確認)。`cargo test --locked -p dev-workflow-cli --example r4e_contract` だけで流れます。platform の `r4e.rs` の検査は、通常の `cargo test --locked` で流れます。

認めた差は、決定の表にあるものだけです。R4E だけに当てる差は、CSV の経路を移さないこと・XLSX を圧縮しないこと・一時ファイルを使わないことです。
契約と認めた差の一覧は [設計のR4E節](../docs/design.md#markdown-の表の-xlsx-出力r4e)にあります。
[比較の手順と件数](../docs/testing.md#r4eの比較)に従ってください。通常の`cargo test --locked`だけでは、exampleの単体検査と旧版比較は完了しません。

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
  - R4D-1 が足した自作の `unsafe` は、`close_owned` の 1 か所です。

`diagnostic_git` は `commands/mod.rs` に登録していません。検査は `tests/diagnostic_reftable.rs` が `#[path]` で読み込みます。

必須の比較は [テストの手順](../docs/testing.md#r4d-1-の-reftable-の比較) に従ってください。
比較のテストには `#[ignore]` の印を付けたので、通常の `cargo test --locked` では流れません(結果は `ignored`)。比較は、次のコマンドか `build.py r4d1` で流します。

```sh
(cd native && cargo test --locked -p dev-workflow-cli --test diagnostic_reftable -- --ignored --exact rust_matches_the_legacy_reftable_reader_on_every_input)
```

比較は、reftable を作れる git(2.45 以上)と Python 3.10 系・root 以外を使います。

## 保護記録の探索・reviewsの配置・内蔵のGit読取り(R4G)

#479で旧 `pending-implementation.py`・`reviews-dir.sh`・`implementation-git.py` と同じ結果を返す部品を実装しました。製品CLIの登録表・help・配布・既存呼出元は変えていません。
製品への接続は、後続の配布タスク(D3・D4)で行います。`implementation-git` の事前検査をRust版へ切り替えるのは、そのうちD3です。契約と決定済みの差は [設計のR4G節](../docs/design.md#保護記録の探索reviewsの配置内蔵のgit読取りr4g)にあります。

- `crates/cli/src/commands/` の専用モジュールは4つです。どれも `mod.rs` から `lines.rs` などの部品を持ちます。
  - `pending_implementation/`: `scan`。`scan.rs`(置き場の走査)・`mark.rs`(印の検査)・`report.rs`(結果のJSONと標準エラーの行)・`system.rs`(OS呼出しの差替え口)・`lines.rs`(旧版の行の固定表)。
  - `reviews_dir/`: `ensure`・`save-untracked`。`shell.rs`(旧bashの子の起動・信号・EXITの罠)・`locale.rs`(`LC_ALL=C` の英語の診断)・`lines.rs`(旧版の行番号の固定表)。
  - `implementation_git/`: `prepare`・`run`の5操作。`args.rs`(Python 3.10のargparseと同じ解析)・`help.rs`(helpの全文と折返し)・`path.rs`(`--cwd` と物理パス)・`lines.rs`(呼出履歴の固定表)。
  - `r4g_support/`: 旧Python 3.10と旧bashの入出力・診断・起動失敗の再現部品。`python_io.rs`(`TextIOWrapper` と同じ時点の書き込み)・`python_startup.rs`(起動の環境変数と起動の失敗の診断)・`bash.rs`・`json.rs`(正準JSON)・`pyint.rs`(`int()` の受理条件)・`strerror.rs`(glibcとlibSystemの英語の文面)・`text.rs`・`traceback.rs`。R4Bの `bytes.rs`・`unicode.rs` は変更せず `#[path]` で取り込みます。
- `crates/cli/examples/r4g_contract.rs` は検査専用入口です。形は `r4g_contract [--r4g-… の指定]… <コマンド> [引数]…` で、コマンドは `pending-implementation`・`reviews-dir`・`implementation-git` です。
  - `--r4g-…` の指定は、旧版の起動の形に無い値を検査のときだけ渡す口です。製品のコマンドには接続しません。指定は `--r4g-script-name`・`--r4g-script-file`・`--r4g-output-fault`・`--r4g-clock`・`--r4g-pending-fault`・`--r4g-deadlines`・`--r4g-mutation` です。
  - 製品の登録表(`COMMANDS`)には載せません。製品の引数・環境変数から保護を外す口はありません。
  - 起動前の記録のマクロ `r4g_record_startup_state!` を展開するのは、この入口と `platform` の `startup.rs` の単体テストだけです。
- `crates/platform/src/r4g/` はR4G専用のOS部品です。R4G が足した自作の `unsafe` は、この3つのファイルだけに置きます(`pub mod r4g;` を `lib.rs` に足しました)。
  - `startup.rs`: 標準ライブラリが `main` の前に変える前の、`SIGPIPE` と信号の扱いの記録と戻し(fd 0〜2は記録しません。2026-11-12 08:55の決定)。旧Pythonの `SIGINT` の扱い。
  - `fs.rs`: fd相対の `openat`・`fstatat`・readdir・`..`・上限付きの読取り・長いパスでも得られる現在のディレクトリ。
  - `process.rs`: 旧 `subprocess` と旧bashと同じ子の実行(継承したfdを閉じる・PATHの探索とerrno・環境の辞書・信号の設定とmask)・旧bashの信号の扱いとコマンド置換のサブシェル。
- `implementation_git::SAFE_GIT`(14項目)と `safe_git_argv()` は、診断9本の移植(R4D)が使う予定の定数です。
- 比較は `native/ci/check_r4g.py` が行います。決定済みの差の一覧は `native/ci/r4g_fixtures/decided-differences.json` にあります。
- Rustの単体テストは、`cargo test --locked -p dev-workflow-cli --example r4g_contract` で52件、`cargo test --locked -p dev-workflow-platform --target x86_64-unknown-linux-musl` の44件(R4Rなどの分を含む)です(手元のLinux x86_64。Macは未確認)。

`libc` は、R4Rが宣言した `0.2.191` をそのまま使います(R4Gは版も機能も変えません)。Rustの依存とcrateの共有ファイル(`Cargo.toml`・`Cargo.lock`・`crates/platform/src/lib.rs`)へのR4Gの差分は、`crates/platform/src/lib.rs` の `pub mod r4g;` の1行だけです。
[比較の手順と件数](../docs/testing.md#r4gの比較)に従ってください。通常の `cargo test --locked` だけでは、exampleの単体検査と旧版比較は完了しません。

## implement-guardのGitを呼ばない3コマンド(S3I-A)

#474で、`implement-guard.sh` のうちGitを呼ばない `taskmd-diff`・`restore-taskmd`・`cleanup` の3つと、全6サブコマンドの使い方・引数の検査・保護領域の確かめ・記録の読み取りを実装しました。製品CLIの登録表・help・配布・既存の呼出元は変えていません。`take` は S3I-B #475、`config-check`・`compare` は S3I-C #476 で移します。

- `crates/cli/src/commands/implement_guard/` に部品があります。
  - `mod.rs`: 入口 `main_entry` と、旧版の関数との対応表(先頭)。引数が正しい `take` と、記録の読み取りまで通った `config-check`・`compare` は、境界の印(stderr の `ERROR [s3i-a-boundary] …` と終了値99)で止めます。
  - `shell.rs`・`stdio.rs`・`sites.rs`: bashの実行時の再現(組み込みの `echo`・`printf` の書き出しと失敗・`ERR`・`EXIT`・TERM・INT・HUP の罠・罠を確かめる区切り)と、失敗の場所と行番号の表。
  - `args.rs`・`usage.txt`: 引数の検査と使い方の文(旧版の2〜202行)。
  - `paths.rs`・`state.rs`・`meta.rs`: パスの解決(`cd -P`・GNU `realpath`・`command -v`)・保護領域の確かめ・記録の読み取り。
  - `text.rs`: C風の引用と戻し、GNU coreutils の引用(`quote`・`quoteaf`)。
  - `taskmd_diff.rs`・`restore.rs`・`cleanup.rs`: 3つのサブコマンドの本体。`cleanup.rs` の `rm -rf` は、親の fd から辿ります。
  - `childenv.rs`: 本文の復元の cp の子の環境。
  - `tests.rs`: 単体テスト(23件)。旧版を測った固定期待(`native/ci/implement_guard_fixtures/expected_old.json`)と同じ地点を、差し込み点で起こして照らします。
    - 故障の固定期待と照らす1件(`faults_match_the_measured_old_version`)は、Linuxだけで流します。Linuxで測ったerrnoの文が、Macの文と違うためです。Macでは22件です。
- `restore.rs` は、exec の時点に当たる所で、R4Rの公開関数(`start`・`parse_request`・`Restore`・`emit`・`exit_flush`・`exit_close` など)を、`examples/r4r_contract.rs` と同じ順に、同じプロセスで呼びます。R4Rの中身は変えていません。errno の文は `restore_taskmd/errno_text.rs`(Linuxは生成物の `errno_glibc.rs`、Macは `platform` の `strerror_text`)から出します。
- `crates/platform/src/implement_guard.rs` は、起動の前の記録・信号の扱いと mask・umask・`/dev/null` への差し替え・fd の種類・環境の置き換え・`faccessat`・地域設定の判定・`rm -rf` の走査(`openat`・getdents・`fstatat`・`unlinkat`)を、安全な関数で公開します。
  - S3I-A の OS の呼び出し(`libc` の関数と `unsafe`)は、このファイルの安全な関数の中だけにあります。例は、信号の扱いと mask・`raise`・umask・fd の差し替えと種類(`dup2`・`fstat`・`fcntl`・`BorrowedFd::borrow_raw`)・環境の読み取りと置き換え(`environ`・`clearenv`・`setenv`)・利用者の番号とログインのシェル(`getuid`・`geteuid`・`getpwuid`)・`faccessat`・`pthread_self`・Macの `newlocale` です。全部の並びではありません。全部は、そのファイルの `unsafe` の所で見てください。
  - Macの errno の文の `strerror_r` は、R4Rの `platform/src/restore_taskmd.rs` の `strerror_text` に足しました(S3I-A が足した `unsafe`)。
- `crates/cli/examples/implement_guard_contract.rs` は検査専用入口です。`args_os()` からプログラム名を除いた argv を、旧版と同じ形で受けます。
  - `#![deny(unsafe_code)]` の中で、初期化の表への登録(Linuxは `.init_array`、Macは `__DATA,__mod_init_func`)の1項目だけを `#[allow(unsafe_code)]` にしています。`main` の前に、引き継いだ `SIGPIPE` の扱いなどを記録するためです。製品の実行ファイルはこの関数を参照しないので、`main` の前に何も走りません。
  - 故障を注入する環境変数や、時間を縮める入口はありません。差し込み点は単体テストの中だけで働きます。
- Rustの単体テストは、`cargo test --locked -p dev-workflow-cli --example implement_guard_contract` で流れます(Linuxで83件。R4Rの件を含む)。platformの6件は `cargo test --locked` で流れます。

契約と当てた決定は [設計のS3I-A節](../docs/design.md#implement-guardのgitを呼ばない3コマンドと記録の読み取りs3i-a)にあります。
[比較の手順と件数](../docs/testing.md#s3i-aの比較)に従ってください。通常の`cargo test --locked`だけでは、exampleの単体検査と旧版比較は完了しません。

## diff-snapshotの全モード(S3D)

#472で、旧`diff-snapshot.sh`の全モードに対応する専用モジュールを実装しました。製品CLIの登録・help・配布・既存呼出元は変えていません。

- `crates/cli/src/commands/diff_snapshot::execute(Vec<OsString>, Signals)`は、`CommandOutput`を返します。`main_entry()`は引数を`args_os()`から取り、信号の受け口を入れて最終の標準エラーを出します。
- `crates/cli/examples/diff_snapshot_contract.rs`が`main_entry()`だけを呼びます。製品の`dev-workflow`には登録しません。`#![deny(unsafe_code)]`で、起動前の信号とfdの状態を記録する`.init_array`の初期化子だけを例外にしています。
- 専用モジュールは、引数(`args`・`helper_args`)・GNU ERE(`ere`)・安全なGit実行(`git`)・事前検査(`precheck`)・承認値(`approval`)・profile(`secret_profiles`)・追跡と未追跡の分類(`tracked`・`untracked`)・11節(`report`)・出力(`output`)・公開(`publish`)に分かれます。
- 外部に起動するのは`git`と`grep`だけです。ほかの道具の処理と失敗の診断は`tools`が持ち、旧版の行番号・文面(`LC_ALL=C`の英語)・終了値に合わせます。`strerror_table`はglibc 2.39の固定表(134件)です。
- 生バイトの引数・パス・Git出力を`to_string_lossy`で変えません。`legacy_text`は、旧Pythonの`PYTHONINTMAXSTRDIGITS`・不正な`PYTHONHASHSEED`・標準入力がディレクトリのときの起動失敗とUTF-8の診断を受け持ちます。文字コード設定は読みません。
- `typed_tests`(`cfg(test)`)は、Rustの型で操作を差し替えて、公開の前後・信号・profileの差替え・時刻・書き込みの故障を検査します。通常の入口にはこの差替えを有効にする引数や環境変数がありません。

#### platformの専用部品

- `platform::diff_snapshot::fs`は、保持したディレクトリからのprofile読取り(`current_profile`)・読取りと同時のhash(`read_and_hash`)・生パスの`read_link`と`open_size`・標準fdへの書き込み(`write_standard`)・開始時の場所の保存と復帰(`SavedDirectory`)・OSの`strerror`(`os_strerror`)を持ちます。
- `platform::diff_snapshot::process`は、旧bashが子の環境へ渡す`LINENO`(`Lineno`)、profile用`git`の10秒つきの取得(`profile_capture`)・期限なしの取得(`capture`)・パイプ(`pipeline`)・bashが子へ渡す環境の再現(`Shell`・`export`)を持ちます。
- `platform::diff_snapshot::signals::Signals`が、旧bashの信号と終了値を再現します。`locale::lc_all`は、`LC_ALL`に指定したlocaleが入っているかをglibcの規則(Linux)かDarwinの`newlocale`(Mac)で判定し、muslの`setlocale`には聞きません。
- `Operations`・`Audit`・`AuditScope`・`FaultScope`は、検査用の操作の差替え・読取りの記録・故障の注入です。通常は`NativeOperations`・`NoAudit`が使われます。
- 残るlibcの直接呼出しの理由は`platform/src/diff_snapshot/mod.rs`の冒頭にあります。`rustix`には`process`・`rand`・`stdio`の機能を足しました。`Cargo.lock`は変わりません。

#### coreの専用部品

- `core::s3d_profile::snapshot_patterns(raw, SnapshotYamlPolicy)`は、1つのprofileの`secret_paths`相当を、`CodepointString`の一覧か`SnapshotProfileError`で返します。既定の追加と重複除去はしません。
- `SnapshotYamlPolicy::python_3_10(LegacyIntLimit)`は、CPython 3.10.11・PyYAML 6.0.3の再帰条件を使います。`recursion`は旧処理の呼出段数を数え、実スタックの深さを使いません。`legacy_scanner`と`legacy_parser`が、token取得の順と状態遷移を再現します。
- `validate_glob`はglob1件を検査します。`codec`は、厳密なUTF-8の検査と孤立surrogateの符号化の失敗位置だけを持ちます。
- `loop_state`には、用途別の入口が必要とする型情報と解析段階の観測を足しました(共有ファイル。既存の入口の受理条件と結果は変えません)。

契約と承認済みの差は [設計のS3D節](../docs/design.md#s3dに限る承認済みの差)にあります。
比較の流し方・版に依る欄・手元実測は [testing.md](../docs/testing.md#s3dの比較)にあります。
通常の`cargo test --locked`だけでは、旧版との比較は完了しません。

## 残り5本の解析補助(R4A)

#471で旧Pythonの5本(`resolve-task-store`・`candidate-keys`・`host-argv`・`loop-task-text`・`loop-child-output`)と同じ結果を返す部品を実装しました。
製品CLIの登録表・help・配布・既存呼出元(`loop.sh` など)は変えていません。未登録・未配布です。

- 本体は `crates/cli/src/commands/<名前>/mod.rs` にあります。ハイフンは下線にします(例: `candidate_keys`)。
  - 共通の部品は `commands/r4a_rest_support/` です(引数の解析 `args`・文字 `chars`・JSON `json`・NFC `nfc`・例外の表示 `exception`・二段のバッファ `runtime`・標準入出力と終了の工程 `stream`・起動前の拒否 `startup`)。
  - Unicode 13の表と `strerror` の表は `r4a_rest_support/unicode/` にあり、生成器(`generate*.py`)が同じ場所にあります。
- 入口の関数は、どれも `main(&mut Program, ...)` の形です。引数はバイトと文字番号を保つ `PyStr` で受けます。
  - `resolve_task_store::main(program, &Env, &[PyStr])`・`resolve(Option<&PyJson>)`(解析済みの値の判定)。
  - `candidate_keys::main(program, &Env, &[PyStr])`・`check_text(&[u32], Option<&[u32]>)`・`normalize_key`。
  - `host_argv::main(program, &[PyStr])`・`validate(&[PyStr]) -> Result<(), Failure>`。
  - `loop_task_text::main(program, &[PyStr])`。
  - `loop_child_output::main(program, &[PyStr], 整数の桁の上限, &mut dyn PermlogHooks)`。
- `crates/platform/src/r4a_rest.rs` は、R4Aだけが使うOSの操作を、安全な関数で公開します。
  - 標準fdの種別と大きさ(`standard_fd_info`・`standard_fd_kind`)、端末幅(`terminal_size`)、スレッドの値(`pthread_identity`)。
  - 生の1回の入出力(`write_once`・`read_once`)。`EINTR` は、SIGINTを受け取っていなければ再試行します。受け取っていれば `INTR` を返します。
  - SIGINT(`install_sigint_handler`・`sigint_pending`・`take_sigint`・`exit_by_sigint`)。受け継いだ扱いが `SIG_DFL` のときだけ受け手を入れます。`SIG_IGN` はそのまま残します。
  - ファイルの読取り(`read_file`)と、permlogの読取り(`read_permlog`)。permlogは、親と末端をリンクを辿らずに1段ずつ開きます。
  - `PermlogHooks` は、検査が競合や `close` の失敗を差し込む口です。製品は `NoHooks` を渡し、何も起こしません。
- `crates/cli/examples/r4a_rest_contract.rs` は検査専用入口です。第1引数に5本の名前を取ります。
  - 検査専用の補助が3つあります(`strerror`・`chartable`・`nfc`)。
  - 環境変数で故障や競合を差し込む部品(`R4A_FAULT`・`R4A_RACE`・`R4A_CLOSE_FAIL`)は、このexampleの中にだけあり、製品の入口にはありません。
- 製品の `commands/mod.rs` には、5本を登録していません。
- Rustの単体テストは、`cargo test --locked -p dev-workflow-cli --example r4a_rest_contract` が48件、`-p dev-workflow-platform --example r4a_rest_platform` が7件です。

認めた旧版との差と、比較の前提の外は、利用者の決定7つ(2026-11-11 15:53・17:45・17:58〜22:38、22:31、2026-11-12 00:28・00:42・08:55)の範囲だけです。時刻つきの一覧は [設計のR4A節](../docs/design.md#認めた旧版との差と比較の前提の外利用者の決定の範囲)にあります。
契約は [設計のR4A節](../docs/design.md#残り5本の解析補助r4a)にあります。
[比較の手順と件数](../docs/testing.md#残り5本の解析補助の比較r4a)に従ってください。通常の `cargo test --locked` だけでは、旧版との比較は完了しません。

## 候補の採用と改名(R4C)

#478で、旧`adopt-candidate.py`(候補を進行中の名前へ採用する処理)と同じ結果を返す部品を実装しました。
製品CLIの登録表・help・配布・既存呼出元は変えていません。未登録・未配布です。

- 本体は`crates/cli/src/commands/adopt_candidate/`にあります。役割は次のとおりです。
  - `args`: 旧`argparse`(Python 3.10)相当の引数の解析・usage・help・端末幅。引数は生バイトのまま持ちます。
  - `paths`: `Path.absolute()`と`PurePosixPath`の字句だけの扱い。OSでリンクを解決しません。
  - `plan`: OSを触る前の順序(環境の印→無人実行→rootと候補の絶対化→相対化→候補名→採用名)。
  - `flow`: 旧`adopt()`の流れ。OS・候補解析・事前検査は、`Ops`・`Candidates`・`Precheck`の3つのtraitの向こうに置きます。
  - `candidate`・`index`: 保持SHAと読み直しの比較(本文とdev/ino)、改名前後のindex(stageが0の1件)の解析と比較。
  - `diagnostics`・`output`: 停止の文と旧Pythonの例外の表示、errnoの文(Linuxは基準の表、MacはOSの`strerror`)、正常時のJSON。
  - `runtime`・`program`: 旧Pythonの起動の失敗・環境変数の効果・標準出力の二段のバッファと出力の故障、`main()`全体。
  - `real`: 実際のOS操作(`RealOps`)と、事前検査(`SelfPrecheck`)。
  - `inject`: 検査専用の注入(下)。
- `crates/platform/src/adopt_candidate.rs`は、R4Cだけが使うOS操作を、安全な関数で公開します。
  - 保持したディレクトリ(`open_chain`)と、旧版と同じ手順の候補の読取り(`read_candidate`・`read_candidate_with`)。
  - リンクを辿らない`stat`(`stat_nofollow`)、無上書きの改名(`rename_noreplace`・`classify_rename`)、排他的な`link_noreplace`と`unlink`。
  - 保持したrootのfdから作業場所を決める子の実行(`run_in_dir`)。stdinは継承し、stdoutとstderrだけ捕捉します。
  - 実行するパスと`argv[0]`を分ける子の実行(`run_in_dir_with_executable`)。事前検査が、自分の実行物を`argv[0]=diff-snapshot.sh`で起動するために使います。
  - 自分の実行物の絶対パス(`own_executable_path`)。起動したとき(作業場所を変える前)に決めます。
  - SIGINT(`install_interrupt_handler`・`interrupt_protection`・`interrupted`・`interrupt_exit`)と、標準fdの調べ・書込み(`write_standard`・`standard_is_directory`・`standard_is_terminal`・`standard_blksize`)。
- `crates/cli/examples/adopt_candidate_contract.rs`は検査専用入口です。
  - S3Dの`diff_snapshot`と、R4Aの`candidate_keys`とその依存(`r4a_rest_support`・`resolve_task_dir`・`task_digest`)を`#[path]`で取り込みます(複製しません)。R4Aの`r4a_rest_contract`と同じ形で、exampleの中に`mod commands`の置き場を作ります。
  - 子のモード: `main`の最初に`argv[0]`だけを見ます。`diff-snapshot.sh`と完全に一致するときは、R4Cの設定(信号・起動時の調べ・注入の読取り)より前に、S3Dの入口`diff_snapshot::main_entry`へ入ります。事前検査は、この形で自分自身を子として起動します。
  - 候補解析は、`CandidateKeys`が`candidate_keys::check_text(本文, None)`を呼び、戻りの`problems`を元の順のまま移します。辞書・文字列・配列以外が来たら内部の失敗として止めます。
  - `#![deny(unsafe_code)]`で、`main`の前に起動前の状態を記録する初期化子の1項目だけを許します(S3Dのexampleと同じ)。
- 注入の仕組み: 環境変数`R4C_FAULT_SPEC`が指すJSONの仕様を、`Ops`と`Precheck`の境界(`Injected`・`InjectedPrecheck`)が読みます。
  - 旧版の比較用の道具が、旧テストと同じ点に差し込む操作を、Rustも同じ点へ差し込みます。点`start`では、`R4C_FAULT_NOTIFY`と`R4C_FAULT_WAIT`のfdで、比較器と同期します。
  - 仕様の書き方は`native/ci/adopt_candidate_fixtures/fault_scenarios.json`と`check_adopt_candidate.py`にあります。
  - この環境変数は、このexampleだけにあります。製品の入口にはありません。
- 製品の`commands/mod.rs`には、登録していません。
- 既知の窓: forkの直後から`prepare_child`の信号のmaskの設定までの短い間に届いたSIGINTは、子へ届くのが少し遅れます(2026-11-12の実装レビュー4回目のトリアージで決めました)。旧版にも似た窓があり、0.25秒の待ちの中に収まります。
- Rustの単体テストは、`cargo test --locked -p dev-workflow-cli --example adopt_candidate_contract -- --skip diff_snapshot:: --skip candidate_keys:: --skip r4a_rest_support:: --skip resolve_task_dir_definition:: --skip task_digest_definition::`です。取り込んだS3DとR4Aの部品の単体テストは、それぞれのexampleの単体検査(`build`の`quality()`。S3DのMacは`s3d`ジョブの型検査)に残すので外します。件数は未確認です。

認めた旧版との差と、比較の前提の外は、全コマンドの決定の範囲だけです。時刻つきの一覧は [設計のR4C節](../docs/design.md#r4cに限る承認済みの差)にあります。
契約は [設計のR4C節](../docs/design.md#候補の採用と改名r4c)にあります。
[比較の手順と件数](../docs/testing.md#r4cの比較)に従ってください。通常の`cargo test --locked`だけでは、旧版との比較は完了しません。

## environment-guardの環境照合(S2E)

#470で、旧`environment-guard.py`(以下、旧guard)の9コマンドと内部reader(`host-check.py`が使う3つの関数)と同じ結果を返す部品を実装しました。
製品CLIの登録表・help・配布・既存呼出元は変えていません。未登録・未配布です。

- 本体は`crates/cli/src/commands/environment_guard/`にあります。役割は次のとおりです。
  - `args`・`output`・`runtime`・`error`・`errno_table`・`startup`: 旧`argparse`(Python 3.10)相当の引数の解析・usage・help、旧Pythonのbuffering・途中のflush・終了時のflush失敗(終了コード120)、旧版の例外の型名と理由、`errno`の文(Linuxはglibcの表)、`PYTHONUNBUFFERED`・`PYTHONINTMAXSTRDIGITS`・`PYTHONHASHSEED`の扱い、旧Pythonが起動時に止まる経路(診断を標準エラーへ書き、終了コード1)。
  - `pyvalue`・`pyjson`・`pyops`・`pystr`・`pypath`・`unicode_tables`: Pythonの値と等しさ、JSONの読み書き(用途ごとの読取り3種と書き出し5種)、文字列(空白・NFC・切り詰め)、`Path`と`expanduser`。Unicodeの表は、Python 3.10(Unicode 13.0.0)から作ります。
  - `budget`・`fsio`・`depth`: 観測1回ごとの予算(項目10,000・1ファイル8MiB・総量64MiB・15秒)、リンクを辿らないファイル操作と親の照らし直し、旧Pythonの再帰の深さの数え方。
  - `component`・`record`・`gitconfig`・`specs`・`inventory`・`settings`・`snapshot`・`state`・`entry`: 部品の木・記録・Git設定の解析(旧版と同じ順序の観測と拒否)・控え`version 3`の値と照合。
  - `bootstrap`・`copy`・`pair`・`dispatch`: `bootstrap`、`read`・`exec`・`hook`、`verify-pair`と、コマンドの振り分け。
  - `reader`: 内部reader3つ(`read_host_settings`・`read_components`・`component_policy_digest`)のAPI。Pythonの値と同じ意味の型(`PyValue`)か、旧版の例外と同じ型名と理由を持つエラーを返します。
- `crates/platform/src/environment_guard.rs`は、S2Eだけが使うOS操作を、安全な関数で公開します(例外は、`main`より前に呼ぶ`unsafe`関数`capture_inherited_sigpipe`だけです)。
  - ディレクトリ一覧・`readlinkat`・排他作成・`fchmod`・リンクを辿らない`chmod`・`fsync`・`mkdirat`・標準入力の期限つき待ち(`poll`。Macの`verify-pair`は旧版の`select`と同じ呼出しを使う)。`go`の待ち(`wait_readable_interruptible`)は約100ミリ秒に区切り、待ちの前と区切りごとに`SIGINT`の印を見ます。残りの時間は、入口で決めた期限(単調な時計)から区切りごとに計算し、区切りの回数では数えません(区切り1回が延びても、合計は期限を大きく越えません)。子の`wait`も、`WNOHANG`と最大10ミリ秒の休みで印を見直します(印を見てから待ちに入るまでに届いた信号を取りこぼさないため)。
  - 子の起動(`fork`と`execve`。PATHの探し方・受け継ぐfd・子の信号の設定を旧Pythonの`subprocess`に合わせます)と、`SIGINT`・`SIGXFSZ`の扱い、利用者データベースの読取り(`~user`)。
  - `unsafe`は、この1ファイルの関数の中だけに置きます(各所に安全の理由を書きました)。ただし、初期化の表への登録の`static`だけは、マクロを展開した側(`examples/s2e_contract.rs`とplatformのテスト)に生まれます(下の`main`より前の初期化を参照)。`cli`の`environment_guard`は`unsafe`を使いません。
- `crates/cli/examples/s2e_contract.rs`は検査専用入口です。`#![forbid(unsafe_code)]`で、9コマンドを動かします。
  - 隠しコマンド: 引数が`reader-`で始まるときだけ、内部reader3つを呼び、返した値を型の印つきの1行のJSONで出します。`reader-host-settings <state> <sha256> [呼出し側の深さ]`・`reader-components …`・`reader-policy-digest …`です。比較器が、旧版の同じ関数の値と比べます。
  - 製品の入口には、この隠しコマンドも、注入の入口もありません。
- 細かい競合(`lstat`と`open`の間・`EINTR`の注入など)は、`cfg(test)`のフックだけで差し込みます。フックは製品と同じ関数の中にあり、フックを外した経路は製品と同じコードを通ります。単体検査は`observe_tests.rs`・`race_tests.rs`・`command_tests.rs`・`copy_tests.rs`にあります。
- `main`より前の初期化は、検査専用入口(`examples/s2e_contract.rs`)とplatformのテストにだけあります。platformのマクロ`s2e_record_startup_state!`を展開した実行ファイルだけが、初期化の表へ`capture_inherited_sigpipe`を載せます(Linuxは`.init_array`、Macは`__DATA,__mod_init_func`)。この関数が、`main`より前に、受け継いだ`SIGPIPE`の扱いを`sigaction`で読んで控えます。
  - 展開してよいのは、`examples/s2e_contract.rs`の1か所と、`environment_guard.rs`の`#[cfg(test)]`のテストのモジュールの中だけです。`scripts/test_native_s2e.py`が、展開の場所・別名・許可の外からの関数の参照を静的に確かめます。
  - 展開した側のcrateに`#[unsafe(link_section)]`の`static`が生まれます(外部のマクロの展開なので、`#![forbid(unsafe_code)]`には当たりません)。対応外のOSでは何も登録せず、`inherited_sigpipe()`は`None`です(`compile_error!`にしません。控えが無い機種は既定へ戻す、という今までの動きを変えないためです)。
  - Rustの標準ライブラリは、`main`の前に`SIGPIPE`を無視へ変えるので、受け継いだ値はこの時点でしか読めません。
  - 初期化関数は、確保・ロック・Rustの入出力を使いません。控えは`inherited_sigpipe()`で読みます。設定の段階の起動の失敗の診断を書く前に、控えが無視でなければ既定へ戻すために使います。
  - Macの実機での動作は未確認です(clippyだけ、aarch64とx86_64のappleで通りました)。
- 製品の`commands/mod.rs`には、登録していません。
- このソースから作る製品の`dev-workflow`には、`main`より前の処理が入りません。
  - 2026-11-26に、muslのreleaseを直す前の作りと比べました。`strace -f -e trace=rt_sigaction dev-workflow --version`で、直す前にあった`rt_sigaction(SIGPIPE, NULL, …)`が無くなり、標準ライブラリが`SIGPIPE`を無視へ変える呼出しだけが残りました。
  - 製品の`.init_array`の大きさは、gnuが0x10・muslが0x8です(R4Eの前のmainと同じ)。
  - 配布中のv4.53.0も、`main`より前の処理は無いままです。
- Rustの単体テストは、`cargo test --locked -p dev-workflow-cli --example s2e_contract`です。作業機のdebugで、glibcは193件、muslは194件が通りました(2026-11-26。`go`の待ちの検査を足した後)。

認めた旧版との差と、比較の前提の外は、利用者の決定(U1〜U7と全コマンドの決定)の範囲だけです。時刻つきの一覧は [設計のS2E節](../docs/design.md#s2eに限る承認済みの差)にあります。
契約は [設計のS2E節](../docs/design.md#environment-guardの環境照合と内部readers2e)にあります。
[比較の手順と件数](../docs/testing.md#s2eの比較)に従ってください。通常の`cargo test --locked`だけでは、旧版との比較は完了しません。

## R4Q(品質の実行の子の監督)

#494で、旧版 `plugins/dev-workflow/skills/ship-task/scripts/check-process.py` と `loop-supervisor.py` と同じ結果を返す専用の部品を実装しました。
製品CLIの登録表・help・配布・既存の呼び出し元(`review-guard.py`・`loop.sh`)は変えていません。未登録・未配布です。
製品の実行物(`dev-workflow`)には入りません。検査用の実行物(`crates/cli/tests/r4q.rs`)だけが読み込みます。

### 入口

- `crates/cli/src/commands/loop_supervisor::run(args: &[OsString]) -> u8` が、`python3 loop-supervisor.py <引数…>` と同じ動作をして終了値を返します。`args` にプログラム名は含めません。
- `crates/cli/src/commands/check_process::run(args: &[OsString]) -> u8` が、`python3 check-process.py <引数…>`(監督の内部 CLI。anchor と linux worker)と同じ動作をして終了値を返します。
- `check_process::run_command::run_command(request: &RunRequest, stop: &StopSignals) -> Result<Vec<(PyValue, PyValue)>, RunError>` が、旧版の親の `run_command` と同じ結果を返します。
  - `RunRequest` は、argv・cwd・env(渡した順。渡さないこともできる)・timeout・directory・grace・method・keep・監督の起動の仕方(`Monitor`)・JSON の整数の桁の上限・呼出元のフレームの数と再帰の上限を持ちます。
  - timeout と grace は、Python の int と float を区別する値(`PyNumber`)で渡します。`--timeout` の字面を旧版の `str(timeout)` と同じにするためです。
  - 返す値は、順序付きの辞書を表す `PyValue` の並びです。鍵の順と型を旧版と同じにします。
  - 誤りは `RunError` の3つです。`Recovery`(回収を確かめられない)・`Interrupted`(停止の信号を受けた)・`Unwrapped`(旧版が包まずに外へ出す例外の型の名前と文)。
  - `StopSignals::enter()` が TERM・INT・HUP の最初の1つを記録する処理を置き、drop で前の処理へ戻します。
- 呼び出し側は、長く生きるスレッド(ふつうは main のスレッド)から `run_command` を呼びます。親の死の通知は、監督を起動したスレッドが終わると届くためです。

### 部品

| 場所 | 受け持ち |
|---|---|
| `crates/platform/src/r4q_process.rs` | OS の部品。subreaper と親の死の通知・pidfd(`libc::syscall`)・`waitpid`・プロセスグループへの信号・信号の処理の置き方と戻し方・子の起動(fork と exec の誤りを返すパイプ・信号の mask の受け継ぎ・PATH の候補へ順に試す・0〜2のほかの fd を閉じる) |
| `crates/cli/src/commands/r4q_python/` | Python の再現の純粋な部品。argparse・`int()`・`float()`・`json.dumps`・`json.loads(bytes)`・`OSError` などの文・`TimeoutExpired` の文・pathlib の整え方・呼出履歴の定型 |
| `crates/cli/src/commands/loop_supervisor/` | loop-supervisor の CLI と `supervise`。OS の操作と時計は trait(差し替えのための型の約束)で受け、製品は本物の実装を使う |
| `crates/cli/src/commands/check_process/` | check-process の監督と、親の `run_command` |

- `unsafe` は `platform/src/r4q_process.rs` の安全な関数の中だけに置きます。`cli` の R4Q の部品は `#![forbid(unsafe_code)]` です。
- 子の起動は std の `Command` を使いません。std は子の信号の mask を空にし、SIGCHLD を無視で受け継いだときの起動の失敗で panic するためです。
- 外部の crate・`rustix` の機能・依存の版は増やしていません。
- 回収した子の終わり方は、C0c の公開の型 `core::process::ChildStatus` で持ちます。C0c の `run_child` は、子の標準入出力が必ずパイプになるので使いません([設計](../docs/design.md#c0c-との関係))。

### 検査用の実行物

`crates/cli/tests/r4q.rs`(`harness = false`)は、引数で動きを変えます。

- `--r4q-loop-supervisor <旧版と同じ引数…>`: loop-supervisor として動きます。
- `--r4q-check-process <旧版と同じ引数…>`: check-process の監督として動きます。
- `--r4q-run-command <要求の JSON のパス>`: `run_command` を呼び、結果か誤りを JSON で出します。
- `--r4q-strerror <最大>`: errno 0〜最大の文を JSON で出します(比較器が0〜1024で、旧版の `os.strerror` と照らします)。
- `--r4q-compare`(`cargo test --locked -p dev-workflow-cli --test r4q -- --r4q-compare`): `native/ci/check_r4q.py --rust <自分>` と `native/ci/test_r4q.py` を起動します。どちらかの失敗・未実行・時間切れでテストを落とします。`PR_SET_CHILD_SUBREAPER` は設定しません。
- 引数が無いとき: 比較を流さず、`R4Q の比較は流していない: ` で始まる1行(流し方を添える)を出して終了0にします。比較の合格には数えません。
- ほかの引数は受け付けません。

単体テストは `crates/cli/tests/r4q_units.rs`(harness あり)です。固定期待(`native/fixtures/r4q/units.json`)と、OS の操作と時計を差し替えた比較(`native/fixtures/r4q/injected.json`)を照らします。

### 保証の範囲

- Linux は、監督が所有する子孫だけを回収します。同じ利用者の権限の完全な隔離ではありません。
- Linux 以外は、元のプロセスグループだけを回収します。子孫が setsid で群を抜けた場合は保証しません。
- Rust にしたことで、この範囲は広がっていません。旧版の不具合に当たる動き(SIGCHLD を無視で受け継いだ起動)も、旧版と同じにしています。

認めた旧版との差と、比較の前提の外は、全コマンドの決定の範囲だけです。時刻つきの一覧は [設計のR4Q節](../docs/design.md#全コマンド共通の決定の当て方r4q)にあります。
契約は [設計のR4Q節](../docs/design.md#r4q品質の実行の子の監督)にあります。
[比較の手順と件数](../docs/testing.md#r4qの比較)に従ってください。通常の`cargo test --locked`だけでは、旧版との比較は完了しません。

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
`Cargo.lock` は共有します。直接依存は `sha2`・`serde`・`serde_json`・C0bの `rustix`・R4Rの `libc 0.2.191`(`platform` だけ。R4Gも同じ宣言を使い、版も機能も変えません)と、R4Sのローカル部品 `workflow-yaml-parser 0.1.0`です。
`rustix` は `=1.1.6` に固定し、`platform` だけで `fs`・C0cの `event`・task-digestの`termios`と既定の `std` を使います。子の起動・KILL・回収は `std::process` です。`rustix` の `process`・ネットワーク機能は有効にしません。
R4Rも子を `std::process::Command` で起動します。ただし、子だけの `RLIMIT_FSIZE`・余分な fd を継がせない印・子の信号の既定と mask・旧版と同じ `execv` は、exec の前に子の中で動く処理が要ります。そのため `platform` の `restore_taskmd` だけが `pre_exec` と `libc` を使います。最初の信号を記録する処理と mask、Mac の地域設定の判定、環境の `LC_CTYPE` の書き換えも、同じファイルの `unsafe` に置きます。`rustix` の採用の根拠と、R4Rが `libc` と `unsafe` を使う理由は、[設計の記録](../docs/design.md#c0b-で採用した-os-呼出しの部品)にあります。
R4D-1(#481)は、閉じる失敗を返すために `platform` で `try_close` 機能も有効にしました。R4D-1 が足した自作の `unsafe` は、`platform/src/fs.rs` の `close_owned` の 1 か所です。理由は [設計のR4D-1節](../docs/design.md#診断の-reftable-の読み取りr4d-1) にあります。
R4Gも `platform` の `libc` を使います。R4G が足した自作の `unsafe` は、`platform` の `r4g` の3つのファイル(`startup.rs`・`fs.rs`・`process.rs`)だけに置き、`restore_taskmd` には足しません。理由と範囲は [設計のR4G節](../docs/design.md#保護記録の探索reviewsの配置内蔵のgit読取りr4g)にあります。
S3I-Aが起動するのは、本文の復元の GNU `cp` だけです(R4R の部品が起動します)。`platform` の `implement_guard` が、検査専用入口の OS の呼び出しに `libc` と `unsafe` を使います(並びの例は上の S3I-A の節)。Macの `strerror_text` は `restore_taskmd` に足しました。`rm -rf` の走査は `rustix` の `openat`・`Dir`・`statat`・`unlinkat` を使い、`unsafe` を増やしていません。
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
R4Uの比較は、`--r4u-compare` を付けない `cargo test --locked` では流れません(`check_links` のテストは `R4U の比較は流していない: ` で始まる1行を出して終了0)。流すときは `R4U_PYTHON` に本家(CPythonの公式の版)と同じ中身の Python 3.10.11 を渡し、Cコンパイラ `cc` を用意して、`cargo test --locked -p dev-workflow-cli --test check_links -- --r4u-compare` を流してください。渡さずに作業機の Ubuntu の 3.10.12 で流すと失敗します。CIでは必須ジョブ `r4u` が流します。[手順](../docs/testing.md#check_links-の比較r4u)を参照してください。

release の設定は `codegen-units=1`・`opt-level=3`・`lto="fat"`・`strip="symbols"`・`debug=0`・`incremental=false` です。
panic の設定は既定のままです。Linux のリンカーは Rust の musl 向けの既定を使います。
`.cargo/config.toml` と `.cargo/rustc-wrapper.sh` がソース・依存・出力先の絶対パスを固定名へ置き換えます。
wrapper は bash 3.2 以上で動きます。Cargo が渡す `CARGO_BIN_EXE_dev-workflow` もコンパイラへ引き継ぎます。
CI(変更ごとの自動検査)から `RUSTFLAGS` や最適化設定を渡す必要はありません。
外部の設定や環境変数でビルドの設定を上書きしないでください。
再現性の比較は同じ道具と設定を使い、別々の空の `CARGO_TARGET_DIR` で作った最終実行ファイルに `cmp` を使います。

テストでは、一時リポジトリの `native/` の外のファイルを変更し、計算値が不変であることを確認します。
追跡した本文・パス・Git のモードの変更では計算値が変わることも確認します。
