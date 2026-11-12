#!/bin/bash
# cunquote の `printf -v ch "\\$oct"` を、代表の文字の組で測る。1 行に: 書式の16進 TAB 終了値 TAB 値の16進 TAB stderr(16進)
set -u
export LC_ALL=C
chars=()
for c in 0 1 7 8 9 a b c d e f g i n o q r s t u v x z A E F G Q T U X h j l L % '\' '"' "'" '?' ' ' '-' '#' '.' '*' '(' ')' '+' k y '$' $'\001' $'\t' $'\n' $'\x7f' $'\x80' $'\xc3' $'\xff'; do chars+=("$c"); done
hex() { printf '%s' "$1" | od -An -v -tx1 | tr -d ' \n'; }
err="$1/err"
for o in 0 1 3 7; do
  for x in "" "${chars[@]}"; do
    for y in "" "${chars[@]}"; do
      [ -z "$x" ] && [ -n "$y" ] && continue
      oct="$o$x$y"
      ch='\'
      rc=0
      printf -v ch "\\$oct" 2>"$err" || rc=$?
      printf '%s\t%s\t%s\t%s\n' "$(hex "$oct")" "$rc" "$(hex "$ch")" "$(od -An -v -tx1 < "$err" | tr -d ' \n')"
    done
  done
done
