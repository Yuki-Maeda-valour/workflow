# base誤検出修正

> **ステータス**: ✅ 完了(2026-09-11)
> **作成日**: 2026-09-11
> **関連**: https://github.com/Yuki-Maeda-valour/workflow/issues/44

## 概要
最新 origin/main (755c143) の検証で再現した一般変数名 base の誤検出を修正する。
ユーザー依頼: 最新を検証し、エラーを Issue 化して ship-task で修正・PR 作成まで行う。

## 現状分析
`scripts/validate.py:42` はホーム配下 dev のディレクトリ名を禁止語として収集する。
同44行の一般語除外に base がなく、`plugins/dev-workflow/skills/update-doc/scripts/check_links.py:53` の局所変数と同64・73・76行の参照がエラーになる。

### 対象ファイル
| ファイル | 現状 | 変更 |
|---|---|---|
| scripts/validate.py:44 | 一般語除外集合 | base を追加 |
| scripts/test_validate.py | 存在しない | 環境を隔離した回帰テスト |
| README.md:95 | 検証手順 | 回帰テストの実行方法 |

## 再現手順・原因分析
Issue #44 の一時ホームと Path.home の mock を用いて全体検証を実行すると、4件のエラーと終了値1を再現する。
一般語のディレクトリ名をプロジェクト固有名と誤認している。
各テストは Path.home を一時ホームへ mock した状態で runpy.run_path により新規ロードし、ERRORS/WARNS と禁止語を毎回初期化する。負例は一時コピーしたリポジトリをロードし、その検査対象ファイルに固有名または絶対パスを追加する。実リポジトリと実ホームは変更しない。

## 参考実装
`scripts/validate.py:44` の既存一般語除外集合を踏襲する。
Python 回帰テストは該当なし・新規パターン。標準 unittest / tempfile / mock を使い追加依存を避ける。

## スコープ
base の一般語除外、回帰テスト、README の検証手順同期。

## スコープ外
スキル本文・配布物・版数・リンク検査器の挙動変更は不要。配布対象を変更せず開発検証器を修正するため版数は維持する。
全12スキルの業務フロー実動試験は本不具合の完了条件に含めず、実施済みと報告しない。

## 実装タスク
### Phase 1: 回帰テストと修正
- [x] **Task 1.1**: scripts/test_validate.py に回帰を追加し、修正前に base ケースの失敗を確認する。
- [x] **Task 1.2**: scripts/validate.py の一般語除外集合に base を追加する。固有名の検査は維持する。
### Phase 2: ドキュメント同期
- [x] **Task 2.1**: README.md の検証節向けに回帰コマンドと一般語除外の説明を追加修正記録へ用意する。適用は実装完了後の update-doc 工程で行う。

## テスト計画
| テスト | 種別 | 対応 | 修正後の期待結果 |
|---|---|---|---|
| scripts/test_validate.py | integration | 新規 | 一時ホーム dev/base で全体検証が成功する |
| 同上 | integration | 新規 | 固有名ディレクトリと同名本文の混入を検知し終了値1 |
| 同上 | integration | 新規 | ユーザー絶対パスの混入を検知し終了値1 |
| scripts/validate.py | 既存検証 | 確認のみ | 通常環境でエラー・警告0 |

## 技術的考慮事項・横展開
一般語除外方式は既存契約を維持する最小修正。未知の一般語すべてを判別する変更はしない。
同型の収集処理を検索し、修正対象がこの1箇所であることを確認する。
DB/API/型伝播/エンティティ操作への影響なし。独立した作業ツリーで無関係な未追跡ファイルを混入させない。

## チェックリスト
- [x] 修正前の回帰テスト失敗を確認
- [x] 実装タスク完了
- [x] python3 -m unittest discover -s scripts -p 'test_*.py' が成功
- [x] python3 scripts/validate.py が成功
- [x] git diff --check が成功
- [x] Python 構文確認が成功
- [x] 独立レビューが APPROVED

## 完了条件
1. 一時ホームの dev/base を用いる回帰テストを実行し、全体検証の終了値0・エラー0を観測する。
2. 固有名とユーザー絶対パスを一時ツリーの検査対象に混入させ、終了値1と該当エラーを観測する。
3. 通常の規約検証が ERROR 0 / WARN 0、回帰テストが全件成功する。
4. README の検証手順を更新して実行確認し、Issue #44 に紐づく PR を作成する。
専用 formatter/typechecker/build は未設定。git diff --check と Python 構文検査を行う。
Codex の plugin validate は現 CLI で未対応。配布元の claude plugin validate は存在を確認して利用可能なら実行する。

## 追加修正記録

### 2026-09-11 / do-task 実装担当 / 反復 1

- 修正前確認: `test_base_directory_is_treated_as_generic` は終了値1で失敗し、`check_links.py` の局所変数 `base` を4件誤検出した。
- 実装: `_GENERIC_DIR_NAMES` に `base` を追加。検証器を一時コピーから毎回新規ロードする回帰テスト3件を追加し、一般語の成功、固有名とユーザー絶対パスの検知を確認した。
- 品質ゲート: unittest 3件成功。通常検証は ERROR 0 / WARN 0。`git diff --check` と `py_compile` が成功した。
- README 追記案: 検証コマンドへ `python3 -m unittest discover -s scripts -p 'test_*.py'` を追加し、`~/dev` のディレクトリ名検査では `base` などの一般語を除外する旨を説明する。適用は update-doc 工程で行う。
- reviewer: 内蔵の独立 reviewer が APPROVED(反復1)。team-lead が unittest 3件・通常検証・元の再現手順・Python構文・差分検査を再実行して成功。配布元 `claude plugin validate .` も成功。
- create-task: checker と能力帯を分けた2 reviewer で設計レビュー2回、全員承認。Phase 2 は未確定事項なしで続行。
- 機械突合: 実装2ファイル、テスト3件、未完了チェックなし。スコープ縮小の対象語句なし。README同期とPRは ship-task の後工程で実施する。
