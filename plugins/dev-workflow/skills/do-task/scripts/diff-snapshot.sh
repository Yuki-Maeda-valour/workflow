#!/usr/bin/env bash
# 基準からの差分スナップショットを 1 つの Markdown に生成する共有スクリプト(dev-workflow)。
# 呼び出し手順は ../references/diff-snapshot-call.md。
#
# 使い方:
#   bash diff-snapshot.sh --cwd <dir> --base <tree-ish> --out <file> \
#        (--exclude-glob <glob> | --exclude <ERE>)… [オプション]
#   bash diff-snapshot.sh --cwd <dir> --precheck [--accept <承認ダイジェスト>]
#   bash diff-snapshot.sh --print-exclude-ere (--exclude-glob <glob> | --exclude <ERE>)…
#
# 何を含むか:
#   基準(--base の tree)から現在の作業ツリーまでの**追跡差分**と、**未追跡ファイル**の内容。
#   追跡差分には index に stage しただけの変更も、基準の後に積んだ途中 commit の変更も入る
#   (比較先は常に作業ツリー)。新規ファイルだけの変更でも突合できる。
#   追跡側の比較は **stat 情報を持たない使い捨ての index** に対して行い、ユーザーの
#   `.git/index` は読むだけで書き換えない(mtime とサイズを合わせた隠蔽を通さないため)。
#   サブモジュールは gitlink(コミット ID)の変更だけを扱い、サブモジュール内の未コミットの
#   変更は出ない。
#
# 除外:
#   - `--exclude-glob` / `--exclude`(= 機密パスの指定)に一致したものは、追跡・未追跡・変更・
#     削除を問わず内容を出さず `## 除外(機密)` にパスだけ載せる。**許可リスト(例外)は無い** —
#     `.env.example` のようなテンプレート名も、指定に当たれば除外される
#   - 既定除外 `.claude/(reviews/|grasp.md|settings.local.json|.understand-project-done)` は常に
#     除外する。`reviews/`・`grasp.md`・`.understand-project-done` は skill と hook が置く揮発性の
#     状態ファイル(キャッシュ)、`settings.local.json` は commit しない個人設定で、いずれも
#     レビュー対象の成果物ではない
#   - `--out` / `--patch-out` 自身(と生成中の一時ディレクトリ)は `## 除外(既定)` に
#     理由「出力先自身」で載せる
#   - `--pre-untracked` の一覧にある未追跡ファイルは `## 基準時点から存在(対象外)` へ回し、
#     内容を出さない(タスク中に変更しても diff には出ない。含めたいときは
#     `--include-untracked <パス>` を付ける。`git add` は不要)
#   - `.claude/project-profile.yml` だけは機密指定の適用対象から常に外す(除外設定の変更が
#     必ず差分に出るようにするため)
#
# 別節:
#   `## 含められなかった未追跡` の理由は 9 種 — ネスト repo / リンク切れ / ディレクトリ等への
#   リンク / 列挙後に消失 / 通常ファイルでない / 読めない / diff 失敗 / `.git` という名前の
#   エントリ / 追跡ファイルの別表記(同一 inode)。
#   `## 内容を省略した未追跡` の理由は 3 種 — 1 MiB 超 / バイナリ / 総量上限。
#
# 引数(必須 / 任意):
#   --cwd <dir>                  必須。リポジトリ内の任意のディレクトリ(toplevel は自前解決する)
#   --base <tree-ish>            必須。比較の基準。空ツリーも可
#   --out <file>                 必須。生成先。呼び出し側 cwd 基準の相対 / 絶対。親は作る
#   --exclude-glob <glob>        繰り返し可。機密パスの glob 指定(下の書式)
#   --exclude-exception-glob <glob>
#                                繰り返し可。**除外に当たっても機密扱いにしない**パス
#                                (`.env.example` のような公開例)。--exclude-glob / --exclude と
#                                併せてのみ指定でき、全パスに当たる指定は usage エラー
#   --exclude <ERE>              繰り返し可。機密パスの拡張正規表現指定
#                                (--exclude-glob と --exclude のどちらか 1 つ以上が必須)
#   --pre-untracked <file>       任意。基準時点の未追跡一覧(NUL 区切り・toplevel 相対)。
#                                呼び出し側 cwd 基準。symlink でない読める通常ファイルに限る
#   --pre-untracked-sha256 <hex> --pre-untracked と対で必須。一覧の sha256
#   --include-untracked <path>   任意・繰り返し可。toplevel 相対。一覧にあるパスを本文に戻す
#   --max-untracked-bytes <N>    任意。未追跡本文の総量上限(既定 8388608)。0 で内容を出さない
#   --patch-out <file>           任意。一時ツリーへ適用するパッチを同じ除外で書き出す。
#                                指定先は**未存在であること**(既存なら exit 2)
#   --patch-base <tree-ish>      任意。パッチの基点(既定 HEAD)。--patch-out と対でのみ指定できる
#   --print-exclude-ere          任意。結合した除外 ERE を 1 行出して終わる(リポジトリ不要)
#   --precheck                   任意。改竄の事前検査だけを行う(--cwd だけ必須)
#   --accept <承認ダイジェスト>  任意・1 回だけ。ユーザーが承認した疑いの一覧を疑いから外す
#   -h / --help                  この使い方を出す
#
# 前提:
#   bash 4.0 以上(連想配列を使う)。満たさないときは起動直後に exit 2。
#   promisor remote の遅延取得は `GIT_NO_LAZY_FETCH=1` で止める(git 2.45 以上)。
#   2.45 未満で promisor 構成のリポジトリは、起動時のローカル設定検査で見つけた時点で
#   stderr へ `## 改竄の疑い` と同じ書式の行を出して exit 22(承認の対象外。`--out` は作らない。
#   属性検査・index フラグ検査・置換参照・`--base` の解決より前に止める)。
#
# 終了コード:
#   0  生成した
#   2  usage(引数・除外指定・出力先・一時ディレクトリの不備)
#   4  no-base(--base を解決できない)
#   20 internal(予期しない失敗。--out は作られない。**作業ツリーの追跡ファイルが読めない
#      (`chmod 000`)ときもここに落ちる** — 使い捨ての index には stat が無く git が全追跡
#      ファイルの内容を読むため、内容を変えていなくても `diff` が rc 128 で失敗する。読めない
#      未追跡(21 で続行)と違い snapshot を作れないので fail-closed。DoS の類型で受け入れる限界。
#      promisor remote(部分クローン)で基準側のオブジェクトが手元に無いときも、遅延取得を
#      止めているため `diff` が失敗して 20 になる — 先にオブジェクトを取得してから再実行する)
#   21 partial(生成したが `## 含められなかった未追跡` に失敗〈読めない・diff 失敗〉がある)
#   22 改竄の疑い(`## 改竄の疑い` が非空。21 と同時なら 22。呼び出し側は続行しない)
#
# --precheck:
#   内容変換を起動しない検査(ローカル設定・作業ツリーの包含・属性・index フラグ)だけを行う。
#   終了コードは 0 / 2 / 20 / 22 で、**呼び出し側は exit 0 のときだけ続行する**。
#   見出しを持たないので NOTE は stderr に出す。疑いも承認も無効化も無ければ出力は空。
#   承認された filter があるときだけ、無効化に使う環境変数の指定を stdout へ
#   `NAME=VALUE` の NUL 終端トークン(最後のトークンの後にも NUL)で出す。呼び出し側は
#   `read -r -d ''` で 1 つずつ受けて `export "$トークン"` する。
#   **`--accept` 付きの通常実行(`--precheck` でないとき)でも、承認済みの filter が
#   あれば同じトークンを stdout に出す**(呼び出し側は同じ読み方で受ける)。
#
# --accept <承認ダイジェスト>:
#   exit 22 のとき stderr の最後に出る `承認ダイジェスト:` の値を渡す。**提示された疑いの
#   一覧の全行をユーザーが明示的に承認したときだけ**渡すもので、一覧が 1 行でも変われば
#   一致せず exit 22 に戻る。承認した項目は見出しの NOTE に出る。
#   `core.worktree` の差し替え・`.git/info/attributes` が通常ファイルでない・事後検出の
#   `Binary files 検出`・`ident` / `working-tree-encoding` 属性・git 2.45 未満での promisor 構成は
#   承認の対象外。
#   **承認した filter は実行せず、作業ツリーの実内容で比較する**。
#   exit 22 のときの出力: 事前検査の疑いでは本文を生成せず、11 節に固定文字列を書く。
#   `--patch-out` は公開しない。`core.worktree` の差し替えと、git 2.45 未満での promisor 構成では
#   `--out` も作らない。
#
# 改竄耐性の 3 節:
#   `## 内容を省略した追跡`  内容を本文に出さなかった追跡パス。理由は `バイナリ` /
#                            `1 MiB 超` / `作業ツリーが通常ファイルでない`(stat には残る)
#   `## 改竄の疑い`          差分を隠せる設定・属性・index フラグ。行の書式は
#                            `<パス または キー名>\t<理由>\t<内容ダイジェスト>`(設定の行だけ
#                            4 列目に表示用の値 — 制御文字を `?` に置き換え 200 文字で切ったもの)。
#                            stderr と `--out` に書くとき(`## 改竄の疑い` の行と、NOTE
#                            「承認済みとして扱った項目」の列挙と、NOTE「承認済みの filter を
#                            実行せず実内容で比較した」の filter 名)は、すべての列に同じ制御文字の
#                            置換を掛ける。内容ダイジェストと承認ダイジェストは置換・切り詰めの
#                            前のバイト列から計算する。理由は `ローカル設定: <キー名>` /
#                            `include: <キー名>` / `.git/info/attributes` /
#                            `.git/info/attributes が通常ファイルでない` / `filter 属性: <値>` /
#                            `working-tree-encoding 属性: <値>` / `ident 属性: <値>` /
#                            `Binary files 検出` / `index フラグ: assume-unchanged` /
#                            `index フラグ: skip-worktree`。内容ダイジェストは設定値または
#                            ファイルの sha256、それ以外は `-`。
#                            **承認ダイジェスト**は、事前検査の全行の先頭 3 列をタブで連結して
#                            NUL で終端したレコードを `LC_ALL=C sort -z` で並べた連結の sha256
#   `## gitignore により除外(.gitignore 以外)`
#                            **この節だけはパスだけの行**(タブも属性も無い。引用の規則は同じで、
#                            照合側はタブが無い行を行全体でパス列として扱う)。
#                            `.git/info/exclude` / `core.excludesFile` で隠れた未追跡のパス
#                            (内容は出さない)。**`.gitignore` 由来で ignore された未追跡は
#                            列挙されず本文にもどの節にも現れない**(`git ls-files -o --ignored
#                            --exclude-standard` で確認する)
#
# 見出しの NOTE:
#   `core.fsmonitor` / `core.hookspath` を無効化して実行した(キー名は git の出力どおり小文字)/
#   `core.filemode` / `core.symlinks` / `core.ignorecase` を無効化して実行した /
#   `core.pager` / `pager.*` を無効化して実行した / 置換参照を無効化して実行 /
#   追跡ファイルの別表記(同一 inode)の件数 / `.git` という名前のエントリの件数 /
#   サブモジュール配下の `.git` 名エントリの件数とパス /
#   `.git` 名エントリ・ディレクトリの走査に失敗した経路あり / `.git/info/exclude` あり /
#   `.gitignore` が基準から変更または未追跡で追加(と該当パス〈未追跡のもの〉)/
#   gitignore 済みの未追跡の件数 /
#   `.gitattributes` が変更された(と該当パス〈未追跡のもの〉)/
#   `filter=lfs` のパスを実内容で比較した /
#   `core.ignorestat` を無効化して実行した / promisor 構成: 遅延取得を無効化して実行 /
#   承認済みとして扱った項目 / 承認済みの filter を実行せず実内容で比較した /
#   sparse-checkout で作業ツリーに無い skip-worktree の件数とパス /
#   未追跡の総量上限に達した / `--pre-untracked` の指定が無く全未追跡を対象にした /
#   対象外の分類はリポジトリ内の一覧に基づく
#
# `## 基準時点から存在(対象外)` の行:
#   `<パス>\t<種別>\t<sha256 または ->\t<バイト数 または ->`。種別は `ネスト repo` /
#   `symlink` / `消失` / `通常ファイルでない` / `読めない` / `通常ファイル(1 MiB 超)` /
#   `通常ファイル`。**内容を読むのは 1 MiB 以下の通常ファイルだけ**で、symlink はリンク先を
#   読まずリンク文字列のハッシュを載せる。
#
# `filter=lfs`:
#   本文・パッチ・未追跡の diff では LFS の clean フィルタを通さず**作業ツリーの実内容**で
#   比較する(列挙と stat はポインタのまま比較する)。該当パスがあれば NOTE を出す。
#
# `--exclude-glob` の glob 書式:
#   `*` は `/` を跨がない任意長、`**` は `/` を跨ぐ任意長(`**/` はゼロ階層も含む)、
#   `?` は `/` 以外の 1 文字。末尾の `/` は「そのディレクトリ配下全体」、先頭の `/` は
#   「リポジトリのルート固定」を表す。`//`・空の要素・引用符 / 改行 / 制御文字 /
#   バッククォート / `$` / ブラケット `[` `]` / ブレース `{` `}` を含む要素は受け付けない
#   (黙って「1 件も当たらない除外」に化けるのを防ぐため exit 2)。
#
# パスの書き方:
#   `--cwd` はリポジトリ内の任意のディレクトリ(toplevel を自前解決する)。
#   `--out` / `--pre-untracked` / `--patch-out` は呼び出し側 cwd 基準(絶対パスも可)。
#   節の各行は toplevel 相対のパス + タブ + 属性で、パスにタブ・改行・その他の制御文字・
#   `"`・`\` を含むか先頭が `"` のものは C 風引用(`"…"` で囲み `\t` `\n` `\\` `\"`、
#   その他の制御文字は 8 進 `\ooo`)で書く。
#
# 基準時点の未追跡一覧:
#   再計算できない最良努力の任意入力で、欠落しても動作は止まらず未追跡の対象集合が広がるだけ。
#
# その他:
#   `TMPDIR` がリポジトリ内を指す環境では、一時ファイルが未追跡として混入するため exit 2。
# --- end usage ---
set -eEuo pipefail
export LC_ALL=C
# 連想配列を使うので bash 4.0 以上が要る(引数解析より前に見る)
if [ "${BASH_VERSINFO[0]:-0}" -lt 4 ]; then
  echo "ERROR [usage] bash 4.0 以上が必要" >&2
  exit 2
