# Changelog

v4.2.0 以前は commit 履歴を参照。

## v4.3.0

### 変更

- 標準構成が `AGENTS.md` のみに(Claude Code 固有の指示は `.claude/rules/claude-code.md`)

### 後方互換を破る変更

- `source_of_truth` の値 `claude-md` を廃止し `agents-md` に改名した。有効な 3 値(`serena` / `docs` / `agents-md`)以外では skill が停止する

### 移行方法

- `.claude/project-profile.yml` の `source_of_truth: claude-md` を `source_of_truth: agents-md` に置換する
- `@AGENTS.md` を import する `CLAUDE.md` を持つ既存の構成(互換形)は引き続き正常。完成形への移行は `/init-project` の再実行で提案される
