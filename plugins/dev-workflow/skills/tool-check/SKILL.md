---
name: tool-check
description: ツールによる機械検査(format/lint/typecheck/test/build)を一括実行する。「チェックして」「品質確認して」「lint かけて」「型チェックして」「テスト通して」「ビルド確認」等で使う。コマンドは profile→package.json scripts→設定ファイル検出→言語別既定の順で解決し、パッケージマネージャとモノレポ構成を自動判定する。--fix で自動修正(修正後に全ゲート再実行)、--only=<gate> で単一ゲートのみ。実際に動かして観察する実動確認はここでは行わない(/do-task の実動確認フェーズの責務)。
argument-hint: "[--fix | --only=<gate>]"
allowed-tools: Bash, Read, Glob
---

# tool-check — ツールによる機械検査の一括実行

format → lint → typecheck → test → build を、プロジェクトに実在するコマンドで順に実行する。**チェック 3 階層(design §5-19)のうち階層 1(静的検査)+ 2(自動テスト)を担う定型実行層** — タスク文脈に依存する階層 3(実動確認)は /do-task が担う。

## 原則

- **推測でコマンドを作らない。** 実在を確認したコマンドだけを実行する。
- 固有の事実(コマンド・PM・構成)は本文にハードコードせず、実行時に解決する(profile → 動的検出 → CLAUDE.md)。
- `has_code: false` の profile なら **品質ゲート対象外**(ドキュメントのみのプロジェクト)と報告して終了。

## 1. コマンド解決チェーン(この順)

1. **profile**: `.claude/project-profile.yml` の `quality`(`format`/`check`/`typecheck`/`test`/`build`)。`root` があればその配下で実行。`build_optional: true` なら build は既定でスキップ。profile が `check` を持つ場合は **format+lint 兼務ゲート** として扱う(下記の Biome と同じ)。
2. **package.json scripts の存在検出**: `format` / `check` / `lint` / `type-check`|`typecheck` / `test` / `build` を scripts から拾う。
   - **Biome の重複排除**: `check` スクリプトが `biome check`(format+lint 兼務)を呼ぶ場合、format と lint を個別に走らせない。`check` 単独を lint ゲートとして扱う。
3. **設定ファイル検出**(scripts に該当が無いゲートのみ): 設定ファイルの存在からツールを直接特定して実行する(実行形は PM に合わせ `pnpm exec` / `npx` 等)。例: biome.json → `biome check` / eslint.config.* → `eslint .` / .prettierrc* → `prettier --check .` / tsconfig.json → `tsc --noEmit` / vitest.config.* → `vitest run` / jest.config.* → `jest` / playwright.config.* → `playwright test`(E2E は test ゲートと別枠で報告)。
4. **言語別既定**(上記のいずれでも解決しないとき、マニフェストで判定):
   | 言語 | 検出ファイル | lint/format | test |
   |---|---|---|---|
   | PHP | composer.json | `vendor/bin/pint --test` | `php artisan test`(無ければ phpunit) |
   | Python | pyproject.toml / requirements.txt | `ruff check` + `ruff format --check` | `pytest` |
   | Go | go.mod | `go vet ./...` | `go test ./...` |
   | Rust | Cargo.toml | `cargo clippy` + `cargo fmt --check` | `cargo test` |

## 2. パッケージマネージャ判定(§3 標準ロジック)

`packageManager` フィールド → lockfile(`pnpm-lock.yaml`→pnpm / `bun.lock*`→bun / `yarn.lock`→yarn / `package-lock.json`→npm)→ 既定 npm。実行は `<pm> run <script>`。

- **bun 注意**: `bun test` を直接呼ばず `bun run test`(スクリプト経由)で実行する。

## 3. 実行順とゲート

`format` → `lint` → `typecheck` → `test` → `build` の順。build は `build_optional` 指定時・profile 未指定時はスキップ可(完了条件から除外)。前段が失敗しても後段を止めず全ゲートを走らせ、最後にまとめて結果を出す(全体像を一度で見せるため)。

## 4. 引数

- `--fix`: format/lint を修正モードで実行(`biome check --write` / `prettier --write` / `eslint --fix` / `ruff check --fix` + `ruff format` / `cargo fmt` / `pint`)。修正後に `git diff --stat` を提示し、**全ゲートを通常モードで再実行して緑を確認してから**報告する(修正した結果まだ赤いものを「修正済み」と報告しない)。
- `--only=<gate>`: 指定ゲートのみ実行(`format`|`lint`|`typecheck`|`test`|`build`)。

## 5. モノレポ対応

`pnpm-workspace.yaml` / `package.json#workspaces` を検出したら:

1. ルートに一括スクリプト(例 `check` / `test`)があればルートで一度だけ実行する。
2. 無ければ各 workspace パッケージを列挙し、それぞれで解決チェーンを回す。
3. パッケージごとに結果を分けて報告する。

## 最終ゲート(出力)

ゲートごとに ✅/❌/skip のサマリー表を出す:

| ゲート | コマンド | 結果 |
|---|---|---|
| format | … | ✅/❌/skip |
| lint | … | ✅/❌/skip |
| typecheck | … | ✅/❌/skip |
| test | … | ✅/❌/skip |
| build | … | ✅/❌/skip |

- 失敗ゲートは該当出力の **要点(エラー行)** を抜粋して示す(全ログは貼らない)。
- 自動修正で解決しうる失敗は `--fix` を案内する。
- 実在確認済みのコマンドだけを実行したか、最後に自己点検する。

## 関連

- 単体で動く。`do-task` の完了確認(Phase 5)と同じ規律。実際に動かして確認したいときは do-task の実動確認(または口頭依頼)。
- 失敗の修正は `do-task`(または軽微なら直接)で行う。
- 本スキルは構文・型の機械検査。データの通り道の意味的監査(機密露出・認可・過剰取得)は `/data-audit` が担う(補完関係)。