fi
# promisor remote の遅延取得を止める(取得は `remote.<名>.uploadpack` で任意コマンドを起動できる)
export GIT_NO_LAZY_FETCH=1

# ── 引数の受け皿 ──
CWD=""
BASE=""
OUT=""
GLOBS=()
EXCEPT_GLOBS=()   # 除外の例外(--exclude-exception-glob)。除外に当たっても機密扱いにしない
EXCEPT_ERE=""
ERES=()
PRE_LIST=""
PRE_LIST_GIVEN=0
PRE_SHA=""
PRE_SHA_GIVEN=0
INCLUDES=()
MAX_BYTES=8388608
PATCH_OUT=""
PATCH_BASE="HEAD"
PATCH_BASE_GIVEN=0
PRINT_ERE=0
PRECHECK=0
ACCEPT=""
ACCEPT_GIVEN=0

# LFS の clean フィルタを通さず実内容で比較するための上書き。**この 4 つだけ**変数に置き、
# 本文・パッチ・未追跡の diff の行でだけ展開する(git 前置きをリテラルで書く規約の例外)。
LFS_OFF='-c filter.lfs.clean= -c filter.lfs.smudge= -c filter.lfs.process= -c filter.lfs.required=false'

TOP=""
TMPD=""
PUB=""
PUB2=""
OUT_ABS=""
PATCH_ABS=""
SHA_CMD=""
FAILED=0          # 読めない・diff 失敗の件数(1 以上なら exit 21)
TAMPER=0          # 改竄の疑いあり(exit 22)
STOP_NOW=0        # これ以上 git にそのファイルを読ませてはいけない(属性ファイルが通常ファイルでない)
DEFAULT_EXCLUDE='(^|/)\.claude/(reviews/|grasp\.md$|settings\.local\.json$|\.understand-project-done$)'
PROFILE_PATH='.claude/project-profile.yml'
ERE=""

# 改竄の疑いの行(1 列目・2 列目・3 列目・4 列目・承認できるか)
TC1=(); TC2=(); TC3=(); TC4=(); TOK=()
NOTES=()          # 見出しの NOTE(--precheck では stderr)
ACCEPT_TOKENS=()  # 承認した filter を無効化する NAME=VALUE トークン

cleanup_publish() { if [ -n "$PATCH_OUT" ] && [ -n "${PUB2:-}" ] && [ -n "${PUB:-}" ] && [ ! -e "$PUB2/patch.tmp" ] && [ -e "$PUB/out.tmp" ]; then rm -f -- "$PATCH_ABS"; fi; }
cleanup() {
  cleanup_publish
  if [ -n "${PUB:-}" ]; then rm -rf -- "$PUB"; fi
  if [ -n "${PUB2:-}" ]; then rm -rf -- "$PUB2"; fi
  if [ -n "${TMPD:-}" ]; then rm -rf -- "$TMPD"; fi
}
on_signal() { cleanup; trap - EXIT; exit 20; }
trap cleanup EXIT
trap on_signal TERM INT HUP
trap 'ec=$?; echo "ERROR [internal] 予期しない失敗(終了コード $ec・行 $LINENO)" >&2; exit 20' ERR

# ヘッダのコメント全体を使い方として出す(番兵で範囲が自動追従する)
usage() { sed -n '2,/^# --- end usage ---$/p' "$0" | sed '$d' >&2; }

fail_usage() { echo "ERROR [usage] $*" >&2; trap - ERR; exit 2; }
fail_nobase() { echo "ERROR [no-base] $*" >&2; trap - ERR; exit 4; }
fail_internal() { echo "ERROR [internal] $*" >&2; trap - ERR; exit 20; }

need_val() { # $1=オプション名 $2=残り引数の個数
  if [ "$2" -lt 2 ]; then
    echo "ERROR [usage] $1 に値が必要です" >&2
    usage
    trap - ERR
    exit 2
  fi
}

# ── sha256(コマンドの解決順は一覧のダイジェストと承認ダイジェストで共通)──
resolve_sha_cmd() {
  if command -v sha256sum >/dev/null 2>&1; then SHA_CMD=sha256sum
  elif command -v shasum >/dev/null 2>&1; then SHA_CMD=shasum
  elif command -v openssl >/dev/null 2>&1; then SHA_CMD=openssl
  else SHA_CMD=""; fi
}
sha256_file() { # $1=ファイル → 16 進を stdout へ
  case "$SHA_CMD" in
    sha256sum) sha256sum <"$1" | cut -d' ' -f1 ;;
    shasum) shasum -a 256 <"$1" | cut -d' ' -f1 ;;
    openssl) openssl dgst -sha256 <"$1" | sed 's/.*[= ]//' ;;
    *) return 1 ;;
  esac
}
sha256_str() { printf '%s' "$1" >"$TMPD/.sha.in"; sha256_file "$TMPD/.sha.in"; }

# ── C 風引用(git の core.quotePath と同じ規則)──
needs_quote() {
  case "$1" in
    '"'*) return 0 ;;
    *'"'*|*'\'*) return 0 ;;
    *[[:cntrl:]]*) return 0 ;;
  esac
  return 1
}
cquote() { # $1=パス → C 風引用した文字列
  local s="$1" out='"' i ch code
  for (( i = 0; i < ${#s}; i++ )); do
    ch="${s:i:1}"
    case "$ch" in
      '"') out="$out\\\"" ;;
      '\') out="$out\\\\" ;;
      $'\a') out="$out\\a" ;;
      $'\b') out="$out\\b" ;;
      $'\t') out="$out\\t" ;;
      $'\n') out="$out\\n" ;;
      $'\v') out="$out\\v" ;;
      $'\f') out="$out\\f" ;;
      $'\r') out="$out\\r" ;;
      *)
        case "$ch" in
          [[:cntrl:]]) printf -v code '\\%03o' "'$ch"; out="$out$code" ;;
          *) out="$out$ch" ;;
        esac
        ;;
    esac
  done
  printf '%s"' "$out"
}
path_field() { if needs_quote "$1"; then cquote "$1"; else printf '%s' "$1"; fi; }

sec_line() { # $1=節ファイル $2=パス $3…=属性
  local file="$1" out a
  out="$(path_field "$2")"
  shift 2
  for a in "$@"; do out="$out"$'\t'"$a"; done
  printf '%s\n' "$out" >>"$file"
}

# 表示の無害化(制御文字を ? に置き換える。3 列目までは切り詰めない)
sanitize() { printf '%s' "$1" | LC_ALL=C tr '[:cntrl:]' '?'; }

# 表示用の値(4 列目)を 200 文字で切る。LC_ALL=C 固定なのでシェルの部分文字列はバイト単位に
# なるため、UTF-8 の先頭バイトから文字の長さを見て進め、文字の途中では切らない
# (不正なバイト列を出さない。外部コマンドには頼らない)
CUT200=""
cut200() { # $1=文字列 → CUT200 に先頭 200 文字を入れる
  local s="$1"
  local total=${#s} i=0 n=0 b len
  while [ "$i" -lt "$total" ]; do
    if [ "$n" -ge 200 ]; then CUT200="${s:0:i}"; return 0; fi
    b="${s:i:1}"
    case "$b" in
      [$'\xc2'-$'\xdf']) len=2 ;;
      [$'\xe0'-$'\xef']) len=3 ;;
      [$'\xf0'-$'\xf4']) len=4 ;;
      *) len=1 ;;                    # ASCII と、単独で現れた継続バイトは 1 文字として数える
    esac
    i=$((i + len))
    n=$((n + 1))
  done
  CUT200="$s"
}
# 4 列目の表示用の値: 制御文字を置き換えてから 200 文字で切る。
# **内容ダイジェスト(3 列目)と承認ダイジェストは切る前の値から計算する**(ここでは触らない)
disp_val() { local t; t="$(sanitize "$1")"; cut200 "$t"; printf '%s' "$CUT200"; }

