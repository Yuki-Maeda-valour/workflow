# 候補モード(`--candidates`)

発見元(data-audit と create-task `--refactor`)の候補モードの正本。候補モードは、見つけた指摘を承認を待たずに task_dir の `候補_{名}.md`(設計書の手前の候補)として書き出す。対話点を出さず、改名・commit・チェーンの提案もしない。data-audit・create-task の SKILL.md には、各対話点に 1 行の分岐とこの文書への参照だけを置く(同じ事実を 2 か所に書かない)。

- `候補_` の書式・メタ行の照合パターン・採用の引き継ぎの正本は [task-template.md](task-template.md) の記法の規約。採用の経路の手順は [../SKILL.md](../SKILL.md) の「採用の経路」
- 識別子の定義は発見元の側に置く: data-audit は [../../data-audit/references/checks.md](../../data-audit/references/checks.md) の「候補モードの識別子と観点群」、refactor は [refactor-analysis.md](refactor-analysis.md) §6
- 無人ループの発見の周(`/ship-task --discover=<発見元> --unattended`)が候補モードを呼ぶ。周の照合・commit・PR は [../../ship-task/references/discover-mode.md](../../ship-task/references/discover-mode.md)
- `--candidates` を付けなければ、この文書は使わない(既存の「承認 → /create-task チェーン」のまま)

## 1. 引数

- `/data-audit --candidates [--layer=…] [--quick] [--max-candidates=N] [--unattended]`
- `/create-task --refactor [対象 | --area=…] --candidates [--max-candidates=N] [--unattended]`
- `--candidates` を明示したときだけ動く。既定はオフ(profile のスイッチは作らない)
- 上限の既定は data-audit 10・refactor 5。`--max-candidates=N` で変える
- 次のときは何も書かずに停止する(無人では失敗扱い)
  - create-task で `--refactor` が無い。`--compact`・`--light`・`--no-review`・`--runners` と併用した
  - `--unattended` を `--candidates` なしで渡した(この 2 つの skill に、候補モードのほかの無人の経路は無い)
  - `--max-candidates` を `--candidates` なしで渡した。N が 1 以上の整数でない

## 2. 指摘キー

同じ指摘の同一性を、行番号に頼らずに表す。**発見元が書く候補は、1 候補 = 1 キー**。

| 発見元 | 形 | 候補の単位と識別子の定義 |
|---|---|---|
| data-audit | `data-audit:{観点群}:{種類}:{識別子}` | checks.md の「候補モードの識別子と観点群」 |
| refactor | `refactor:file:{path}` | refactor-analysis.md §6 |
| stack-research・reflect-decisions | 別の Issue で決める(案: `stack-research:{パッケージ}:{勧告 ID}`・`reflect-decisions:{原資料の相対パス}#{番号}`) | ― |

- 例: `data-audit:authz:route:src/routes/orders.ts#ordersRouter:GET /:id` / `refactor:file:src/components/OrderForm.tsx`
- **正規化**(書く前と比べる前に必ずかける): NFC・前後の空白を削る・連続する空白を 1 つに・パスは管理ルート相対で `/` 区切り・先頭の `./` を付けない・改行を入れない。METHOD は大文字。`candidate-keys.py` は、このうち NFC と空白の 2 つを機械でかけて返す
- **既知の判定**: 候補のキーが、task_dir のどれかの状態名 MD のヘッダの指摘キー(複数行ならそのどれか)と一致すれば既知。見る範囲は全状態(`候補_`〈見送りを含む〉・`進行中_`・`完了_`・`保留_`・`中断_`)
- **回帰の疑い**: `完了_` のキーと一致したときは積まず、「完了した指摘がまた見つかった(回帰の疑い)」として報告と結果の行に件数を出す。ただし、同じキーが `完了_` と `完了_` 以外(採用で分割した残りの `進行中_` など)の両方にあるときは、回帰の疑いにしない(実装中の既知として数える)
- **キーの意味の帰結**: 見送り・既知の候補は、同じ観点群・同じ場所(refactor はファイル)の以後の指摘を**すべて**止める。同じ場所に後から生まれた同じ群の新しい問題(同じ handler への別の IDOR・同じファイルの新しいリファクタの問題)も、候補にならない

## 3. 対話点と工程の置き換え

無人の周の対話点の表([../../ship-task/references/unattended-mode.md](../../ship-task/references/unattended-mode.md) §4)からは、この表を指す。候補モードでは、`--unattended` の有無に関わらず、この表のとおりに振る舞う。

