#!/usr/bin/env bash
# Usage: replicate.sh N "LABEL" [env assignments...]
N="$1"; LABEL="$2"; shift 2
SC="$(cd "$(dirname "$0")" && pwd)"
green=0; red=0; err=0
for i in $(seq 1 "$N"); do
  out=$(env "$@" "$SC/loop.sh" "$SC/probe_variant.py" 2>&1)
  if   echo "$out" | grep -q "== GREEN"; then green=$((green+1)); v=GREEN
  elif echo "$out" | grep -q "== RED";   then red=$((red+1));   v=RED
  else err=$((err+1)); v=ERR; fi
  proc=$(echo "$out" | grep -oE 'processed=[0-9]+' | head -1)
  printf '   run %d: %-5s %s\n' "$i" "$v" "$proc"
done
printf '  >> %s : GREEN %d/%d | RED %d/%d | ERR %d\n\n' "$LABEL" "$green" "$N" "$red" "$N" "$err"