add_note() { NOTES[${#NOTES[@]}]="$1"; }

add_tamper() { # $1=1 列目 $2=理由 $3=内容ダイジェスト $4=表示用の値 $5=承認できるか(1/0)
  TC1[${#TC1[@]}]="$1"; TC2[${#TC2[@]}]="$2"; TC3[${#TC3[@]}]="$3"
  TC4[${#TC4[@]}]="$4"; TOK[${#TOK[@]}]="$5"
  TAMPER=1
}

# 除外・制御ファイル名の照合(NUL でパス全体を 1 レコードとして、大小文字を区別せずに当てる)
ere_hit() { printf '%s' "$1" | LC_ALL=C grep -Eiqz -- "$2"; }

# 絶対化・正規化(realpath → readlink -f → cd + pwd -P)
abs_existing_dir() { # $1=存在するディレクトリ(失敗したら空を返す)
  local r=""
  if command -v realpath >/dev/null 2>&1; then r="$(realpath -- "$1" 2>/dev/null || printf '')"; fi
  if [ -z "$r" ] && command -v readlink >/dev/null 2>&1; then r="$(readlink -f -- "$1" 2>/dev/null || printf '')"; fi
  if [ -z "$r" ]; then r="$(CDPATH= cd -P -- "$1" >/dev/null 2>&1 && pwd -P || printf '')"; fi
  printf '%s' "$r"
}
abs_maybe() { # $1=存在しなくてもよいパス(失敗したら空を返す)
  local r=""
  if command -v realpath >/dev/null 2>&1; then r="$(realpath -m -- "$1" 2>/dev/null || printf '')"; fi
  if [ -z "$r" ] && command -v readlink >/dev/null 2>&1; then r="$(readlink -f -- "$1" 2>/dev/null || printf '')"; fi
  if [ -z "$r" ]; then r="$(CDPATH= cd -P -- "$1" >/dev/null 2>&1 && pwd -P || printf '')"; fi
  printf '%s' "$r"
}

# ── 引数解析 ──
while [ $# -gt 0 ]; do
  case "$1" in
    --cwd) need_val "$1" "$#"; CWD="$2"; shift 2 ;;
    --base) need_val "$1" "$#"; BASE="$2"; shift 2 ;;
    --out) need_val "$1" "$#"; OUT="$2"; shift 2 ;;
    --exclude-glob) need_val "$1" "$#"; GLOBS[${#GLOBS[@]}]="$2"; shift 2 ;;
    --exclude-exception-glob) need_val "$1" "$#"; EXCEPT_GLOBS[${#EXCEPT_GLOBS[@]}]="$2"; shift 2 ;;
    --exclude) need_val "$1" "$#"; ERES[${#ERES[@]}]="$2"; shift 2 ;;
    --pre-untracked) need_val "$1" "$#"; PRE_LIST="$2"; PRE_LIST_GIVEN=1; shift 2 ;;
    --pre-untracked-sha256) need_val "$1" "$#"; PRE_SHA="$2"; PRE_SHA_GIVEN=1; shift 2 ;;
    --include-untracked) need_val "$1" "$#"; INCLUDES[${#INCLUDES[@]}]="$2"; shift 2 ;;
    --max-untracked-bytes) need_val "$1" "$#"; MAX_BYTES="$2"; shift 2 ;;
    --patch-out) need_val "$1" "$#"; PATCH_OUT="$2"; shift 2 ;;
    --patch-base) need_val "$1" "$#"; PATCH_BASE="$2"; PATCH_BASE_GIVEN=1; shift 2 ;;
    --print-exclude-ere) PRINT_ERE=1; shift ;;
    --precheck) PRECHECK=1; shift ;;
    --accept)
      need_val "$1" "$#"
      if [ "$ACCEPT_GIVEN" -eq 1 ]; then fail_usage "--accept は 1 回だけ指定できる"; fi
      ACCEPT="$2"; ACCEPT_GIVEN=1; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR [usage] 不明な引数: $1" >&2; usage; trap - ERR; exit 2 ;;
  esac
done

if [ "$PRINT_ERE" -eq 1 ] && [ "$PRECHECK" -eq 1 ]; then fail_usage "--print-exclude-ere と --precheck は併用できない"; fi

if [ "$PRINT_ERE" -eq 1 ]; then
  # 除外指定だけが必須。それ以外の引数との併用は受け付けない
  if [ -n "$CWD" ] || [ -n "$BASE" ] || [ -n "$OUT" ] || [ -n "$PATCH_OUT" ] || [ -n "$PRE_LIST" ] \
     || [ "$PRE_SHA_GIVEN" -eq 1 ] || [ "$PATCH_BASE_GIVEN" -eq 1 ] || [ "$ACCEPT_GIVEN" -eq 1 ] \
     || [ ${#INCLUDES[@]} -gt 0 ] || [ "$MAX_BYTES" != 8388608 ]; then
    fail_usage "--print-exclude-ere は除外指定以外の引数と併用できない"
  fi
elif [ "$PRECHECK" -eq 1 ]; then
  # --cwd だけ必須。--accept 以外の引数との併用は受け付けない
  if [ -n "$BASE" ] || [ -n "$OUT" ] || [ -n "$PATCH_OUT" ] || [ -n "$PRE_LIST" ] \
     || [ "$PRE_SHA_GIVEN" -eq 1 ] || [ "$PATCH_BASE_GIVEN" -eq 1 ] \
     || [ ${#INCLUDES[@]} -gt 0 ] || [ ${#GLOBS[@]} -gt 0 ] || [ ${#ERES[@]} -gt 0 ] \
     || [ "$MAX_BYTES" != 8388608 ]; then
    fail_usage "--precheck は --cwd と --accept 以外の引数と併用できない"
  fi
  [ -n "$CWD" ] || fail_usage "--cwd が必要です"
else
  [ -n "$CWD" ] || fail_usage "--cwd が必要です"
  [ -n "$BASE" ] || fail_usage "--base が必要です"
  [ -n "$OUT" ] || fail_usage "--out が必要です"
  if [ "$PATCH_BASE_GIVEN" -eq 1 ] && [ -z "$PATCH_OUT" ]; then
    fail_usage "--patch-base は --patch-out と対でのみ指定できる"
  fi
  if [ "$PRE_LIST_GIVEN" -ne "$PRE_SHA_GIVEN" ]; then
    fail_usage "--pre-untracked と --pre-untracked-sha256 は対で指定する"
  fi
  case "$MAX_BYTES" in
    ''|*[!0-9]*) fail_usage "--max-untracked-bytes は非負の十進整数" ;;
  esac
fi

# ── 除外指定 → 結合 ERE(glob の変換はこのスクリプトが行う)──
if [ "$PRECHECK" -eq 0 ]; then
  if [ ${#GLOBS[@]} -eq 0 ] && [ ${#ERES[@]} -eq 0 ]; then
    fail_usage "--exclude-glob か --exclude のどちらか 1 つ以上が必要です"
  fi
fi

E1=$'\001'; E2=$'\002'; E3=$'\003'
RE_ANY_DIR='(.*/)?'
RE_ANY='.*'
RE_SEG='[^/]*'
RE_ONE='[^/]'

check_glob_raw() { # ⓪ 生の要素の検査(変換より先に、親シェルで全要素を 1 巡して行う)
  case "$1" in
    '') fail_usage "--exclude-glob に空の値は指定できない" ;;
    *//*) fail_usage "--exclude-glob に '//' は使えない: $1" ;;
  esac
  case "$1" in
    *\'*|*'"'*|*'`'*|*'$'*|*'['*|*']'*|*'{'*|*'}'*)
      fail_usage "--exclude-glob に引用符・バッククォート・\$・ブラケット・ブレースは使えない(黙って非マッチになるため): $1" ;;
  esac
  case "$1" in
    *[[:cntrl:]]*) fail_usage "--exclude-glob に改行・制御文字は使えません" ;;
  esac
  # 末尾 / と先頭 / を外すと空になる要素も、変換に入る前に(親シェルで)弾く
  local t="$1"
  t="${t%/}"
  t="${t#/}"
  [ -n "$t" ] || fail_usage "--exclude-glob の要素が空になる: $1"
}

glob_to_ere() { # $1=生の glob 要素 → ERE を stdout へ(検査は check_glob_raw で済んでいる)
  local g="$1" anchor='(^|/)'
  g="${g%/}"
  case "$g" in
    /*) g="${g#/}"; anchor='^' ;;
  esac
  g="${g//\*\*/$E1}"
  g="${g//\*/$E2}"
  g="${g//\?/$E3}"
  g="${g//\\/\\\\}"
  g="${g//./\\.}"
  g="${g//^/\\^}"
  g="${g//\$/\\\$}"
  g="${g//+/\\+}"
  g="${g//(/\\(}"
  g="${g//)/\\)}"
  g="${g//[/\\[}"
  g="${g//]/\\]}"
  g="${g//\{/\\\{}"
  g="${g//\}/\\\}}"
  g="${g//|/\\|}"
  g="${g//${E1}\//$RE_ANY_DIR}"
  g="${g//$E1/$RE_ANY}"
  g="${g//$E2/$RE_SEG}"
  g="${g//$E3/$RE_ONE}"
  printf '%s%s($|/)' "$anchor" "$g"
}

validate_one_ere() { # $1=ERE(不正なら exit 2)
  local rc2=0
  printf '' | grep -Eqz -- "$1" 2>/dev/null || rc2=$?
  if [ "$rc2" -ge 2 ]; then fail_usage "除外の正規表現が不正"; fi
}

REP_PATHS=('x' 'a/b/c.ts' '.claude/x' 'src/app.ts' 'README.md')
matches_all_paths() { # $1=ERE
  local p
  for p in "${REP_PATHS[@]}"; do
    if ! ere_hit "$p" "$1"; then return 1; fi
  done
  return 0
}

build_ere() {
  local g e parts=() one
  for g in ${GLOBS[@]+"${GLOBS[@]}"}; do check_glob_raw "$g"; done
  for g in ${GLOBS[@]+"${GLOBS[@]}"}; do
    one="$(glob_to_ere "$g")"
    validate_one_ere "$one"
    if matches_all_paths "$one"; then fail_usage "secret_paths が全パスに当たる: $g"; fi
    parts[${#parts[@]}]="$one"
  done
  for e in ${ERES[@]+"${ERES[@]}"}; do
    [ -n "$e" ] || fail_usage "--exclude に空の値は指定できない"
    validate_one_ere "$e"
    if matches_all_paths "$e"; then fail_usage "secret_paths が全パスに当たる: $e"; fi
    parts[${#parts[@]}]="$e"
  done
  local acc=""
  for one in ${parts[@]+"${parts[@]}"}; do
    if [ -z "$acc" ]; then acc="$one"; else acc="$acc|$one"; fi
  done
  ERE="$acc"
}

build_except_ere() { # EXCEPT_GLOBS → EXCEPT_ERE。書式検査は --exclude-glob と同じものを使う
  local g parts=() one acc=""
  for g in ${EXCEPT_GLOBS[@]+"${EXCEPT_GLOBS[@]}"}; do check_glob_raw "$g"; done
  for g in ${EXCEPT_GLOBS[@]+"${EXCEPT_GLOBS[@]}"}; do
    one="$(glob_to_ere "$g")"
    validate_one_ere "$one"
    # 例外が全パスに当たると除外が丸ごと無効になる(機密が素通りする)ので止める
    if matches_all_paths "$one"; then fail_usage "除外の例外が全パスに当たる: $g"; fi
    parts[${#parts[@]}]="$one"
  done
  for one in ${parts[@]+"${parts[@]}"}; do
    if [ -z "$acc" ]; then acc="$one"; else acc="$acc|$one"; fi
  done
  EXCEPT_ERE="$acc"
}

validate_ere() { # 結合 ERE を 1 回だけ検証する(rc 1 は妥当な非マッチ)
  local rc=0
  printf '' | grep -Eqz -- "$ERE" 2>/dev/null || rc=$?
  if [ "$rc" -ge 2 ]; then fail_usage "除外の正規表現が不正"; fi
}

if [ ${#GLOBS[@]} -gt 0 ] || [ ${#ERES[@]} -gt 0 ]; then
  build_ere
  validate_ere
  if matches_all_paths "$ERE"; then fail_usage "secret_paths が全パスに当たる: $ERE"; fi
fi
if [ ${#EXCEPT_GLOBS[@]} -gt 0 ]; then
  [ -n "$ERE" ] || fail_usage "--exclude-exception-glob は --exclude-glob / --exclude と併せて指定する"
  build_except_ere
fi

if [ "$PRINT_ERE" -eq 1 ]; then
  printf '%s\n' "$ERE"
  trap - ERR
  exit 0
fi

# ── toplevel の自前解決 ──
[ -d "$CWD" ] || fail_usage "--cwd が存在しない: $CWD"
TOP_RAW=""
rc=0
TOP_RAW="$(git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false -C "$CWD" rev-parse --show-toplevel 2>/dev/null)" || rc=$?
if [ "$rc" -ne 0 ] || [ -z "$TOP_RAW" ]; then fail_usage "--cwd が git リポジトリの中でない: $CWD"; fi
TOP="$(abs_existing_dir "$TOP_RAW")"
[ -n "$TOP" ] || fail_usage "toplevel の物理パスを解決できない: $TOP_RAW"

# ①′ 作業ツリーの包含検査(toplevel へ cd する前・--out も一時ディレクトリも作る前に判定する)
CWD_PHYS="$(abs_existing_dir "$CWD")"
case "${CWD_PHYS:-/dev/null/none}" in
  "$TOP"|"$TOP"/*) : ;;
  *)
    rc=0
    WT_VAL="$(git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false -C "$CWD" config --get core.worktree 2>/dev/null)" || rc=$?
    if [ "$rc" -ne 0 ]; then WT_VAL=""; fi
    WT_TMP="$(mktemp -d)"
    printf '%s' "$WT_VAL" >"$WT_TMP/v"
    WT_SHA=""
    resolve_sha_cmd
    if [ -n "$SHA_CMD" ]; then WT_SHA="$(sha256_file "$WT_TMP/v")"; else WT_SHA="-"; fi
    rm -rf -- "$WT_TMP"
    printf '%s\t%s\t%s\t%s\n' "core.worktree" "ローカル設定: core.worktree" "$WT_SHA" "$(disp_val "$WT_VAL")" >&2
    echo "WARNING: 作業ツリーが差し替えられている疑いがある(--cwd が toplevel の配下にない)" >&2
    trap - ERR
    exit 22 ;;
esac

CALLER_PWD="$PWD"
abs_from_caller() { case "$1" in /*) printf '%s' "$1" ;; *) printf '%s/%s' "$CALLER_PWD" "$1" ;; esac; }
if [ -n "$OUT" ]; then OUT="$(abs_from_caller "$OUT")"; fi
if [ -n "$PATCH_OUT" ]; then PATCH_OUT="$(abs_from_caller "$PATCH_OUT")"; fi
if [ -n "$PRE_LIST" ]; then PRE_LIST="$(abs_from_caller "$PRE_LIST")"; fi

CDPATH= cd -P -- "$TOP"

# ── 一時ディレクトリ(リポジトリ外であること)──
TMPD="$(mktemp -d)"
TMPD_PHYS="$(abs_existing_dir "$TMPD")"
case "${TMPD_PHYS:-}" in
  "$TOP"|"$TOP"/*) fail_usage "一時ディレクトリがリポジトリ内にある" ;;
esac

resolve_sha_cmd
[ -n "$SHA_CMD" ] || fail_usage "sha256 を計算できない"

# ── git の版(GIT_NO_LAZY_FETCH は 2.45 で入った)──
git_ge_245() {
  local v major minor rest
  v="$(git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false version 2>/dev/null || printf '')"
  v="${v#git version }"
  case "$v" in ''|[!0-9]*) return 1 ;; esac
  major="${v%%.*}"
  rest="${v#*.}"
  minor="${rest%%.*}"
  case "$minor" in ''|*[!0-9]*) return 1 ;; esac
  if [ "$major" -gt 2 ]; then return 0; fi
  if [ "$major" -eq 2 ] && [ "$minor" -ge 45 ]; then return 0; fi
  return 1
}

# ── 出力先の検査(存在に依存しない正規化 → symlink → 種別。$PUB はまだ作らない)──
CHECKED_PATH=""
check_out_path() { # $1=絶対パス $2=out|patch → 正規化した絶対パスを CHECKED_PATH へ
  local p="$1" mode="$2" parent base rem seg acc pfx hit=0 i
  parent="$(dirname -- "$p")"
  base="$(basename -- "$p")"
  # 親ディレクトリの先頭部分列を順に正規化し、$TOP に一致する箇所より後ろだけを symlink 検査する
  PFX=()
  acc=""
  rem="${parent#/}"
  while [ -n "$rem" ]; do
    seg="${rem%%/*}"
    acc="$acc/$seg"
    PFX[${#PFX[@]}]="$acc"
    if [ "$seg" = "$rem" ]; then rem=""; else rem="${rem#*/}"; fi
  done
  for (( i = 0; i < ${#PFX[@]}; i++ )); do
    pfx="$(abs_maybe "${PFX[$i]}")"
    if [ -n "$pfx" ] && [ "$pfx" = "$TOP" ]; then hit=$((i + 1)); break; fi
  done
  if [ "$hit" -gt 0 ]; then
    for (( i = hit; i < ${#PFX[@]}; i++ )); do
      if [ -L "${PFX[$i]}" ]; then fail_usage "出力先の経路に symlink がある: ${PFX[$i]}"; fi
    done
  fi
  if [ -L "$p" ]; then fail_usage "出力先が symlink になっている: $p"; fi
  if [ "$mode" = patch ]; then
    if [ -e "$p" ]; then fail_usage "--patch-out の指定先が既に存在する: $p"; fi
  else
    if [ -e "$p" ] && [ ! -f "$p" ]; then fail_usage "--out が通常ファイルでない: $p"; fi
  fi
  mkdir -p -- "$parent"
  local pp; pp="$(abs_maybe "$parent")"
  [ -n "$pp" ] || fail_usage "出力先の親ディレクトリを解決できない: $parent"
  CHECKED_PATH="$pp/$base"
}

if [ "$PRECHECK" -eq 0 ]; then
  if [ -n "$PATCH_OUT" ]; then check_out_path "$PATCH_OUT" patch; PATCH_ABS="$CHECKED_PATH"; fi
  check_out_path "$OUT" out; OUT_ABS="$CHECKED_PATH"
  if [ -n "$PATCH_OUT" ] && [ "$PATCH_ABS" = "$OUT_ABS" ]; then
    fail_usage "--patch-out と --out が同じパスを指している"
  fi
fi

# ── 基準時点の未追跡一覧 ──
declare -A PRE_SET=()
declare -A INC_SET=()
for p in ${INCLUDES[@]+"${INCLUDES[@]}"}; do
  [ -n "$p" ] || fail_usage "--include-untracked に空の値は指定できない"
  INC_SET["$p"]=1
done
if [ -n "$PRE_LIST" ]; then
  if [ ! -e "$PRE_LIST" ] && [ ! -L "$PRE_LIST" ]; then fail_usage "--pre-untracked が存在しない: $PRE_LIST"; fi
  if [ -L "$PRE_LIST" ] || [ ! -f "$PRE_LIST" ] || [ ! -r "$PRE_LIST" ]; then
    fail_usage "--pre-untracked は symlink でない読み取り可能な通常ファイルに限る: $PRE_LIST"
  fi
  GOT_SHA="$(sha256_file "$PRE_LIST")"
  if [ "$GOT_SHA" != "$PRE_SHA" ]; then fail_usage "未追跡一覧のダイジェストが一致しない"; fi
  while IFS= read -r -d '' p; do
    [ -n "$p" ] || fail_usage "--pre-untracked の一覧に空のレコードがある"
    PRE_SET["$p"]=1
  done <"$PRE_LIST"
fi

# ── LFS の診断(列挙・stat の失敗を原因ごと出す)──
lfs_diag_or_fail() { # $1=rc $2=stderr を捨てたファイル
  if [ "$1" -eq 0 ]; then return 0; fi
  if grep -q -e 'git-lfs' -e 'filter-process' "$2" 2>/dev/null; then
    echo "ERROR [internal] git-lfs を起動できない(filter.lfs.required=true の標準構成では git-lfs が PATH に要る)" >&2
    trap - ERR
    exit 20
  fi
  cat "$2" >&2 || true
  fail_internal "git の呼び出しに失敗した(終了コード $1)"
}

# ── 改竄耐性 ①: 起動時のローカル設定検査 ──
LFS_STD_CLEAN='git-lfs clean -- %f'
LFS_STD_SMUDGE='git-lfs smudge -- %f'
LFS_STD_PROCESS='git-lfs filter-process'
DIS1=(); DIS2=(); DIS3=(); PROMISOR=(); PROMISOR_SHA=(); PROMISOR_VAL=()
rc=0
git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false config --show-scope --includes --list -z >"$TMPD/cfg.z" 2>"$TMPD/cfg.err" || rc=$?
if [ "$rc" -ge 2 ]; then cat "$TMPD/cfg.err" >&2 || true; fail_internal "ローカル設定を読めない(終了コード $rc)"; fi
while IFS= read -r -d '' cf_scope && IFS= read -r -d '' cf_kv; do
  case "$cf_scope" in
    local|worktree) : ;;
    *) continue ;;
  esac
  case "$cf_kv" in
    *$'\n'*) cf_key="${cf_kv%%$'\n'*}"; cf_val="${cf_kv#*$'\n'}" ;;
    *) cf_key="$cf_kv"; cf_val="" ;;
  esac
  cf_low="$(printf '%s' "$cf_key" | LC_ALL=C tr '[:upper:]' '[:lower:]')"
  printf '%s' "$cf_val" >"$TMPD/cfgval"
  cf_sha="$(sha256_file "$TMPD/cfgval")"
  case "$cf_low" in
    include.path) add_tamper "$cf_key" "include: $cf_key" "$cf_sha" "$(disp_val "$cf_val")" 1; continue ;;
    includeif.*) add_tamper "$cf_key" "include: $cf_key" "$cf_sha" "$(disp_val "$cf_val")" 1; continue ;;
    core.fsmonitor|core.hookspath) DIS1[${#DIS1[@]}]="$cf_low"; continue ;;
    core.filemode) if [ "$cf_val" = false ]; then DIS2[${#DIS2[@]}]="$cf_low"; fi; continue ;;
    core.symlinks) if [ "$cf_val" = false ]; then DIS2[${#DIS2[@]}]="$cf_low"; fi; continue ;;
    core.ignorecase) if [ "$cf_val" = true ]; then DIS2[${#DIS2[@]}]="$cf_low"; fi; continue ;;
    core.ignorestat) if [ "$cf_val" = true ]; then DIS2[${#DIS2[@]}]="$cf_low"; fi; continue ;;
    core.splitindex) continue ;;
    extensions.partialclone) PROMISOR[${#PROMISOR[@]}]="$cf_key"; PROMISOR_SHA[${#PROMISOR_SHA[@]}]="$cf_sha"; PROMISOR_VAL[${#PROMISOR_VAL[@]}]="$(disp_val "$cf_val")"; continue ;;
    core.pager|pager.*) DIS3[${#DIS3[@]}]="$cf_low"; continue ;;
    remote.*.promisor) PROMISOR[${#PROMISOR[@]}]="$cf_key"; PROMISOR_SHA[${#PROMISOR_SHA[@]}]="$cf_sha"; PROMISOR_VAL[${#PROMISOR_VAL[@]}]="$(disp_val "$cf_val")"; continue ;;
    filter.lfs.clean) if [ "$cf_val" = "$LFS_STD_CLEAN" ]; then continue; fi ;;
    filter.lfs.smudge) if [ "$cf_val" = "$LFS_STD_SMUDGE" ]; then continue; fi ;;
    filter.lfs.process) if [ "$cf_val" = "$LFS_STD_PROCESS" ]; then continue; fi ;;
    filter.lfs.required) if [ "$cf_val" = true ]; then continue; fi ;;
  esac
  case "$cf_low" in
    core.excludesfile|core.attributesfile|core.bigfilethreshold|diff.external|filter.*|diff.*.textconv|diff.*.command)
      add_tamper "$cf_key" "ローカル設定: $cf_key" "$cf_sha" "$(disp_val "$cf_val")" 1 ;;
  esac
done <"$TMPD/cfg.z"
if [ ${#DIS1[@]} -gt 0 ]; then add_note "無効化して実行: ${DIS1[*]}"; fi
if [ ${#DIS2[@]} -gt 0 ]; then add_note "無効化して実行: ${DIS2[*]}"; fi
if [ ${#DIS3[@]} -gt 0 ]; then add_note "無効化して実行: ${DIS3[*]}"; fi
# promisor 構成で git が 2.45 未満なら遅延取得(= `remote.<名>.uploadpack` の起動)を
# 止められない。**この場で** exit 22 にする(①′ の core.worktree と同じ形)。
# 検出のあとに回す ④ の check-attr も見出しの git も欠落オブジェクトを読みに行くので、
# 続行すると同じ穴が開く。`--out` は作らず、承認の対象外なので承認ダイジェストも出さない
if [ ${#PROMISOR[@]} -gt 0 ]; then
  # キー名は利用者が決められるので、表示に回す列にはすべて制御文字の置換を掛ける
  # (内容ダイジェストは置換前の値から計算済み、4 列目は表示用に切り詰め済み)
  pnames=""
  for (( pi = 0; pi < ${#PROMISOR[@]}; pi++ )); do pnames="$pnames $(sanitize "${PROMISOR[$pi]}")"; done
  if git_ge_245; then
    add_note "promisor 構成: 遅延取得を無効化して実行(${pnames# })"
  else
    for (( pi = 0; pi < ${#PROMISOR[@]}; pi++ )); do
      printf '%s\t%s\t%s\t%s\n' "$(sanitize "${PROMISOR[$pi]}")" \
        "$(sanitize "ローカル設定: ${PROMISOR[$pi]}")" \
        "${PROMISOR_SHA[$pi]}" "${PROMISOR_VAL[$pi]}" >&2
    done
    echo "WARNING: promisor 構成で遅延取得を止められない(git 2.45 以上が要る)" >&2
    trap - ERR
    exit 22
  fi
fi

# ── 置換参照(refs/replace/*)──
rc=0
git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false for-each-ref refs/replace/ >"$TMPD/replace.txt" 2>/dev/null || rc=$?
if [ "$rc" -eq 0 ] && [ -s "$TMPD/replace.txt" ]; then add_note "置換参照を無効化して実行"; fi

# ── 基準の検証 ──
if [ "$PRECHECK" -eq 0 ]; then
  rc=0
  git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false rev-parse --verify --quiet "$BASE^{tree}" >/dev/null 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then fail_nobase "--base を解決できない: $BASE"; fi
  if [ -n "$PATCH_OUT" ]; then
    rc=0
    git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false rev-parse --verify --quiet "$PATCH_BASE^{tree}" >/dev/null 2>&1 || rc=$?
    if [ "$rc" -ne 0 ]; then fail_nobase "--patch-base を解決できない: $PATCH_BASE"; fi
  fi
fi


# ── 改竄耐性 ①: .git/info/attributes(種別を見てから読む)──
INFO_ATTR="$(git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false rev-parse --path-format=absolute --git-path info/attributes 2>/dev/null || printf '')"
if [ -n "$INFO_ATTR" ] && { [ -e "$INFO_ATTR" ] || [ -L "$INFO_ATTR" ]; }; then
  if [ ! -f "$INFO_ATTR" ] || [ -L "$INFO_ATTR" ]; then
    add_tamper ".git/info/attributes" ".git/info/attributes が通常ファイルでない" "-" "" 0
    STOP_NOW=1
  else
    ia_sha="$(sha256_file "$INFO_ATTR")"
    ia_hit=0
    while IFS= read -r ia_line || [ -n "$ia_line" ]; do
      case "$ia_line" in
        '#'*|'') continue ;;
      esac
      # 1 語目(パターン)は照合しない。引用符で囲んだ空白入りパターンは閉じ引用符まで
      case "$ia_line" in
        '"'*)
          ia_rest="${ia_line#\"}"
          ia_acc=""
          while [ -n "$ia_rest" ]; do
            case "$ia_rest" in
              '\\'*) ia_rest="${ia_rest#??}" ;;
              '\"'*) ia_rest="${ia_rest#??}" ;;
              '"'*) ia_rest="${ia_rest#\"}"; break ;;
              *) ia_rest="${ia_rest#?}" ;;
            esac
          done
          ia_attrs="$ia_rest" ;;
        *)
          case "$ia_line" in
            *[[:space:]]*) ia_attrs="${ia_line#*[[:space:]]}" ;;
            *) ia_attrs="" ;;
          esac ;;
      esac
      ia_arr=()
      read -r -a ia_arr <<<"$ia_attrs" || true
      for ia_tok in ${ia_arr[@]+"${ia_arr[@]}"}; do
        case "$ia_tok" in
          filter=*|diff=*|-diff|binary|working-tree-encoding=*) ia_hit=1 ;;
        esac
      done
    done <"$INFO_ATTR"
    if [ "$ia_hit" -eq 1 ]; then
      add_tamper ".git/info/attributes" ".git/info/attributes" "$ia_sha" "" 1
    fi
  fi
fi

# 属性ファイルが通常ファイルでないときは、これ以降の列挙・属性検査・diff が同じファイルを読んで
# 待つため、ここで打ち切る(承認の対象外)。
if [ "$STOP_NOW" -eq 0 ]; then

# ── 列挙(ユーザーの index に対して。④ と ⑦ はここまでを見る)──
rc=0
git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false ls-files -z >"$TMPD/tracked.z" 2>"$TMPD/tracked.err" || rc=$?
lfs_diag_or_fail "$rc" "$TMPD/tracked.err"
rc=0
git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false ls-files -o --exclude-standard -z >"$TMPD/untracked.z" 2>"$TMPD/untracked.err" || rc=$?
lfs_diag_or_fail "$rc" "$TMPD/untracked.err"
rc=0
git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false ls-files -s -z >"$TMPD/stage.z" 2>"$TMPD/stage.err" || rc=$?
lfs_diag_or_fail "$rc" "$TMPD/stage.err"

# ── 改竄耐性 ④: 属性検査(index 全体 + 未追跡の全パス)──
cat "$TMPD/tracked.z" "$TMPD/untracked.z" >"$TMPD/attrin.z"
declare -A ATTR_FILTER=()
rc=0
git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false check-attr --stdin -z filter working-tree-encoding ident <"$TMPD/attrin.z" >"$TMPD/attr.z" 2>"$TMPD/attr.err" || rc=$?
lfs_diag_or_fail "$rc" "$TMPD/attr.err"
PENDING_UNSET=()
PENDING_UNSET_PATH=()
while IFS= read -r -d '' at_path && IFS= read -r -d '' at_name && IFS= read -r -d '' at_val; do
  case "$at_name" in
    filter)
      ATTR_FILTER["$at_path"]="$at_val"
      case "$at_val" in
        unspecified|unset)
          PENDING_UNSET[${#PENDING_UNSET[@]}]="$at_val"
          PENDING_UNSET_PATH[${#PENDING_UNSET_PATH[@]}]="$at_path" ;;
        lfs) : ;;
        *) add_tamper "$at_path" "filter 属性: $at_val" "-" "" 1 ;;
      esac ;;
    working-tree-encoding)
      case "$at_val" in
        unspecified|unset) : ;;
        *) add_tamper "$at_path" "working-tree-encoding 属性: $at_val" "-" "" 0 ;;
      esac ;;
    ident)
      case "$at_val" in
        unspecified|unset) : ;;
        *) add_tamper "$at_path" "ident 属性: $at_val" "-" "" 0 ;;
      esac ;;
  esac
done <"$TMPD/attr.z"
if [ ${#PENDING_UNSET[@]} -gt 0 ]; then
  rc=0
  git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false config --get-regexp '^filter\.(unset|unspecified)\.' >"$TMPD/unsetf.txt" 2>/dev/null || rc=$?
  if [ "$rc" -eq 0 ]; then
    declare -A DEFINED_UNSET=()
    while IFS= read -r uf_line; do
      uf_key="${uf_line%% *}"
      case "$uf_key" in
        filter.unset.*) DEFINED_UNSET[unset]=1 ;;
        filter.unspecified.*) DEFINED_UNSET[unspecified]=1 ;;
      esac
    done <"$TMPD/unsetf.txt"
    for (( i = 0; i < ${#PENDING_UNSET[@]}; i++ )); do
      if [ -n "${DEFINED_UNSET[${PENDING_UNSET[$i]}]:-}" ]; then
        add_tamper "${PENDING_UNSET_PATH[$i]}" "filter 属性: ${PENDING_UNSET[$i]}" "-" "" 1
      fi
    done
  fi
fi

# ── 改竄耐性 ⑦: index フラグ検査 ──
SPARSE_OK=()
rc=0
git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false ls-files -v -z >"$TMPD/flags.z" 2>"$TMPD/flags.err" || rc=$?
lfs_diag_or_fail "$rc" "$TMPD/flags.err"
SP_ENABLED=0
rc=0
SP="$(git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false config --get core.sparsecheckout 2>/dev/null)" || rc=$?
if [ "$rc" -eq 0 ] && [ "$SP" = true ]; then
  SP_FILE="$(git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false rev-parse --path-format=absolute --git-path info/sparse-checkout 2>/dev/null || printf '')"
  if [ -n "$SP_FILE" ] && [ -e "$SP_FILE" ]; then SP_ENABLED=1; fi
fi
FLAG_PATH=(); FLAG_REASON=(); CAND_PATH=()
while IFS= read -r -d '' fl_rec; do
  fl_ch="${fl_rec:0:1}"
  fl_path="${fl_rec:2}"
  case "$fl_ch" in
    s) fl_reason="index フラグ: assume-unchanged,skip-worktree" ;;
    S) fl_reason="index フラグ: skip-worktree" ;;
    [a-z]) fl_reason="index フラグ: assume-unchanged" ;;
    *) continue ;;
  esac
  if [ "$SP_ENABLED" -eq 1 ] && [ "$fl_ch" = S ] && [ ! -e "$fl_path" ] && [ ! -L "$fl_path" ]; then
    CAND_PATH[${#CAND_PATH[@]}]="$fl_path"
  fi
  FLAG_PATH[${#FLAG_PATH[@]}]="$fl_path"
  FLAG_REASON[${#FLAG_REASON[@]}]="$fl_reason"
done <"$TMPD/flags.z"
declare -A SPARSE_EXCEPT=()
if [ ${#CAND_PATH[@]} -gt 0 ]; then
  : >"$TMPD/cand.z"
  for p in "${CAND_PATH[@]}"; do printf '%s\0' "$p" >>"$TMPD/cand.z"; done
  rc=0
  git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false sparse-checkout check-rules -z <"$TMPD/cand.z" >"$TMPD/rules.z" 2>/dev/null || rc=$?
  if [ "$rc" -eq 0 ]; then
    declare -A IN_RULES=()
    while IFS= read -r -d '' p; do IN_RULES["$p"]=1; done <"$TMPD/rules.z"
    for p in "${CAND_PATH[@]}"; do
      if [ -z "${IN_RULES[$p]:-}" ]; then SPARSE_EXCEPT["$p"]=1; fi
    done
  fi
fi
for (( i = 0; i < ${#FLAG_PATH[@]}; i++ )); do
  if [ -n "${SPARSE_EXCEPT[${FLAG_PATH[$i]}]:-}" ]; then
    SPARSE_OK[${#SPARSE_OK[@]}]="${FLAG_PATH[$i]}"
  else
    add_tamper "${FLAG_PATH[$i]}" "${FLAG_REASON[$i]}" "-" "" 1
  fi
done
if [ ${#SPARSE_OK[@]} -gt 0 ]; then
  sp_list=""
  for (( i = 0; i < ${#SPARSE_OK[@]} && i < 20; i++ )); do sp_list="$sp_list $(path_field "${SPARSE_OK[$i]}")"; done
  add_note "sparse-checkout で作業ツリーに無い skip-worktree: ${#SPARSE_OK[@]} 件(${sp_list# })"
fi

fi

# ── 承認ダイジェストと --accept ──
: >"$TMPD/dg"
N_OK=0
N_BAD=0
for (( i = 0; i < ${#TC1[@]}; i++ )); do
  if [ "${TOK[$i]}" = 1 ]; then
    printf '%s\t%s\t%s\0' "${TC1[$i]}" "${TC2[$i]}" "${TC3[$i]}" >>"$TMPD/dg"
    N_OK=$((N_OK + 1))
  else
    N_BAD=$((N_BAD + 1))
  fi
done
LC_ALL=C sort -z <"$TMPD/dg" >"$TMPD/dgs"
DIGEST="$(sha256_file "$TMPD/dgs")"
SHOW_DIGEST=0
if [ "$TAMPER" -eq 1 ] && [ "$N_BAD" -eq 0 ] && [ "$N_OK" -gt 0 ]; then SHOW_DIGEST=1; fi

ACCEPT_MISMATCH=0
APPROVED_NAMES=()
if [ "$TAMPER" -eq 1 ] && [ "$ACCEPT_GIVEN" -eq 1 ]; then
  if [ "$N_BAD" -gt 0 ]; then
    : # 承認できない疑いがあるときは --accept を無視する
  elif [ "$ACCEPT" = "$DIGEST" ] && [ "$N_OK" -gt 0 ]; then
    add_note "承認済みとして扱った項目: $N_OK 件(承認ダイジェスト $DIGEST)"
    for (( i = 0; i < ${#TC1[@]}; i++ )); do
      add_note "承認済み: $(sanitize "${TC1[$i]}")	$(sanitize "${TC2[$i]}")"
      case "${TC2[$i]}" in
        'ローカル設定: filter.'*)
          ap_key="${TC2[$i]#ローカル設定: }"
          ap_key="${ap_key#filter.}"
          ap_name="${ap_key%.*}"
          APPROVED_NAMES[${#APPROVED_NAMES[@]}]="$ap_name" ;;
        'filter 属性: '*)
          APPROVED_NAMES[${#APPROVED_NAMES[@]}]="${TC2[$i]#filter 属性: }" ;;
      esac
    done
    TC1=(); TC2=(); TC3=(); TC4=(); TOK=()
    TAMPER=0
    SHOW_DIGEST=0
  else
    ACCEPT_MISMATCH=1
  fi
fi

# 承認した filter は実行せず、作業ツリーの実内容で比較する(環境変数は設定ファイルより優先される)
AF_N=0
AF_EXPORTS=()
AF_UNIQ=()
for n in ${APPROVED_NAMES[@]+"${APPROVED_NAMES[@]}"}; do
  af_dup=0
  for m in ${AF_UNIQ[@]+"${AF_UNIQ[@]}"}; do if [ "$m" = "$n" ]; then af_dup=1; break; fi; done
  if [ "$af_dup" -eq 1 ]; then continue; fi
  AF_UNIQ[${#AF_UNIQ[@]}]="$n"
  for v in clean smudge process required; do
    AF_N=$((AF_N + 1))
    AF_EXPORTS[${#AF_EXPORTS[@]}]="GIT_CONFIG_KEY_$((AF_N - 1))=filter.$n.$v"
    if [ "$v" = required ]; then
      AF_EXPORTS[${#AF_EXPORTS[@]}]="GIT_CONFIG_VALUE_$((AF_N - 1))=false"
    else
      AF_EXPORTS[${#AF_EXPORTS[@]}]="GIT_CONFIG_VALUE_$((AF_N - 1))="
    fi
  done
done
if [ "$AF_N" -gt 0 ]; then export GIT_CONFIG_COUNT="$AF_N" ${AF_EXPORTS[@]+"${AF_EXPORTS[@]}"}; fi
if [ "$AF_N" -gt 0 ]; then for n in ${AF_UNIQ[@]+"${AF_UNIQ[@]}"}; do for v in clean smudge process; do rc=0; af_v="$(git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false config --get "filter.$n.$v" 2>/dev/null)" || rc=$?; if [ "$rc" -ne 0 ] || [ -n "$af_v" ]; then echo "ERROR [tamper] 承認した filter を無効化できない(filter.$n.$v)" >&2; trap - ERR; exit 22; fi; done; done; fi
if [ ${#AF_UNIQ[@]} -gt 0 ]; then
  for n in "${AF_UNIQ[@]}"; do
    add_note "承認済みの filter を実行せず実内容で比較した: $(sanitize "$n")(この filter の対象パスは内容が同じでも変更ありに出ることがあり、clean フィルタで伏せている内容は snapshot に出る)"
  done
fi

emit_tamper_rows() { # $1=出力先(ファイル。空なら stderr)
  local i line
  for (( i = 0; i < ${#TC1[@]}; i++ )); do
    line="$(sanitize "${TC1[$i]}")	$(sanitize "${TC2[$i]}")	$(sanitize "${TC3[$i]}")"
    if [ -n "${TC4[$i]}" ]; then line="$line	$(sanitize "${TC4[$i]}")"; fi
    line="${line}"
    if [ -n "$1" ]; then printf '%s\n' "$line" >>"$1"; else printf '%s\n' "$line" >&2; fi
  done
  if [ "$SHOW_DIGEST" -eq 1 ]; then
    if [ -n "$1" ]; then printf '承認ダイジェスト: %s\n' "$DIGEST" >>"$1"; else printf '承認ダイジェスト: %s\n' "$DIGEST" >&2; fi
  fi
}

emit_notes_stderr() { local n; for n in ${NOTES[@]+"${NOTES[@]}"}; do printf 'NOTE: %s\n' "$n" >&2; done; }
emit_accept_tokens() { # 承認した filter の無効化を呼び出し側が再現するための機械可読出力
  local t
  if [ "$AF_N" -eq 0 ]; then return 0; fi
  printf '%s\0' "GIT_CONFIG_COUNT=$AF_N"
  for t in ${AF_EXPORTS[@]+"${AF_EXPORTS[@]}"}; do printf '%s\0' "$t"; done
}

if [ "$PRECHECK" -eq 1 ]; then
  emit_notes_stderr
  if [ "$TAMPER" -eq 1 ]; then
    if [ "$ACCEPT_MISMATCH" -eq 1 ]; then
      echo "承認ダイジェストが一致しない(疑いの内容が承認時から変わった)" >&2
    fi
    emit_tamper_rows ""
    trap - ERR
    exit 22
  fi
  emit_accept_tokens
  trap - ERR
  exit 0
fi

# ── 節ファイルの用意 ──
SEC_STAT="$TMPD/s_stat"; SEC_BODY="$TMPD/s_body"; SEC_UNTRACKED="$TMPD/s_untracked"
SEC_SECRET="$TMPD/s_secret"; SEC_DEFAULT="$TMPD/s_default"; SEC_OUTSIDE="$TMPD/s_outside"
SEC_NOTINC="$TMPD/s_notinc"; SEC_OMIT_U="$TMPD/s_omit_u"; SEC_OMIT_T="$TMPD/s_omit_t"
SEC_TAMPER="$TMPD/s_tamper"; SEC_GITIGNORE="$TMPD/s_gitignore"
for f in "$SEC_STAT" "$SEC_BODY" "$SEC_UNTRACKED" "$SEC_SECRET" "$SEC_DEFAULT" "$SEC_OUTSIDE" \
         "$SEC_NOTINC" "$SEC_OMIT_U" "$SEC_OMIT_T" "$SEC_TAMPER" "$SEC_GITIGNORE"; do : >"$f"; done

CNT_A=0; CNT_B1=0; CNT_B2=0; CNT_C=0; CNT_D=0; CNT_E=0; CNT_F=0
SUMMARY=""
WANT_PATCH=0

rc=0
HEAD_NOW="$(git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false rev-parse --verify --quiet HEAD 2>/dev/null)" || rc=$?
if [ "$rc" -ne 0 ] || [ -z "$HEAD_NOW" ]; then HEAD_DESC="(HEAD 無し)"; else HEAD_DESC="$HEAD_NOW"; fi
rc=0
git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false merge-base --is-ancestor "$BASE" HEAD >/dev/null 2>&1 || rc=$?
case "$rc" in
  0) ANC_DESC="基準は HEAD の祖先" ;;
  1) ANC_DESC="WARNING: 基準は HEAD の祖先ではない" ;;
  *) ANC_DESC="祖先判定は不能(基準が tree / HEAD 無し)" ;;
esac
if [ -n "$PRE_LIST" ]; then PRE_DESC="あり"; else PRE_DESC="なし"; add_note "\`--pre-untracked\` の指定が無く全未追跡を対象にした"; fi

write_section() { # $1=見出し $2=節ファイル $3=空のときの文字列
  printf '\n## %s\n\n' "$1"
  if [ -s "$2" ]; then cat "$2"; else printf '%s\n' "$3"; fi
}

assemble_out() { # $1=固定文字列(空なら通常出力)
  local fixed="$1"
  {
    printf '# diff スナップショット\n\n'
    printf -- '- 基準: %s\n' "$BASE"
    printf -- '- 現在 HEAD: %s\n' "$HEAD_DESC"
    printf -- '- 生成日時: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
    printf -- '- 除外 ERE: %s\n' "$ERE"
    [ -n "$EXCEPT_ERE" ] && printf -- '- 除外の例外 ERE: %s\n' "$EXCEPT_ERE"
    printf -- '- 祖先判定: %s\n' "$ANC_DESC"
    printf -- '- 基準時点の未追跡一覧: %s\n' "$PRE_DESC"
    printf -- '- 件数の数え方: 要約行の件数は未追跡だけを数える(追跡の除外はパスだけ各節に載る)\n'
    if [ "$TAMPER" -eq 1 ]; then printf -- '- WARNING: 改竄の疑いがある(`## 改竄の疑い` を参照)\n'; fi
    local n
    for n in ${NOTES[@]+"${NOTES[@]}"}; do printf -- '- NOTE: %s\n' "$n"; done
    printf '\n%s\n' "$SUMMARY"
    if [ -n "$fixed" ]; then
      printf '\n## %s\n\n%s\n' "追跡差分(stat)" "$fixed"
      printf '\n## %s\n\n%s\n' "追跡差分" "$fixed"
      printf '\n## %s\n\n%s\n' "未追跡ファイル" "$fixed"
      printf '\n## %s\n\n%s\n' "除外(機密)" "$fixed"
      printf '\n## %s\n\n%s\n' "除外(既定)" "$fixed"
      printf '\n## %s\n\n%s\n' "基準時点から存在(対象外)" "$fixed"
      printf '\n## %s\n\n%s\n' "含められなかった未追跡" "$fixed"
      printf '\n## %s\n\n%s\n' "内容を省略した未追跡" "$fixed"
      printf '\n## %s\n\n%s\n' "内容を省略した追跡" "$fixed"
      write_section "改竄の疑い" "$SEC_TAMPER" "(差分なし)"
      printf '\n## %s\n\n%s\n' "gitignore により除外(.gitignore 以外)" "$fixed"
    else
      write_section "追跡差分(stat)" "$SEC_STAT" "(差分なし)"
      write_section "追跡差分" "$SEC_BODY" "$BODY_EMPTY"
      write_section "未追跡ファイル" "$SEC_UNTRACKED" "(差分なし)"
      write_section "除外(機密)" "$SEC_SECRET" "(差分なし)"
      write_section "除外(既定)" "$SEC_DEFAULT" "(差分なし)"
      write_section "基準時点から存在(対象外)" "$SEC_OUTSIDE" "(差分なし)"
      write_section "含められなかった未追跡" "$SEC_NOTINC" "(差分なし)"
      write_section "内容を省略した未追跡" "$SEC_OMIT_U" "(差分なし)"
      write_section "内容を省略した追跡" "$SEC_OMIT_T" "(差分なし)"
      write_section "改竄の疑い" "$SEC_TAMPER" "(差分なし)"
      write_section "gitignore により除外(.gitignore 以外)" "$SEC_GITIGNORE" "(差分なし)"
    fi
  } >"$TMPD/out.raw"
}

publish_outputs() {
  local outdir patchdir
  outdir="$(dirname -- "$OUT_ABS")"
  PUB="$(mktemp -d "$outdir/.diff-snapshot.XXXXXX")"
  cp -- "$TMPD/out.raw" "$PUB/out.tmp"
  if [ -n "$PATCH_OUT" ] && [ "$WANT_PATCH" -eq 1 ]; then
    patchdir="$(dirname -- "$PATCH_ABS")"
    PUB2="$(mktemp -d "$patchdir/.diff-snapshot.XXXXXX")"
    cp -- "$TMPD/patch.raw" "$PUB2/patch.tmp"
    mv -- "$PUB2/patch.tmp" "$PATCH_ABS" || fail_internal "パッチの公開に失敗した(何も公開していない)"
  fi
  if ! mv -- "$PUB/out.tmp" "$OUT_ABS"; then
    if [ -n "$PATCH_OUT" ] && [ "$WANT_PATCH" -eq 1 ]; then rm -f -- "$PATCH_ABS"; fi
    fail_internal "出力の公開に失敗した"
  fi
}

BODY_EMPTY="(差分なし)"

if [ "$TAMPER" -eq 1 ]; then
  if [ "$ACCEPT_MISMATCH" -eq 1 ]; then
    echo "承認ダイジェストが一致しない(疑いの内容が承認時から変わった)" >&2
  fi
  emit_tamper_rows "$SEC_TAMPER"
  emit_tamper_rows ""
  SUMMARY="要約: 改竄の疑いがあるため未集計"
  assemble_out "(改竄の疑いがあるため生成しない)"
  publish_outputs
  trap - ERR
  exit 22
fi
emit_accept_tokens

# ── 改竄耐性 ⑧: stat 情報を持たない使い捨ての index を作り、以後の差分をそれに向ける ──
TMPIDX="$TMPD/index.tmp"
git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false ls-files -s -z | GIT_INDEX_FILE="$TMPIDX" git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false -c core.ignoreStat=false update-index -z --index-info
if [ ${#SPARSE_OK[@]} -gt 0 ]; then
  : >"$TMPD/sparse.z"
  for p in "${SPARSE_OK[@]}"; do printf '%s\0' "$p" >>"$TMPD/sparse.z"; done
  GIT_INDEX_FILE="$TMPIDX" git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false -c core.ignoreStat=false update-index --skip-worktree -z --stdin <"$TMPD/sparse.z"
fi
# 使い捨ての index に assume-unchanged(小文字のタグ)が付いていないことを確かめる。
# 付いていると git が作業ツリーを読まず、追跡差分が丸ごと消える
# (skip-worktree の `S` は sparse-checkout の正当な再適用なので対象外)
GIT_INDEX_FILE="$TMPIDX" git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false ls-files -z -v >"$TMPD/idxflags.z"
while IFS= read -r -d '' idx_rec; do
  case "${idx_rec:0:1}" in
    [a-z]) fail_internal "使い捨ての index に assume-unchanged が残っている(core.ignoreStat の可能性)" ;;
  esac
done <"$TMPD/idxflags.z"
export GIT_INDEX_FILE="$TMPIDX"

# ── 除外の判定(追跡・未追跡に共通。最初に当たった分類で確定する)──
is_output_path() {
  local abs="$TOP/$1"
  if [ -n "$OUT_ABS" ] && [ "$abs" = "$OUT_ABS" ]; then return 0; fi
  if [ -n "$PATCH_ABS" ] && [ "$abs" = "$PATCH_ABS" ]; then return 0; fi
  if [ -n "${PUB:-}" ]; then case "$abs" in "$PUB"/*) return 0 ;; esac; fi
  if [ -n "${PUB2:-}" ]; then case "$abs" in "$PUB2"/*) return 0 ;; esac; fi
  return 1
}
CLASS=""
classify() { # $1=パス $2=tracked|untracked
  CLASS=include
  if ere_hit "$1" "$DEFAULT_EXCLUDE"; then CLASS=default; DEF_REASON="既定除外"; return 0; fi
  if is_output_path "$1"; then CLASS=default; DEF_REASON="出力先自身"; return 0; fi
  # 除外に当たっても、例外に当たるものは機密扱いにしない(`.env.example` のような公開例)
  if [ "$1" != "$PROFILE_PATH" ] && [ -n "$ERE" ] && ere_hit "$1" "$ERE" \
     && { [ -z "$EXCEPT_ERE" ] || ! ere_hit "$1" "$EXCEPT_ERE"; }; then CLASS=secret; return 0; fi
  if [ "$2" = untracked ] && [ -n "${PRE_SET[$1]:-}" ] && [ -z "${INC_SET[$1]:-}" ]; then CLASS=outside; return 0; fi
  return 0
}
DEF_REASON=""

EXC_OUT=()
build_exclude_pathspec() { # INC_ARR(含めるパス)と CAND_ARR(除外候補)から EXC_OUT を作る
  EXC_OUT=()
  local c i
  for c in ${CAND_ARR[@]+"${CAND_ARR[@]}"}; do
    for i in ${INC_ARR[@]+"${INC_ARR[@]}"}; do
      case "$c" in
        "$i"/*) EXC_OUT[${#EXC_OUT[@]}]="$c"; break ;;
      esac
    done
  done
}

# ── 追跡差分の列挙 ──
rc=0
git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false -c core.filemode=true -c core.symlinks=true diff --name-only --no-renames --ignore-submodules=dirty -z "$BASE" >"$TMPD/tdiff.z" 2>"$TMPD/tdiff.err" || rc=$?
lfs_diag_or_fail "$rc" "$TMPD/tdiff.err"
INC_T=(); EXC_T=()
while IFS= read -r -d '' p; do
  classify "$p" tracked
  case "$CLASS" in
    default) sec_line "$SEC_DEFAULT" "$p" "(追跡)" "$DEF_REASON"; EXC_T[${#EXC_T[@]}]="$p" ;;
    secret) sec_line "$SEC_SECRET" "$p" "(追跡)"; EXC_T[${#EXC_T[@]}]="$p" ;;
    *) INC_T[${#INC_T[@]}]="$p" ;;
  esac
done <"$TMPD/tdiff.z"

# ── 改竄耐性 ②: 内容を省略した追跡(両側の存在と git モードを先に見る)──
declare -A OMIT_T=()
OMIT_LIST=()
head_has_nul() { # $1=ファイル
  local a b
  a="$( export LC_ALL=C; head -c 8000 <"$1" | tr -d '\000' | wc -c )"
  b="$( export LC_ALL=C; head -c 8000 <"$1" | wc -c )"
  [ "${a// /}" != "${b// /}" ]
}
for p in ${INC_T[@]+"${INC_T[@]}"}; do
  bm=""
  rc=0
  lt="$(git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false ls-tree -z "$BASE" -- ":(literal)$p" 2>/dev/null | tr -d '\000')" || rc=$?
  if [ "$rc" -eq 0 ] && [ -n "$lt" ]; then bm="${lt%% *}"; fi
  rc=0
  ls="$(git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false ls-files -s -z -- ":(literal)$p" 2>/dev/null | tr -d '\000')" || rc=$?
  im=""
  if [ "$rc" -eq 0 ] && [ -n "$ls" ]; then im="${ls%% *}"; fi
  cur_kind=file
  if [ -L "$p" ]; then cur_kind=symlink
  elif [ ! -e "$p" ]; then cur_kind=absent
  elif [ -d "$p" ] && [ "$im" = 160000 ]; then cur_kind=gitlink
  elif [ ! -f "$p" ]; then cur_kind=other
  fi
  base_judge=0
  case "$bm" in 100644|100755) base_judge=1 ;; esac
  cur_judge=0
  if [ "$cur_kind" = file ]; then cur_judge=1; fi
  is_bin=0; is_big=0
  if [ "$base_judge" -eq 1 ]; then
    set +o pipefail; git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false cat-file blob "$BASE:$p" 2>/dev/null | head -c 8000 >"$TMPD/basehead"; set -o pipefail
    if head_has_nul "$TMPD/basehead"; then is_bin=1; fi
    rc=0
    bsz="$(git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false cat-file -s "$BASE:$p" 2>/dev/null)" || rc=$?
    if [ "$rc" -eq 0 ] && [ -n "$bsz" ] && [ "$bsz" -gt 1048576 ]; then is_big=1; fi
  fi
  if [ "$cur_judge" -eq 1 ] && [ -r "$p" ]; then
    if head_has_nul "$p"; then is_bin=1; fi
    csz="$(wc -c <"$p" | tr -d ' ')"
    if [ "$csz" -gt 1048576 ]; then is_big=1; fi
  fi
  if [ "$base_judge" -eq 1 ] && { [ "$cur_kind" = symlink ] || [ "$cur_kind" = other ]; }; then
    sec_line "$SEC_OMIT_T" "$p" "作業ツリーが通常ファイルでない"
  fi
  omit_reason=""
  if [ "$is_bin" -eq 1 ]; then omit_reason="バイナリ"
  elif [ "$is_big" -eq 1 ]; then omit_reason="1 MiB 超"
  fi
  if [ -n "$omit_reason" ]; then
    OMIT_T["$p"]=1
    OMIT_LIST[${#OMIT_LIST[@]}]="$p"
    sec_line "$SEC_OMIT_T" "$p" "$omit_reason"
  fi
done

# ── 追跡差分(stat)と本文 ──
if [ ${#INC_T[@]} -gt 0 ]; then
  INC_ARR=(); for p in "${INC_T[@]}"; do INC_ARR[${#INC_ARR[@]}]="$p"; done
  CAND_ARR=(); for p in ${EXC_T[@]+"${EXC_T[@]}"}; do CAND_ARR[${#CAND_ARR[@]}]="$p"; done
  build_exclude_pathspec
  PS=()
  for p in "${INC_T[@]}"; do PS[${#PS[@]}]=":(literal)$p"; done
  for p in ${EXC_OUT[@]+"${EXC_OUT[@]}"}; do PS[${#PS[@]}]=":(literal,exclude)$p"; done
  rc=0
  git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false -c core.filemode=true -c core.symlinks=true diff --stat --stat-width=200 --stat-name-width=500 --no-ext-diff --no-textconv --no-color --no-renames --submodule=short --ignore-submodules=dirty --text --src-prefix=a/ --dst-prefix=b/ "$BASE" -- "${PS[@]}" >"$SEC_STAT" 2>"$TMPD/stat.err" || rc=$?
  lfs_diag_or_fail "$rc" "$TMPD/stat.err"

  BINC=()
  for p in "${INC_T[@]}"; do if [ -z "${OMIT_T[$p]:-}" ]; then BINC[${#BINC[@]}]="$p"; fi; done
  if [ ${#BINC[@]} -eq 0 ]; then
    BODY_EMPTY='内容は全て `## 内容を省略した追跡` へ'
  else
    INC_ARR=(); for p in "${BINC[@]}"; do INC_ARR[${#INC_ARR[@]}]="$p"; done
    CAND_ARR=(); for p in ${EXC_T[@]+"${EXC_T[@]}"}; do CAND_ARR[${#CAND_ARR[@]}]="$p"; done
    for p in ${OMIT_LIST[@]+"${OMIT_LIST[@]}"}; do CAND_ARR[${#CAND_ARR[@]}]="$p"; done
    build_exclude_pathspec
    PS=()
    for p in "${BINC[@]}"; do PS[${#PS[@]}]=":(literal)$p"; done
    for p in ${EXC_OUT[@]+"${EXC_OUT[@]}"}; do PS[${#PS[@]}]=":(literal,exclude)$p"; done
    rc=0
    git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false -c core.filemode=true -c core.symlinks=true $LFS_OFF diff --unified=3 --no-ext-diff --no-textconv --no-color --no-renames --submodule=short --ignore-submodules=dirty --text --src-prefix=a/ --dst-prefix=b/ "$BASE" -- "${PS[@]}" >"$SEC_BODY" 2>"$TMPD/body.err" || rc=$?
    lfs_diag_or_fail "$rc" "$TMPD/body.err"
  fi
fi

# ── 追跡ファイルの別表記(同一 inode)を見分けるための小文字化した追跡パス集合 ──
LC_ALL=C tr '[:upper:]' '[:lower:]' <"$TMPD/tracked.z" >"$TMPD/tracked_lower.z"
declare -A TRACKED_LOWER=()
exec 3<"$TMPD/tracked.z"
exec 4<"$TMPD/tracked_lower.z"
while IFS= read -r -d '' tp <&3 && IFS= read -r -d '' tl <&4; do
  if [ -z "${TRACKED_LOWER[$tl]:-}" ]; then TRACKED_LOWER["$tl"]="$tp"; fi
done
exec 3<&-
exec 4<&-

# ── 基準時点から存在(対象外)の記録(種別を見てから内容を読む)──
record_outside() {
  local f="$1" kind sha bytes lnk
  case "$f" in
    */) kind="ネスト repo"; sha="-"; bytes="-" ;;
    *)
      if [ -L "$f" ]; then kind="symlink"; lnk="$(readlink -- "$f" 2>/dev/null; printf x)"; lnk="${lnk%x}"; lnk="${lnk%$'\n'}"; sha="$(sha256_str "$lnk")"; bytes="${#lnk}"
      elif [ ! -e "$f" ]; then kind="消失"; sha="-"; bytes="-"
      elif [ ! -f "$f" ]; then kind="通常ファイルでない"; sha="-"; bytes="-"
      elif [ ! -r "$f" ]; then kind="読めない"; sha="-"; bytes="-"
      else
        bytes="$(wc -c <"$f" | tr -d ' ')"
        if [ "$bytes" -gt 1048576 ]; then kind="通常ファイル(1 MiB 超)"; sha="-"
        else kind="通常ファイル"; sha="$(sha256_file "$f")"
        fi
      fi ;;
  esac
  sec_line "$SEC_OUTSIDE" "$f" "$kind" "$sha" "$bytes"
}

# ── 未追跡ファイルの処理 ──
declare -A NESTED=()
while IFS= read -r -d '' f; do
  case "$f" in
    */) NESTED["${f%/}"]=1 ;;
  esac
done <"$TMPD/untracked.z"
UNTRACKED_TOTAL=0
CAP_HIT=0
ALIAS_N=0
notinc() { sec_line "$SEC_NOTINC" "$1" "$2"; CNT_E=$((CNT_E + 1)); }
omit_u() { sec_line "$SEC_OMIT_U" "$1" "$2"; CNT_D=$((CNT_D + 1)); }
# 一覧は FD 3 から読み、ループ本体の stdin は /dev/null にする
# (`-` という名前のパスを引数に取るコマンドが一覧を飲み込むのを防ぐ)
while IFS= read -r -d '' f <&3; do
  classify "$f" untracked
  case "$CLASS" in
    default) sec_line "$SEC_DEFAULT" "$f" "(未追跡)" "$DEF_REASON"; CNT_B2=$((CNT_B2 + 1)); continue ;;
    secret) sec_line "$SEC_SECRET" "$f" "(未追跡)"; CNT_B1=$((CNT_B1 + 1)); continue ;;
    outside) record_outside "$f"; CNT_C=$((CNT_C + 1)); continue ;;
  esac
  case "$f" in
    */) notinc "$f" "ネスト repo"; continue ;;
  esac
  u_sym=0
  if [ -L "$f" ]; then
    if [ ! -e "$f" ]; then notinc "$f" "リンク切れ"; continue; fi
    if [ ! -f "$f" ]; then notinc "$f" "ディレクトリ等へのリンク"; continue; fi
    u_sym=1
  elif [ ! -e "$f" ]; then
    notinc "$f" "列挙後に消失"; continue
  elif [ ! -f "$f" ]; then
    notinc "$f" "通常ファイルでない"; continue
  elif [ ! -r "$f" ]; then
    notinc "$f" "読めない"; FAILED=$((FAILED + 1)); continue
  fi
  if [ "$u_sym" -eq 1 ]; then
    u_link="$(readlink -- "$f" 2>/dev/null; printf x)"
    u_link="${u_link%x}"
    u_link="${u_link%$'\n'}"
    u_size="${#u_link}"
  else
    u_low="$(printf '%s' "$f" | LC_ALL=C tr '[:upper:]' '[:lower:]')"
    u_tp="${TRACKED_LOWER[$u_low]:-}"
    u_alias=0
    if [ -n "$u_tp" ]; then if [ "$TOP/$f" -ef "$TOP/$u_tp" ]; then u_alias=1; fi; fi
    if [ "$u_alias" -eq 1 ]; then notinc "$f" "追跡ファイルの別表記(同一 inode)"; ALIAS_N=$((ALIAS_N + 1)); continue; fi
    u_size="$(wc -c <"$f" | tr -d ' ')"
    if [ "$u_size" -gt 1048576 ]; then omit_u "$f" "1 MiB 超"; continue; fi
    if head_has_nul "$f"; then omit_u "$f" "バイナリ"; continue; fi
  fi
  if [ "$CAP_HIT" -eq 1 ]; then omit_u "$f" "総量上限"; continue; fi
  if [ $((UNTRACKED_TOTAL + u_size)) -gt "$MAX_BYTES" ]; then
    CAP_HIT=1
    omit_u "$f" "総量上限"
    continue
  fi
  # toplevel 相対パスが `-` そのものだと git diff --no-index も stdin と解釈するので呼ばない
  if [ "$f" = "-" ]; then
    notinc "$f" "diff 失敗"
    FAILED=$((FAILED + 1))
    continue
  fi
  UNTRACKED_TOTAL=$((UNTRACKED_TOTAL + u_size))
  rc=0
  git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false -c core.filemode=true -c core.symlinks=true $LFS_OFF diff --unified=3 --no-ext-diff --no-textconv --no-color --no-renames --submodule=short --ignore-submodules=dirty --text --src-prefix=a/ --dst-prefix=b/ --no-index -- /dev/null "$f" >"$TMPD/ud" 2>"$TMPD/uderr" || rc=$?
  if [ "$rc" -eq 0 ] || { [ "$rc" -eq 1 ] && [ -s "$TMPD/ud" ]; }; then
    cat "$TMPD/ud" >>"$SEC_UNTRACKED"
    CNT_A=$((CNT_A + 1))
  else
    notinc "$f" "diff 失敗"
    FAILED=$((FAILED + 1))
  fi
done 3<"$TMPD/untracked.z" </dev/null
if [ "$CAP_HIT" -eq 1 ]; then add_note "未追跡の総量上限に達した"; fi
if [ "$ALIAS_N" -gt 0 ]; then add_note "追跡ファイルの別表記(同一 inode): $ALIAS_N 件"; fi
if [ -s "$SEC_OUTSIDE" ]; then add_note "対象外の分類はリポジトリ内の一覧に基づく"; fi

# ── `.git` という名前のエントリと読めないディレクトリの走査 ──
declare -A GITLINKS=()
while IFS= read -r -d '' rec; do
  if [ "${rec%% *}" = 160000 ]; then GITLINKS["${rec#*$'\t'}"]=1; fi
done <"$TMPD/stage.z"
inside_set() { # $1=パス $2=set 名(nested|gitlink)
  local k
  if [ "$2" = nested ]; then
    if [ ${#NESTED[@]} -eq 0 ]; then return 1; fi
    for k in "${!NESTED[@]}"; do
      if [ "$1" = "$k" ]; then return 0; fi
      case "$1" in "$k"/*) return 0 ;; esac
    done
  else
    if [ ${#GITLINKS[@]} -eq 0 ]; then return 1; fi
    for k in "${!GITLINKS[@]}"; do
      if [ "$1" = "$k" ]; then return 0; fi
      case "$1" in "$k"/*) return 0 ;; esac
    done
  fi
  return 1
}
FIND_RC=0
( CDPATH= cd -P -- "$TOP" && find . -path ./.git -prune -o -name .git -prune -print0 -o -type d -print0 ) >"$TMPD/find.z" 2>/dev/null || FIND_RC=$?
if [ "$FIND_RC" -ne 0 ]; then add_note "\`.git\` 名エントリ・ディレクトリの走査に失敗した経路あり"; fi
SUBGIT=()
GITNAME_N=0
while IFS= read -r -d '' e; do
  if [ "$e" = "." ]; then continue; fi
  e="${e#./}"
  case "$e" in
    .git|*/.git)
      if inside_set "$e" nested; then continue; fi
      if inside_set "$e" gitlink; then SUBGIT[${#SUBGIT[@]}]="$e"; continue; fi
      if [ -L "$e" ]; then notinc "$e" ".git という名前のエントリ"
      elif [ -d "$e" ]; then notinc "$e/" ".git という名前のエントリ"
      else notinc "$e" ".git という名前のエントリ"
      fi
      GITNAME_N=$((GITNAME_N + 1)) ;;
    *)
      if inside_set "$e" nested; then continue; fi
      if inside_set "$e" gitlink; then continue; fi
      if [ ! -r "$e" ]; then notinc "$e/" "読めない"; FAILED=$((FAILED + 1)); fi ;;
  esac
done <"$TMPD/find.z"
if [ "$GITNAME_N" -gt 0 ]; then add_note "\`.git\` という名前のエントリ: $GITNAME_N 件"; fi
if [ ${#SUBGIT[@]} -gt 0 ]; then
  sg_list=""
  for p in "${SUBGIT[@]}"; do sg_list="$sg_list $(path_field "$p")"; done
  add_note "サブモジュール配下の \`.git\` 名エントリ: ${#SUBGIT[@]} 件(${sg_list# })"
fi

# ── 改竄耐性 ⑥: gitignore の可視化 ──
rc=0
git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false ls-files -o --ignored --exclude-per-directory=.gitignore -z >"$TMPD/ignA.z" 2>/dev/null || rc=$?
rc=0
git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false ls-files -o --ignored --exclude-standard -z >"$TMPD/ignB.z" 2>/dev/null || rc=$?
declare -A IGN_A=()
IGN_G=0
IGN_GITIGNORE=()
while IFS= read -r -d '' p; do
  IGN_A["$p"]=1
  IGN_G=$((IGN_G + 1))
  case "$p" in .gitignore|*/.gitignore) IGN_GITIGNORE[${#IGN_GITIGNORE[@]}]="$p" ;; esac
done <"$TMPD/ignA.z"
while IFS= read -r -d '' p; do
  if [ -z "${IGN_A[$p]:-}" ]; then printf '%s\n' "$(path_field "$p")" >>"$SEC_GITIGNORE"; CNT_F=$((CNT_F + 1)); fi
done <"$TMPD/ignB.z"
if [ "$IGN_G" -gt 0 ]; then
  add_note "gitignore 済みの未追跡 $IGN_G 件はこの snapshot の対象外(\`.gitignore\` が覆う領域の変更はレビューされない)"
fi
EXCL_FILE="$(git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false rev-parse --path-format=absolute --git-path info/exclude 2>/dev/null || printf '')"
if [ -n "$EXCL_FILE" ] && [ -f "$EXCL_FILE" ] && [ ! -L "$EXCL_FILE" ] && [ -r "$EXCL_FILE" ]; then
  if grep -qv -e '^#' -e '^[[:space:]]*$' "$EXCL_FILE" 2>/dev/null; then add_note "\`.git/info/exclude\` あり"; fi
fi
UNTRACKED_NAMED=()
collect_named() { # $1=ファイル名(.gitignore / .gitattributes)
  UNTRACKED_NAMED=()
  local p
  while IFS= read -r -d '' p; do
    case "$p" in "$1"|*/"$1") UNTRACKED_NAMED[${#UNTRACKED_NAMED[@]}]="$p" ;; esac
  done <"$TMPD/untracked.z"
  while IFS= read -r -d '' p; do
    case "$p" in "$1"|*/"$1") UNTRACKED_NAMED[${#UNTRACKED_NAMED[@]}]="$p" ;; esac
  done <"$TMPD/ignA.z"
}
control_changed() { # $1=ファイル名 → 変更・追加があれば 0
  # 追跡側が変更されていても未追跡側のパスを NOTE に載せるため、早期 return しない
  local rc2=0 out hit=0
  UNTRACKED_NAMED=()
  out="$(git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false -c core.filemode=true -c core.symlinks=true diff --name-only --no-renames --ignore-submodules=dirty "$BASE" -- "$1" "**/$1" 2>/dev/null)" || rc2=$?
  if [ "$rc2" -eq 0 ] && [ -n "$out" ]; then hit=1; fi
  collect_named "$1"
  if [ ${#UNTRACKED_NAMED[@]} -gt 0 ]; then hit=1; fi
  [ "$hit" -eq 1 ]
}
if control_changed ".gitignore"; then
  gi_list=""
  for p in ${UNTRACKED_NAMED[@]+"${UNTRACKED_NAMED[@]}"}; do gi_list="$gi_list $(path_field "$p")"; done
  add_note "\`.gitignore\` が基準から変更、または未追跡で追加された${gi_list:+(未追跡:${gi_list})}"
fi
if control_changed ".gitattributes"; then
  ga_list=""
  for p in ${UNTRACKED_NAMED[@]+"${UNTRACKED_NAMED[@]}"}; do ga_list="$ga_list $(path_field "$p")"; done
  add_note "\`.gitattributes\` が変更された${ga_list:+(未追跡:${ga_list})}"
fi

# ── filter=lfs のパス(変更・未追跡のうち)──
LFS_N=0
for p in ${INC_T[@]+"${INC_T[@]}"}; do
  if [ "${ATTR_FILTER[$p]:-}" = lfs ]; then LFS_N=$((LFS_N + 1)); fi
done
while IFS= read -r -d '' p; do
  if [ "${ATTR_FILTER[$p]:-}" = lfs ]; then LFS_N=$((LFS_N + 1)); fi
done <"$TMPD/untracked.z"
if [ "$LFS_N" -gt 0 ]; then
  add_note "\`filter=lfs\` のパス $LFS_N 件は LFS ポインタではなく作業ツリーの実内容で比較した"
fi

# ── --patch-out(未追跡の列挙が終わった後に書き出す)──
if [ -n "$PATCH_OUT" ]; then
  : >"$TMPD/patch.raw"
  rc=0
  git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false -c core.filemode=true -c core.symlinks=true diff --name-only --no-renames --ignore-submodules=dirty -z "$PATCH_BASE" >"$TMPD/pdiff.z" 2>"$TMPD/pdiff.err" || rc=$?
  lfs_diag_or_fail "$rc" "$TMPD/pdiff.err"
  PINC=(); PEXC=()
  while IFS= read -r -d '' p; do
    classify "$p" tracked
    case "$CLASS" in
      default|secret) PEXC[${#PEXC[@]}]="$p" ;;
      *) PINC[${#PINC[@]}]="$p" ;;
    esac
  done <"$TMPD/pdiff.z"
  if [ ${#PINC[@]} -gt 0 ]; then
    INC_ARR=(); for p in "${PINC[@]}"; do INC_ARR[${#INC_ARR[@]}]="$p"; done
    CAND_ARR=(); for p in ${PEXC[@]+"${PEXC[@]}"}; do CAND_ARR[${#CAND_ARR[@]}]="$p"; done
    build_exclude_pathspec
    PS=()
    for p in "${PINC[@]}"; do PS[${#PS[@]}]=":(literal)$p"; done
    for p in ${EXC_OUT[@]+"${EXC_OUT[@]}"}; do PS[${#PS[@]}]=":(literal,exclude)$p"; done
    rc=0
    git --no-pager --no-replace-objects -c core.quotePath=false -c core.fsmonitor= -c core.hooksPath=/dev/null -c core.ignoreCase=false -c core.splitIndex=false -c core.filemode=true -c core.symlinks=true $LFS_OFF diff --binary --unified=3 --no-ext-diff --no-textconv --no-color --no-renames --submodule=short --ignore-submodules=dirty --text --src-prefix=a/ --dst-prefix=b/ "$PATCH_BASE" -- "${PS[@]}" >"$TMPD/patch.raw" 2>"$TMPD/patch.err" || rc=$?
    lfs_diag_or_fail "$rc" "$TMPD/patch.err"
  fi
  WANT_PATCH=1
fi

# ── 改竄耐性 ③: --text 付きでも残った `Binary files` の事後検出 ──
: >"$TMPD/binlines"
grep -h '^Binary files ' "$SEC_BODY" "$SEC_UNTRACKED" >"$TMPD/binlines" 2>/dev/null || true
if [ -s "$TMPD/binlines" ]; then
  while IFS= read -r bl; do
    bp="${bl#Binary files }"
    bp="${bp% and *}"
    add_tamper "${bp#a/}" "Binary files 検出" "-" "" 0
  done <"$TMPD/binlines"
  SHOW_DIGEST=0
  emit_tamper_rows "$SEC_TAMPER"
  emit_tamper_rows ""
  WANT_PATCH=0
fi

# ── 要約と公開 ──
CNT_N=$((CNT_A + CNT_B1 + CNT_B2 + CNT_C + CNT_D + CNT_E))
SUMMARY="要約: 未追跡 $CNT_N 件(本文 $CNT_A / 除外(機密) $CNT_B1 / 除外(既定) $CNT_B2 / 対象外 $CNT_C / 省略 $CNT_D / 含められず $CNT_E) / gitignore 外 $CNT_F"
assemble_out ""
publish_outputs
trap - ERR
if [ "$TAMPER" -eq 1 ]; then exit 22; fi
if [ "$FAILED" -gt 0 ]; then exit 21; fi
exit 0