| 発見元 | 場所 | 対話(既定) | 候補モード |
|---|---|---|---|
| data-audit | Phase 0 の 1(`has_code: false`) | 対象外と報告して終える | 何も書かずに、結果の行 `候補なし` を返す |
| data-audit | Phase 0 の 2(把握) | /understand-project の実行を促す | 促さない。マニフェストから最小限のスタック把握を行う |
| data-audit | Phase 2(観点別スキャン) | そのまま | そのまま。無人では委託プロンプトに要点を写す(§7)。`--layer=frontend` 単独の F3 の「要バックエンド突合」は、候補にせず報告だけにする |
| data-audit | Phase 3(裏取り) | 裏取り・誤検知の除去・統合・レポートの保存 | そのまま(team-lead の裏取りは省けない)。ただし 3 の「同一根本原因の統合」は、キーの単位(同じ観点群・同じ識別子)の中だけで行う |
| data-audit | Phase 4(トリアージ) | AskUserQuestion で選ぶ。「要確認」は質問として示す | 尋ねない。選定(§4)で機械的に選ぶ。「要確認」は落とさず、確度 `要確認` の候補として書き、質問は候補の「人が確かめること」の節に置く |
| data-audit | Phase 5(チェーン) | グルーピングの提案と /create-task へのチェーン | しない。書き出し(§5)を行い、結果の行(§6)を返す |
| create-task | Phase 0 の 1・2(把握) | そのまま | そのまま |
| create-task | Phase 0 の 3(保存先) | 解決できなければ停止。無ければ作る | 書き出しの手順 1 で解く。ディレクトリは候補を書くときに作る |
| create-task | Phase 0 の 4(タスク名) | `進行中_{タスク名}.md` | 候補の名(task-template.md の「候補の書式」) |
| create-task | Phase 1 の 1(入口ガード) | 調査を先に行う提案 | しない |
| create-task | Phase 1 の 2(分解と種別) | 曖昧な点を AskUserQuestion で確かめる | 尋ねない。種別はリファクタ |
| create-task | Phase 1 の 2(リファクタの候補) | スコア付きで示し、どれをタスク化するか確かめる | 尋ねない。refactor-analysis.md §6 の足切りと選定(§4)で機械的に選ぶ |
| create-task | Phase 1 の 3・4(researcher の調査) | 現状・類似実装・影響・doc/06・03・07 | refactor-analysis.md §1〜3 と、候補に要る調査(before メトリクス・path:line・doc/06 の該当)だけ。team-lead がメトリクスを測り直して裏を取る。無人では委託プロンプトに要点を写す(§7) |
| create-task | Phase 1.5・2・3・4.5 | 影響範囲調査・タスク MD・checker・設計レビュー | 行わない(採用のときに create-task の checker と設計レビューが必ず走る) |
| create-task | 完了報告・一気通貫の提案 | 報告と `/ship-task` の提案 | 提案しない。結果の行(§6)を返す |

- 表に無い対話点が出たら、尋ねずに停止する(無人では失敗扱い)
- 独立レビュー: `候補_` には当てない。候補は設計の手前で、コード・設定・正本の doc ではない。採用のときに create-task の checker と設計レビューが必ず走る(design §5-23 の対象外)

## 4. 選定

- 並べ方の上位から、上限(§1)まで書く。既知で飛ばした指摘は上限に数えない。上限を超えた分は報告だけにする(翌晩に自然に出る)
- 並べ方
  - data-audit: 深刻度(重大 → 高 → 中)→ 確度(確実が先)→ checks.md の観点の順(候補の観点 ID のうち最も前のもの)→ キーの辞書順
  - refactor: スコアの降順 → キーの辞書順
- 足切りとセキュリティ由来の除外(refactor): refactor-analysis.md §6
- data-audit の「要確認」は落とさない(§3)

## 5. 書き出しの手順

