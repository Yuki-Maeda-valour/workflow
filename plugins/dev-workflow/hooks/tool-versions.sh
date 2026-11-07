#!/bin/sh
# dev-workflow が使う道具の版を確かめ、表で知らせる(止めない)。
# 道具と最低の版は、同じフォルダの tool-requirements.tsv が正本。このスクリプトに版の数字を書かない。
#
# 使い方:
#   sh tool-versions.sh                  いつも確かめて、表を標準出力に出す(setup.sh の最後・手で確かめるとき)
#   sh tool-versions.sh --session-start  Claude Code の SessionStart の hook(hooks.json)から呼ぶ。
#                                        プラグインの版が、前に確かめた版と違うときだけ確かめ、JSON を 1 つ出す
#                                        (systemMessage は利用者に見せる文、additionalContext は Claude に渡す文)。
#                                        DEV_WORKFLOW_LOOP_ITER が空でなければ(無人ループのセッション)、何もしない
#
# 前に確かめた版の記録: ${XDG_STATE_HOME:-$HOME/.local/state}/dev-workflow/tool-versions/checked-version
# 終了コード: 使い方の誤りは 2。ほかは、道具が足りなくても、確かめられなくても 0(起動を止めない)。
# POSIX の sh で書く(bash・python3 が無い機械でも、足りないことを知らせられるように)。
# 確かめる道具(git・grep・sort・realpath など)を、このスクリプトの処理には使わない(sed・awk・tr・mkdir・mv・rm だけ)。

LC_ALL=C
export LC_ALL

mode=report
case $# in
  0) ;;
  1)
    case $1 in
      --session-start) mode=session ;;
      -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
      *) echo "使い方の誤り: $1(引数なしか --session-start)" >&2; exit 2 ;;
    esac
    ;;
  *) echo "使い方の誤り: 引数は 1 つまで" >&2; exit 2 ;;
esac

if [ "$mode" = session ] && [ -n "${DEV_WORKFLOW_LOOP_ITER:-}" ]; then
  exit 0
fi

tab=$(printf '\t')
nl='
'
IFS=" $tab$nl"