1. task_dir を helper で解く(`python3 {create-task の}scripts/resolve-task-dir.py`。[task-directory.md](task-directory.md))。exit 1・2 は停止(無人では失敗扱い)。ship-task から呼ばれたときも、同じ helper で解く
2. `python3 {create-task の}scripts/candidate-keys.py --task-dir=<task_dir>` で、既知の指摘キーと名を集める。exit 0 以外は停止(無人では失敗扱い)
3. 指摘ごとにキーを正規化(§2)して、既知の集合と比べる。在れば書かず、報告にキー・既存のファイル・状態・見送りかを載せる(`完了_` なら回帰の疑い — §2)。同じ実行の中で重なったキーは、1 つにまとめる
4. `git status --porcelain=v1 --untracked-files=all` を控える(git の作業ツリーでなければ task_dir の直下の一覧)。名の衝突を解いてから(task-template.md の「候補の書式」の名)、選定の順で上限まで `Write` する。書くのは新しい `候補_*.md` だけで、既存の状態名 MD は書き換えない
5. 増えたのが task_dir の新しい `候補_*.md` だけかを、4 で控えたものと比べて確かめる。書いた各ファイルを `python3 {create-task の}scripts/candidate-keys.py --check --source=<発見元> <パス>` で確かめる。どちらかが通らなければ失敗扱い(書いたファイルは消さずに報告する)
6. 報告を `.claude/reviews/candidates-<発見元>.md` に保存する: 書いた候補・既知として除いたキーと既存のファイル・上限超え・回帰の疑い・除外(誤検知の除去・セキュリティ由来の除外)と理由・走査の範囲・未監査と理由。書式は次のとおり(見出しの字面を変えない。loop.sh が状態ディレクトリに写し、人と実走の確かめが読む)
   - `## 書いた候補`: `` - `<キー>` — <パス> `` を 1 行に 1 つ
   - `## 既知として除いた指摘`: `` - `<キー>` — <既存のファイル>(<状態名>) `` を 1 行に 1 つ。見送りの候補なら末尾に ` 見送り`、回帰の疑いなら ` 回帰の疑い` を足す
   - `## 上限超え`・`## 除外`(理由つき)・`## 走査の範囲と未監査`: 同じく 1 行に 1 つ
   - 無い節は見出しの下に `- なし` と書く。最後の行に結果の行(§6)を写す

**`candidate-keys.py`**(読み取り専用。`{create-task の}scripts/`)

- 収集: `--task-dir=<ディレクトリ>` → stdout に JSON `{"files": [{"file", "state", "name", "keys": [...], "skipped": <見送りの行の値 | null>, "problems": [...]}], "names": [...]}`
  - 対象は、直下の通常ファイル(symlink は除く)で、名が状態名の正規表現(`resolve-task-dir.py` の `STATE_FILE`)に全体一致するもの
  - ヘッダの指摘キーの行と見送りの行を読む。見送りの書式違いの行があれば、そのファイルの `problems` に出す(exit は変えない)
  - exit 0 = 成功 / 1 = 読めないファイルがある(UTF-8 として読めない・読み取りの失敗。そのファイルの `problems` に `unreadable`)/ 2 = 使い方の誤り(`--task-dir` が無い・ディレクトリでない)。ディレクトリが無いときは exit 0 で空の JSON(最初の候補)
- 検査: `--check [--source=<発見元>] <パス | ->`(`-` は stdin)→ stdout に JSON `{"ok": <真偽>, "source": <発見元 | null>, "keys": [...], "problems": [{"code", "detail"}, ...]}`
  - ok の条件: ヘッダに発見元の行がちょうど 1 行(`--source` があればその値と一致)・指摘キーの行がちょうど 1 行・無人実行のメタ行が無い・見送りの行と見送りの書式違いの行が無い・`## 追加修正記録` の見出しが無い・チェックボックス形の行(`^\s*- \[( |x|X)\]`。フェンスの中も)が無い
  - `code`: `source-count`(発見元の行が 1 行でない)・`source-mismatch`・`key-count`(指摘キーの行が 1 行でない)・`unattended-meta`・`skipped`(見送りの行)・`skipped-malformed`(見送りの書式違い)・`record-section`・`checkbox`・`unreadable`。`detail` は人向けの文で、判定に使わない。収集の `problems` も同じ形
  - exit 0 = ok / 1 = ok でない・読めない / 2 = 使い方の誤り
  - commit した候補は `git show HEAD:./<パス> | python3 {create-task の}scripts/candidate-keys.py --check --source=<発見元> -` の形で確かめられる(許可の仲介の構文に収まる)
- 取り込む兄弟の script(`resolve-task-dir.py`・`task-digest.py`)より前に `sys.dont_write_bytecode` を立て、プラグインルートに `__pycache__` を作らない(周の Bash では `PYTHONDONTWRITEBYTECODE` を渡せない)

## 6. 結果の行

書いたパスの一覧の後に、最後の行として結果の行を返す。

```
<task_dir>/候補_<名>.md
<task_dir>/候補_<名>.md
候補モードの結果: 書き出し — 書き出し 2 / 既知 1 / 上限超え 0 / 回帰の疑い 0 / 除外 3
```