# 自分の置き場(物理パス)と、一覧・plugin.json
case $0 in
  */*) here=${0%/*} ;;
  *) here=. ;;
esac
here=$(CDPATH= cd -P -- "$here" 2>/dev/null && pwd -P) || here=
list=${here:+$here/tool-requirements.tsv}
manifest=${here:+$here/../.claude-plugin/plugin.json}

# プロジェクトの中のファイル(yaml.py など)を読まないように、道具を呼ぶ前に / へ移る
cd / 2>/dev/null || :

plugin_version=
if [ -n "$manifest" ] && [ -f "$manifest" ]; then
  plugin_version=$(sed -n 's/^[[:space:]]*"version"[[:space:]]*:[[:space:]]*"\([0-9A-Za-z.+-]*\)".*$/\1/p' "$manifest" 2>/dev/null | sed -n 1p)
fi
shown_version=${plugin_version:-(版を読めない)}

# 前に確かめた版の記録の置き場
state_base=
case ${XDG_STATE_HOME:-} in
  /*) state_base=$XDG_STATE_HOME ;;
esac
if [ -z "$state_base" ]; then
  case ${HOME:-} in
    /*) state_base=$HOME/.local/state ;;
  esac
fi
state_dir=${state_base:+$state_base/dev-workflow/tool-versions}
state_file=${state_dir:+$state_dir/checked-version}

if [ "$mode" = session ] && [ -n "$state_file" ] && [ -n "$plugin_version" ] && [ -f "$state_file" ]; then
  previous=
  IFS= read -r previous < "$state_file" 2>/dev/null || :
  if [ "$previous" = "$plugin_version" ]; then
    exit 0
  fi
fi

# 版の数字(1.2.3 の形)を 1 行から取り出す。$2 があれば、その文字列の後ろから取る
first_version() {
  fv_line=$1
  if [ -n "${2:-}" ]; then
    case $fv_line in
      *"$2"*) fv_line=${fv_line#*"$2"} ;;
      *) return 1 ;;
    esac
  fi
  printf '%s\n' "$fv_line" | sed -n 's/^[^0-9]*\([0-9][0-9]*\(\.[0-9][0-9]*\)*\).*$/\1/p'
}

# $1 の版が $2 以上なら 0、低ければ 1、比べられなければ 2。数字の組を左から比べる(足りない組は 0 とみなす)
version_ge() {
  vg_a=$1
  vg_b=$2
  # 比べる前に、両方のすべての数を確かめる(数字でないものと、桁の多すぎる数〈sh の数の上限を超えないように〉は比べない)
  for vg_all in "$vg_a" "$vg_b"; do
    vg_rest=$vg_all
    while :; do
      case $vg_rest in
        *.*) vg_n=${vg_rest%%.*}; vg_rest=${vg_rest#*.} ;;
        *) vg_n=$vg_rest; vg_rest= ;;
      esac
      case $vg_n in
        ''|*[!0-9]*|??????????*) return 2 ;;
      esac
      [ -n "$vg_rest" ] || break
    done
  done
  while :; do
    case $vg_a in
      '') vg_x=0 ;;
      *.*) vg_x=${vg_a%%.*}; vg_a=${vg_a#*.} ;;
      *) vg_x=$vg_a; vg_a= ;;
    esac
    case $vg_b in
      '') vg_y=0 ;;
      *.*) vg_y=${vg_b%%.*}; vg_b=${vg_b#*.} ;;
      *) vg_y=$vg_b; vg_b= ;;
    esac
    case $vg_x$vg_y in
      *[!0-9]*) return 2 ;;
    esac
    # 桁の多すぎる数は比べない(sh の数の上限を超えないように)
    case $vg_x in ??????????*) return 2 ;; esac
    case $vg_y in ??????????*) return 2 ;; esac
    if [ "$vg_x" -gt "$vg_y" ]; then return 0; fi
    if [ "$vg_x" -lt "$vg_y" ]; then return 1; fi
    if [ -z "$vg_a" ] && [ -z "$vg_b" ]; then return 0; fi
  done
}

# 実行名の --version の最初の行
version_line() {
  "$1" --version 2>&1 </dev/null | sed -n 1p
}

# 1 行を確かめ、found(見つけた版)と result(結果)を決める
check_row() {
  cr_probe=$1
  cr_min=$2
  cr_need=$3
  found=-
  result=
  case $cr_probe in
    version:*)
      cr_cmd=${cr_probe#version:}
      if command -v "$cr_cmd" >/dev/null 2>&1; then
        found=$(first_version "$(version_line "$cr_cmd")")
        [ -n "$found" ] || { found=-; result=版を読めない; }
      else
        result=無い
      fi
      ;;
    gnu:*)
      cr_rest=${cr_probe#gnu:}
      cr_pkg=${cr_rest%%:*}
      cr_cmds=${cr_rest#*:}
      cr_low=
      cr_old_ifs=$IFS
      IFS=,
      set -f
      for cr_cmd in $cr_cmds; do
        IFS=$cr_old_ifs
        [ -z "$result" ] || continue
        if ! command -v "$cr_cmd" >/dev/null 2>&1; then
          result="無い($cr_cmd)"
          continue
        fi
        cr_line=$(version_line "$cr_cmd")
        case $cr_line in
          *"(GNU $cr_pkg)"*) ;;
          *) result="GNU の版でない($cr_cmd)"; continue ;;
        esac
        cr_v=$(first_version "$cr_line" "(GNU $cr_pkg)")
        if [ -z "$cr_v" ]; then
          result="版を読めない($cr_cmd)"
          continue
        fi
        if [ -z "$cr_low" ]; then
          cr_low=$cr_v
        else
          version_ge "$cr_v" "$cr_low"
          case $? in
            0) ;;
            1) cr_low=$cr_v ;;
            *) result="版を読めない($cr_cmd)" ;;
          esac
        fi
      done
      IFS=$cr_old_ifs
      set +f
      if [ -z "$result" ] && [ -n "$cr_low" ]; then found=$cr_low; fi
      ;;
    python-module:*)
      cr_mod=${cr_probe#python-module:}
      if ! command -v python3 >/dev/null 2>&1; then
        result="無い(python3 が無い)"
      else
        # 名前は一覧の値。英数字と _ のほかは読み込まない
        case $cr_mod in
          ''|*[!A-Za-z0-9_]*) result=版を読めない ;;
          *)
            cr_out=$(python3 -B -c "import $cr_mod, sys; sys.stdout.write(str(getattr($cr_mod, '__version__', '')))" 2>/dev/null </dev/null) || result=無い
            if [ -z "$result" ]; then
              found=$(first_version "$cr_out")
              [ -n "$found" ] || found=-
            fi
            ;;
        esac
      fi
      ;;
    command:*)
      cr_cmds=${cr_probe#command:}
      cr_old_ifs=$IFS
      IFS=,
      set -f
      for cr_cmd in $cr_cmds; do
        IFS=$cr_old_ifs
        [ -n "$result" ] || command -v "$cr_cmd" >/dev/null 2>&1 || result="無い($cr_cmd)"
      done
      IFS=$cr_old_ifs
      set +f
      ;;
    *)
      result=確かめ方を読めない
      ;;
  esac
  if [ -z "$result" ]; then
    if [ "$cr_min" = - ]; then
      result=足りる
    elif [ "$found" = - ]; then
      result=版を読めない
    else
      version_ge "$found" "$cr_min"
      case $? in
        0) result=足りる ;;
        1) result=足りない ;;
        *) result=版を読めない ;;
      esac
    fi
  fi
  case $cr_need in
    任意)
      [ "$result" = 足りる ] || result="$result(任意)"
      ;;
    条件つき)
      [ "$result" = 足りる ] || result="$result(条件つき)"
      ;;
  esac
}

table=
fixes=
short=0
cond=0
rows=0
if [ -z "$list" ] || [ ! -f "$list" ]; then
  table="道具の一覧(tool-requirements.tsv)を読めないので、確かめられませんでした。"
else
  while IFS=$tab read -r name probe min need use fix || [ -n "$name" ]; do
    case $name in
      ''|'#'*) continue ;;
    esac
    rows=$((rows + 1))
    check_row "$probe" "$min" "$need"
    if [ "$min" = - ]; then want=- ; else want="$min 以上"; fi
    table="$table| $name | $found | $want | $result | $use |$nl"
    case $result in
      足りる|*"(任意)") ;;
      *)
        short=$((short + 1))
        [ "$need" != 条件つき ] || cond=$((cond + 1))
        fixes="$fixes- $name($use): $fix$nl"
        ;;
    esac
  done < "$list"
  if [ "$rows" -eq 0 ]; then
    table="道具の一覧(tool-requirements.tsv)に行が無いので、確かめられませんでした。"
  else
    table="| 道具 | 見つけた版 | 要る版 | 結果 | 使う所 |$nl|---|---|---|---|---|$nl$table"
  fi
fi

if [ "$mode" = session ]; then
  message="dev-workflow $shown_version が使う道具の版を確かめました(この版で初めての起動)。$nl$nl$table"
else
  message="dev-workflow $shown_version が使う道具の版:$nl$nl$table"
fi
if [ "$short" -gt 0 ]; then
  if [ "$cond" -gt 0 ]; then
    message="$message${nl}足りない道具が $short 件あります(うち $cond 件は条件つき。使う所に当たるときだけ要ります)。止めずに続けます。直し方:$nl$fixes"
  else
    message="$message${nl}足りない道具が $short 件あります。止めずに続けます。直し方:$nl$fixes"
  fi
elif [ "$rows" -gt 0 ]; then
  message="$message${nl}足りない道具はありません。$nl"
fi

if [ "$mode" = session ]; then
  recorded=no
  if [ -n "$state_file" ] && [ -n "$plugin_version" ]; then
    tmp=$state_dir/.checked-version.$$
    if mkdir -p "$state_dir" 2>/dev/null && printf '%s\n' "$plugin_version" > "$tmp" 2>/dev/null && mv -f "$tmp" "$state_file" 2>/dev/null; then
      recorded=yes
    else
      rm -f "$tmp" 2>/dev/null
    fi
  fi
  if [ "$recorded" = yes ]; then
    message="$message${nl}次に知らせるのは、プラグインの版が変わった後の最初の起動です。"
  else
    message="$message${nl}確かめた版を記録できませんでした。次の起動でも知らせます。"
  fi
fi
if [ -n "$here" ]; then
  message="$message${nl}手で確かめるには: sh \"$here/tool-versions.sh\""
fi

if [ "$mode" = report ]; then
  printf '%s\n' "$message"
  exit 0
fi

# JSON の文字列にする: 改行とタブのほかの制御文字を消し、\ と " とタブと改行をエスケープする
escaped=$(printf '%s' "$message" | tr -d '\001-\010\013-\037\177' | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e "s/$tab/\\\\t/g" | awk 'NR > 1 { printf "\\n" } { printf "%s", $0 }')
printf '{"systemMessage":"%s","hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$escaped" "$escaped"
exit 0