- 書式: `候補モードの結果: (書き出し|候補なし|失敗扱い) — 書き出し N / 既知 M / 上限超え K / 回帰の疑い R / 除外 E`
- 照合パターン(`grep -E`): `^候補モードの結果: (書き出し|候補なし|失敗扱い) — 書き出し [0-9]+ / 既知 [0-9]+ / 上限超え [0-9]+ / 回帰の疑い [0-9]+ / 除外 [0-9]+$`。呼び出し元は、これに一致する**最後の行**を読む
- 一覧: 書いたパスを 1 行に 1 つ(管理ルート相対。ほかの文字を足さない)、結果の行の直前に空行を挟まずに並べる。要約などを書くときは、一覧の前に置き、空行で区切る
- `候補なし` = 書き出し 0(既知だけのときも含む)。`失敗扱い` = 停止した(対話でも無人でも)。N は書き終えた候補の数で、書きかけの `候補_` は残す
- M は既知として書かなかった指摘の数(回帰の疑いを含む)。R はそのうち回帰の疑いの数。K は上限を超えて書かなかった数。E は除外(誤検知の除去とセキュリティ由来の除外)の数
- refactor の E は、公開の PR 本文と結末の行に写さない(discover-mode.md)。内訳はローカルの報告(§5 の 6)だけに置く

## 7. 無人(`--unattended`)と許可の前提

- 周の Bash の書き方・委託するサブエージェントの要点・G1 は、unattended-mode.md のとおりに効く
- data-audit の Phase 2 の委託と、create-task の refactor の researcher の委託にも、unattended-mode.md の「委託するサブエージェント」の項の要点を、要約し直さずに委託プロンプトへ写す
- 引用符の外のグロブで一覧を取る書き方は、許可の仲介に拒否される。既知のキーは `candidate-keys.py` で集める
- 許可の拒否(G1)は、候補モードでは常に失敗扱い(`保留_` にするタスク MD が無い)。書きかけの `候補_` は残す(worktree ごと残る)
- **書き込みの範囲**: task_dir の新しい `候補_*.md` と、状態ファイル(`.claude/reviews/`・`.claude/grasp.md`)だけ。data-audit の原則 1 と create-task の原則 1 の例外
- **許可の前提**: `候補_` の `Write` は、保護パスの外の worktree の中への書き込みなので、編集の自動許可で確認に回らず、許可の仲介の hook に届かない(実装モードで do-task がソースやタスク MD を書くのと同じ経路)。task_dir が保護パスの下なら、`loop.sh` が起動時に止める([../../ship-task/references/loop.md](../../ship-task/references/loop.md) §3 の 3)。確認に回る構成(`loop.sh` の `--allow-classifier` で分類器が確認に回したときなど)では、hook が W の外として拒否し、G1 で失敗扱いになる

## 8. 最終ゲート(候補モード)

- [ ] 書き込みは task_dir の新しい `候補_*.md` と状態ファイルだけ(既存の状態名 MD・コード・ドキュメント・設定を変えていない)
- [ ] 書いた各候補が `candidate-keys.py --check --source=<発見元>` で ok
- [ ] 全指摘に path:line と実コードの裏取りがある(data-audit は Phase 3、refactor はメトリクスの測り直し)
- [ ] 機密の値を候補と報告に書いていない(値ではなく場所で書いた)
- [ ] 対話点を出していない(AskUserQuestion・承認・チェーン・一気通貫・入口ガードの提案)
- [ ] 報告を `.claude/reviews/candidates-<発見元>.md` に保存した
- [ ] 最後の行が結果の行で、N・M・K・R・E が報告と合う

## 9. 受け入れる限界

- 候補の中身の正しさ・機密の値の混入・候補の文面に仕込まれた指示は、機構では守らない(人が PR で読み、採用のときに create-task が実コードで裏を取る)。refactor の候補のセキュリティ由来の除外も、skill の規則で守るだけ
- 見送り・既知は、同じ観点群・同じ場所の以後の指摘をすべて止める(新しい問題が同じ場所に出ても候補にならない — §2)
- 識別子が変わる(ファイルの移動・改名、ルートの字面・メソッドの列の変更、登録先を囲むクラス・関数の改名)か、観点群の振り分けが変わると、同じ指摘が新しいキーでまた出うる(人が見送りを付ける)
- 指摘キーの無いタスク(人が直接作ったもの)とは、重複を照合できない。通常の「承認 → /create-task チェーン」でキーを引き継ぐかは別の Issue
- 候補を消して merge すると記録が残らないので、同じ指摘が後の晩にまた出うる(見送り = もう出さない / 消す = また出てよい)
